"""Encrypted credential storage for builtin connectors.

For Python-implemented connectors that need an API key or username/password
(email-imap, banking-fints, twilio, etc.). Encrypted at rest with Fernet
(AES-128-CBC + HMAC-SHA256). The master key lives in a separate file with
mode 0600 so a DB leak alone doesn't expose credentials.

n8n-backed connectors store their credentials in n8n's own encrypted store —
this module is for the Python-side connectors only.

Master key management:
  - Path: $HOMEOS_CREDENTIAL_KEY_PATH (default data/.credential_key)
  - Auto-generated on first write (Fernet.generate_key)
  - Mode 0600, owner-only
  - Back up data/ to back up credentials
  - Rotating the key invalidates all stored credentials; users would need
    to re-enter them. Not automated yet; a future "rotate" admin tool.

Payload shape (after JSON encoding, then Fernet encrypting):
  {"<field>": "<value>", ...}

Each connector declares its credentials_schema in its ConnectorSpec.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken

from .database import DEFAULT_DB_PATH, conn_ctx

log = logging.getLogger("homeos.credentials")

KEY_PATH = Path(os.getenv("HOMEOS_CREDENTIAL_KEY_PATH", "data/.credential_key"))
DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)

_cached_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """Load or auto-generate the master key, return a Fernet instance."""
    global _cached_fernet
    if _cached_fernet is not None:
        return _cached_fernet
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        key = Fernet.generate_key()
        # Write atomically with 0600 mode set BEFORE any data lands.
        tmp = KEY_PATH.with_suffix(KEY_PATH.suffix + ".tmp")
        with open(tmp, "wb") as f:
            f.write(key)
        os.chmod(tmp, 0o600)
        os.replace(tmp, KEY_PATH)
        log.info("credential_store: generated new master key at %s (0600)", KEY_PATH)
    else:
        # Belt-and-braces: enforce 0600 even if the user copied it in lax.
        try:
            os.chmod(KEY_PATH, 0o600)
        except OSError:
            pass
    key = KEY_PATH.read_bytes().strip()
    _cached_fernet = Fernet(key)
    return _cached_fernet


def put(connector_name: str, credentials: Dict[str, Any]) -> None:
    """Encrypt + store credentials for `connector_name`. Overwrites any existing row."""
    if not isinstance(credentials, dict):
        raise ValueError("credentials must be a dict")
    payload = _get_fernet().encrypt(json.dumps(credentials).encode("utf-8"))
    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO connector_credentials (connector_name, payload, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(connector_name) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
            (connector_name, payload),
        )


def get(connector_name: str) -> Optional[Dict[str, Any]]:
    """Decrypt + return credentials. None if not configured."""
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT payload FROM connector_credentials WHERE connector_name = ?",
            (connector_name,),
        ).fetchone()
    if not row:
        return None
    try:
        raw = _get_fernet().decrypt(bytes(row["payload"]))
    except InvalidToken:
        log.error(
            "credential_store: failed to decrypt credentials for %s — master key "
            "may have been rotated. User needs to re-enter credentials.",
            connector_name,
        )
        return None
    return json.loads(raw.decode("utf-8"))


def delete(connector_name: str) -> bool:
    with conn_ctx(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM connector_credentials WHERE connector_name = ?",
            (connector_name,),
        )
    return cur.rowcount > 0


def list_configured() -> List[Dict[str, Any]]:
    """Public list (no payload) — what's been credentialed, when."""
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT connector_name, updated_at FROM connector_credentials ORDER BY connector_name"
        ).fetchall()
    return [dict(r) for r in rows]


