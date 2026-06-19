"""Move per-user Paperless tokens and Immich API keys from plaintext
columns in user_profiles into the Fernet-encrypted credential_store.

Before this migration, the per-user wave-3 provisioning helper stored
the secrets directly:

    UPDATE user_profiles SET paperless_token = ?, immich_api_key = ?

That meant: a leaked family.db gave the attacker every per-user
service token. The maintainer-level connector_credentials table was
always Fernet-encrypted (good), but the per-user wave wasn't (bad,
caught by the May 2026 security audit).

What this migration does
------------------------
For each user_profiles row with a non-null paperless_token or
immich_api_key:

  1. credential_store.put("paperless_user_<id>", {"token": <token>})
     (or "immich_user_<id>", {"api_key": <key>}) — Fernet-encrypts
     the secret into connector_credentials.
  2. UPDATE user_profiles SET paperless_token = NULL — the column
     stays in the schema (SQLite won't let us drop it cleanly) but
     it's empty going forward. New writes go through credential_store.

After the migration, every external_users.get_user_*_creds() read
hits credential_store first; the legacy column is consulted as a
fallback only on the off-chance someone manually re-inserts a
plaintext value (we keep the fallback so a partially-migrated box
doesn't lose access to its data).

Idempotent: a row with NULL plaintext (post-migration) is skipped.
A re-run is harmless.
"""

from __future__ import annotations


def up(conn) -> None:
    # backend.credential_store is import-safe and doesn't touch our
    # transaction (it writes to a separate table within the same
    # connection, which is fine — the BEGIN IMMEDIATE in the
    # migrations runner wraps both writes).
    from backend import credential_store

    rows = conn.execute(
        "SELECT id, paperless_token, immich_api_key "
        "FROM user_profiles "
        "WHERE paperless_token IS NOT NULL OR immich_api_key IS NOT NULL"
    ).fetchall()

    migrated_paperless = 0
    migrated_immich = 0
    for row in rows:
        uid = row[0] if not isinstance(row, dict) else row["id"]
        paperless_token = row[1] if not isinstance(row, dict) else row["paperless_token"]
        immich_api_key = row[2] if not isinstance(row, dict) else row["immich_api_key"]

        if paperless_token:
            credential_store.put(f"paperless_user_{uid}", {"token": paperless_token})
            conn.execute("UPDATE user_profiles SET paperless_token = NULL WHERE id = ?", (uid,))
            migrated_paperless += 1

        if immich_api_key:
            credential_store.put(f"immich_user_{uid}", {"api_key": immich_api_key})
            conn.execute("UPDATE user_profiles SET immich_api_key = NULL WHERE id = ?", (uid,))
            migrated_immich += 1

    # Stamp a hint in app_settings so a curious operator can see what
    # the migration did without git-archaeology.
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
        "VALUES (?, ?, datetime('now'))",
        ("migration_001_summary",
         f"migrated {migrated_paperless} paperless + {migrated_immich} immich tokens "
         f"from user_profiles plaintext into credential_store"),
    )