def migrate_from_sqlite_if_needed() -> Dict[str, str]:
    """One-shot migration for the common upgrade path:
    install.sh ran with the default SQLite backend and bootstrapped
    Immich + Paperless API keys into data/family.db. The operator
    then flipped YORIK_DB_BACKEND=postgres for multi-tenant. Without
    this migration the postgres connector_credentials table is empty
    and provision_immich/provision_paperless raise "admin key not
    configured" — even though the keys exist on disk one DB over.

    Mechanism: copy the encrypted blob verbatim (same Fernet key file
    is shared between SQLite + Postgres installs), so we never see
    plaintext in memory and the operation is safe regardless of the
    credential schema version.

    Idempotent: per-connector skip when postgres already has a row.
    Safe to call repeatedly. Returns {connector_name: "migrated" |
    "already_present" | "<error>"} so the caller can log a summary.

    No-op when:
      - Backend is SQLite (the source of truth IS family.db).
      - Backend is Postgres AND the SQLite file is missing.
      - The bundled Yorik is itself a tenant (its connector creds
        live in the tenant's own DB; nothing to migrate).
    """
    out: Dict[str, str] = {}
    if (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() != "postgres":
        return out
    sqlite_path = Path(os.getenv("HOMEOS_DB_PATH", "data/family.db"))
    if not sqlite_path.exists():
        return out
    import sqlite3 as _sql
    try:
        src = _sql.connect(str(sqlite_path))
        src.row_factory = _sql.Row
        try:
            sqlite_rows = src.execute(
                "SELECT connector_name, payload FROM connector_credentials"
            ).fetchall()
        except _sql.OperationalError:
            return out  # table never created on this install
        finally:
            src.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("credential_store: SQLite open failed during migration: %s", exc)
        return out
    if not sqlite_rows:
        return out
    for row in sqlite_rows:
        name = row["connector_name"]
        try:
            with conn_ctx(DB_PATH) as pg:
                existing = pg.execute(
                    "SELECT 1 FROM connector_credentials WHERE connector_name = ?",
                    (name,),
                ).fetchone()
                if existing:
                    out[name] = "already_present"
                    continue
                pg.execute(
                    "INSERT INTO connector_credentials (connector_name, payload, updated_at) "
                    "VALUES (?, ?, datetime('now'))",
                    (name, bytes(row["payload"])),
                )
            out[name] = "migrated"
            log.info("credential_store: migrated %s from SQLite to Postgres", name)
        except Exception as exc:  # noqa: BLE001
            out[name] = f"error: {exc}"
            log.warning("credential_store: migration failed for %s: %s", name, exc)

    # Second leg: Paperless's first-run path wrote its admin token to
    # the legacy app_settings table (not connector_credentials). Without
    # promoting it here, tenant signups on a flipped-to-postgres install
    # see no `paperless` entry and skip provisioning. Read from SQLite
    # app_settings, write through credential_store.put() so the row gets
    # Fernet-encrypted into the Postgres connector_credentials table.
    if "paperless" not in out:
        try:
            src = _sql.connect(str(sqlite_path))
            src.row_factory = _sql.Row
            try:
                tok_row = src.execute(
                    "SELECT value FROM app_settings WHERE key = 'paperless_api_token'"
                ).fetchone()
                url_row = src.execute(
                    "SELECT value FROM app_settings WHERE key = 'paperless_base_url'"
                ).fetchone()
            except _sql.OperationalError:
                tok_row = url_row = None
            finally:
                src.close()
            if tok_row and tok_row["value"]:
                with conn_ctx(DB_PATH) as pg:
                    has_pg = pg.execute(
                        "SELECT 1 FROM connector_credentials WHERE connector_name = 'paperless'"
                    ).fetchone()
                if not has_pg:
                    put("paperless", {
                        "api_key":  tok_row["value"],
                        "base_url": (url_row["value"] if url_row else "http://localhost:8010"),
                    })
                    out["paperless"] = "migrated_from_app_settings"
                    log.info("credential_store: promoted paperless admin token from app_settings to Postgres")
        except Exception as exc:  # noqa: BLE001
            out["paperless"] = f"error: {exc}"
            log.warning("credential_store: paperless promotion failed: %s", exc)
    return out
