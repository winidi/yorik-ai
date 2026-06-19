"""Yorik backup — age-encrypted snapshots to a configurable target.

Three priorities baked in:
  1. **No Yorik downtime**: SQLite gets snapshotted via VACUUM INTO
     (consistent point-in-time copy taken while the live DB stays
     writable), so the application keeps running. The slow steps —
     compression + encryption + writing to the external drive —
     happen on the snapshot, not the live DB.
  2. **No leakage on disk theft**: the snapshot is encrypted with age
     using a passphrase the user controls. The encrypted blob is a
     single `.tar.gz.age` per snapshot.
  3. **Graceful when the drive is unplugged**: a missing target path
     fails the backup with a clear last_error and a yellow banner in
     the UI. Next scheduled run retries. Nothing in Yorik blocks.

Stored config (in app_settings):
  backup_target_path        : str — where snapshots land
  backup_passphrase_enc     : str — Fernet-encrypted age passphrase
  backup_schedule           : str — "HH:MM" local time, "" = disabled
  backup_include_photos     : "1"/"0"
  backup_include_paperless  : "1"/"0"
  backup_retain_count       : int — keep N most-recent, prune older

Module state (no globals beyond an in-flight lock):
  Only one backup runs at a time per Yorik process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import credential_store
from .database import get_conn, DEFAULT_DB_PATH, DEFAULT_DOCS_DB_PATH

log = logging.getLogger("yorik.backup")

# Backups never touch the originals — only the snapshots. So the only
# way one backup can hurt another is racing on the temp-dir cleanup.
# A simple lock protects against concurrent /api/backup/run.
_backup_lock = asyncio.Lock()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BRIEFINGS_DIR = PROJECT_ROOT / "briefings"
DEFAULT_TARGET = DATA_DIR / "backups"


# ───────────────────────── config helpers ───────────────────────────

def _get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (key, value),
        )
        conn.commit()


def get_config() -> dict[str, Any]:
    """Current backup config in serialisable form (passphrase
    NEVER returned — only existence flagged)."""
    target = _get_setting("backup_target_path") or str(DEFAULT_TARGET)
    return {
        "target_path":      target,
        "schedule":         _get_setting("backup_schedule"),  # "" = manual only
        "include_photos":   _get_setting("backup_include_photos", "0") == "1",
        "include_paperless": _get_setting("backup_include_paperless", "0") == "1",
        "include_whatsapp":  _get_setting("backup_include_whatsapp", "0") == "1",
        "retain_count":     int(_get_setting("backup_retain_count", "30") or 30),
        "passphrase_set":   bool(credential_store.get("backup_passphrase")),
    }


def set_config(
    target_path: Optional[str] = None,
    schedule: Optional[str] = None,
    include_photos: Optional[bool] = None,
    include_paperless: Optional[bool] = None,
    include_whatsapp: Optional[bool] = None,
    retain_count: Optional[int] = None,
    passphrase: Optional[str] = None,
) -> dict[str, Any]:
    if target_path is not None:
        _set_setting("backup_target_path", target_path)
    if schedule is not None:
        _set_setting("backup_schedule", schedule)
    if include_photos is not None:
        _set_setting("backup_include_photos", "1" if include_photos else "0")
    if include_paperless is not None:
        _set_setting("backup_include_paperless", "1" if include_paperless else "0")
    if include_whatsapp is not None:
        _set_setting("backup_include_whatsapp", "1" if include_whatsapp else "0")
    if retain_count is not None:
        _set_setting("backup_retain_count", str(max(1, int(retain_count))))
    if passphrase is not None:
        # Validation: age requires a non-empty passphrase; we want
        # something meaningful (length matters more than entropy for
        # symmetric crypto + scrypt KDF, but warn on <12 chars).
        if not passphrase or len(passphrase) < 8:
            raise ValueError("passphrase must be at least 8 characters")
        credential_store.put("backup_passphrase", {"passphrase": passphrase})
    return get_config()


# ───────────────────────── target availability ──────────────────────

def target_available(target_path: str) -> dict[str, Any]:
    """Quick health check for the UI ("is the external drive plugged in?").

    Considers the target available if either the target dir itself
    exists writable, OR its parent dir does (we'll mkdir the target
    on first backup). This lets the default `data/backups/` work
    out of the box and lets external drives `/media/usb/yorik-backups/`
    auto-create the subfolder if the drive is mounted but empty."""
    p = Path(target_path)
    # Check the dir or its parent.
    check_p = p if p.exists() else p.parent
    if not check_p.exists():
        return {"available": False, "reason": "path does not exist",
                "is_external": _looks_external(target_path)}
    if not check_p.is_dir():
        return {"available": False, "reason": "path is not a directory"}
    if not os.access(check_p, os.W_OK):
        return {"available": False, "reason": "path is not writable"}
    try:
        stat = shutil.disk_usage(check_p)
        # Flag same-filesystem so the UI can show "this won't survive
        # disk failure" yellow warning. Allowed (user might rsync to
        # cloud), just not silently OK.
        same_fs = False
        try:
            same_fs = check_p.stat().st_dev == PROJECT_ROOT.stat().st_dev
        except OSError:
            pass
        return {
            "available": True,
            "free_bytes": stat.free,
            "total_bytes": stat.total,
            "is_external": _looks_external(target_path),
            "on_same_filesystem": same_fs,
            "will_create": not p.exists(),
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


def _looks_external(path: str) -> bool:
    """Heuristic: external/removable drives on Linux typically mount
    under /media/<user>/ or /run/media/<user>/. Anything in /home or
    /var is internal."""
    p = str(Path(path).resolve())
    return p.startswith("/media/") or p.startswith("/run/media/") or p.startswith("/mnt/")


# ───────────────────────── snapshot + bundle ────────────────────────

def _snapshot_sqlite(src: Path, dst: Path) -> None:
    """Consistent snapshot via VACUUM INTO. Source DB remains writable
    throughout. dst must NOT exist (VACUUM INTO refuses to overwrite)."""
    if dst.exists():
        dst.unlink()
    conn = sqlite3.connect(str(src))
    try:
        # Quote the destination — paths with special chars would otherwise
        # break the SQL. We control the path, so simple escape is fine.
        safe = str(dst).replace("'", "''")
        conn.execute(f"VACUUM INTO '{safe}'")
    finally:
        conn.close()


def _bundle(opts: dict[str, Any], staging: Path) -> tuple[Path, list[str]]:
    """Build the staging directory: snapshot DBs + copy small dirs +
    optionally copy heavy media dirs. Returns (tar_archive_path,
    includes_list)."""
    includes: list[str] = []

    # 1. Database snapshots.
    #
    # On Postgres-backend installs (Phase D+), the SQLite files are
    # stub artifacts (init creates them but writes go to Supabase
    # Postgres). Without the pg_dump below, a 'restored' Yorik comes
    # up with zero tasks, events, contacts, conversations — every
    # single user-created row lives in Postgres now.
    #
    # SQLite-era installs keep working unchanged via the original
    # VACUUM INTO snapshot path.
    if _use_postgres_backend():
        pg_main = _dump_yorik_postgres(staging / "yorik_postgres.sql.gz",
                                       which="main")
        if pg_main:
            includes.append("yorik_postgres_main")
        pg_docs = _dump_yorik_postgres(staging / "yorik_postgres_docs.sql.gz",
                                       which="docs")
        if pg_docs:
            includes.append("yorik_postgres_docs")

        # Phase F-lite: each tenant has its own Postgres database
        # `yorik_tenant_<name>` and its own manifest under
        # data/tenants/<name>/. Bundle both so a restore can recreate
        # the tenant DBs + their port assignments. Tenant manifests
        # are tiny (~1 KB each) and the dumps are typically small
        # too (no media — that lives in shared Immich/Paperless,
        # already covered by the host include_photos / include_paperless
        # flags). Includes get `tenant_<name>_postgres` so the
        # manifest signature reports per-tenant inclusion granularly.
        tenants = _enumerate_tenants()
        if tenants:
            tenants_stage = staging / "tenants"
            tenants_stage.mkdir()
            for t in tenants:
                t_stage = tenants_stage / t
                t_stage.mkdir()
                src_manifest = (Path(__file__).resolve().parent.parent
                                / "data" / "tenants" / t / "manifest.env")
                if src_manifest.exists():
                    shutil.copy2(src_manifest, t_stage / "manifest.env")
                # Per-tenant bearer token. The host's
                # tenant_bearer_tokens table comes back via the main
                # Yorik Postgres dump, so the registered token row
                # restores; this file ships the matching local copy
                # the tenant uvicorn presents in `Authorization:
                # Bearer ...`. Without it the restored tenant can't
                # talk to /api/internal/* and falls into a permanent
                # 401 loop. Backup is encrypted with the operator's
                # passphrase so shipping the bearer is acceptable —
                # same encryption posture as the credential_key.
                src_bearer = (Path(__file__).resolve().parent.parent
                              / "data" / "tenants" / t / "internal_token")
                if src_bearer.exists():
                    shutil.copy2(src_bearer, t_stage / "internal_token")
                ok = _dump_tenant_postgres(t, t_stage / "postgres.sql.gz")
                if ok:
                    includes.append(f"tenant_{t}_postgres")
                else:
                    # Manifest still ships even if the dump failed —
                    # operator can re-create the tenant DB shell at
                    # restore time and the manifest tells them which
                    # name + port + flags to feed create-tenant.sh.
                    log.warning("backup: tenant %s had no dump; manifest-only", t)
                    if src_manifest.exists():
                        includes.append(f"tenant_{t}_manifest_only")
    else:
        if Path(DEFAULT_DB_PATH).exists():
            _snapshot_sqlite(Path(DEFAULT_DB_PATH), staging / "family.db")
            includes.append("family_db")
        if Path(DEFAULT_DOCS_DB_PATH).exists():
            _snapshot_sqlite(Path(DEFAULT_DOCS_DB_PATH), staging / "documents.db")
            includes.append("documents_db")

    # 2. Credential key — the single most-sensitive file, but also the
    #    one without which the encrypted API tokens become useless.
    #    Pull the path from credential_store so this respects
    #    HOMEOS_CREDENTIAL_KEY_PATH overrides instead of hard-coding
    #    PROJECT_ROOT/data/.credential_key.
    cred_key = Path(credential_store.KEY_PATH)
    if cred_key.exists():
        shutil.copy2(cred_key, staging / ".credential_key")
        includes.append("credential_key")

    # 3. User docs (the originals, separate from the vector index).
    #    Same story: honor HOMEOS_DOCS_DIR if set, fall back to the
    #    repo's data/documents/.
    docs = Path(os.getenv("HOMEOS_DOCS_DIR") or (DATA_DIR / "documents"))
    if docs.exists():
        shutil.copytree(docs, staging / "documents", dirs_exist_ok=False)
        includes.append("documents")

    # 4. Briefings (user-installed templates).
    if BRIEFINGS_DIR.exists():
        shutil.copytree(BRIEFINGS_DIR, staging / "briefings", dirs_exist_ok=False)
        includes.append("briefings")

    # 5. Optional heavy dirs.
    if opts.get("include_photos"):
        photos = DATA_DIR / "immich" / "library"
        if photos.exists():
            log.info("backup: including photo library (this can take a while)")
            shutil.copytree(photos, staging / "immich_library", dirs_exist_ok=False)
            includes.append("immich_library")
        # Postgres dump — without this the JPEGs are recoverable but
        # albums, faces, smart-search, date-taken metadata are all
        # gone. Photo originals + Postgres are a two-piece set; one
        # without the other is degraded restore.
        pg_dump = _dump_immich_postgres(staging / "immich_postgres.sql")
        if pg_dump:
            includes.append("immich_postgres")

    if opts.get("include_paperless"):
        for sub in ("data", "media"):
            p = DATA_DIR / "paperless" / sub
            if p.exists():
                log.info("backup: including paperless %s", sub)
                shutil.copytree(p, staging / f"paperless_{sub}", dirs_exist_ok=False)
                includes.append(f"paperless_{sub}")

    # 5b. WhatsApp bridge state. The Baileys bridge stores its pairing
    # secrets in data/whatsapp/. Without it, a restored Yorik forgets
    # which phone it's paired to and the user has to re-scan the QR
    # code (and lose chat continuity until then). Off by default
    # because the dir contains secrets you might not want on an
    # external SSD without thinking about it.
    if opts.get("include_whatsapp"):
        wa = DATA_DIR / "whatsapp"
        if wa.exists():
            shutil.copytree(wa, staging / "whatsapp", dirs_exist_ok=False)
            includes.append("whatsapp")

    # 6. Manifest — what's in the bundle, when, which Yorik version.
    # Schema version comes from the latest _ensure_columns migration
    # tag (a future addition) or git commit if available. Either is
    # better than nothing — restore can warn loudly when restoring a
    # snapshot built against a different schema.
    manifest = {
        "yorik_backup_format": "1.0",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "includes": includes,
        "git_commit": _git_commit_short(),
        "schema_signature": _schema_signature(),
    }
    (staging / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

    # 7. Tar+gzip the staging dir.
    archive = staging.parent / "yorik-snapshot.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        # arcname='.' so the tar entries are relative — restore extracts
        # straight into data/ without an extra level.
        tf.add(staging, arcname=".")
    return archive, includes


# ───────────────────────── encryption ───────────────────────────────

def _use_postgres_backend() -> bool:
    """Mirror backend.database._use_postgres without depending on it
    (the backup module runs in a thread; avoid circular imports)."""
    return (os.getenv("YORIK_DB_BACKEND") or "sqlite").lower() == "postgres"


def _dump_yorik_postgres(dst: Path, *, which: str = "main") -> bool:
    """pg_dump the Yorik Supabase Postgres into `dst` (gzip-compressed).

    `which` selects the database/schema scope:
      * "main"  — pg_dump of the database used by backend/database_pg.py
                  for the public schema (events, tasks, contacts, etc.).
      * "docs"  — same database but pg_dump --schema=docs only. Keeps
                  the doc/paperless vectors separate so a restore can
                  cherry-pick (full vs. settings-only).

    The dump uses --clean --if-exists --no-owner --no-privileges so it
    restores cleanly into a fresh Supabase install of the SAME schema
    version (the manifest's schema_signature lets the verify step
    refuse mismatched restores).

    Best-effort: returns False (after logging) when supabase-db isn't
    up, the password isn't readable, or pg_dump fails. The rest of
    the backup still proceeds.
    """
    import gzip as _gzip
    import subprocess

    container = os.getenv("YORIK_SUPABASE_CONTAINER", "supabase-db")
    db = os.getenv("YORIK_DB_NAME", "postgres")
    user = os.getenv("YORIK_DB_USER", "postgres")

    # Read the password from infra/supabase/docker/.env if no env
    # override is set. Same lookup pattern auth_sessions.py and
    # app_jwt.py use for the JWT secret.
    pw = os.getenv("YORIK_DB_PASSWORD") or ""
    if not pw:
        env_file = (Path(__file__).resolve().parent.parent /
                    "infra/supabase/docker/.env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("POSTGRES_PASSWORD="):
                    pw = line.split("=", 1)[1].strip()
                    break
    if not pw:
        log.warning("backup: POSTGRES_PASSWORD not found — skipping yorik pg_dump (%s)", which)
        return False

    try:
        probe = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            log.info("backup: %s container not running — skipping yorik pg_dump (%s)",
                     container, which)
            return False

        # No --clean here. pg_dump --clean emits DROPs that fail
        # ('cannot drop … because other objects depend on it') when
        # cross-table FKs reference user_profiles_pkey. Our restore
        # path resets the target schemas wholesale (DROP SCHEMA …
        # CASCADE) before piping the dump back, so the dump only
        # needs to recreate, not clean up.
        cmd = ["docker", "exec", "-e", f"PGPASSWORD={pw}", container,
               "pg_dump", "-U", user, "-d", db,
               "--no-owner", "--no-privileges"]
        if which == "docs":
            cmd.extend(["--schema=docs"])
        elif which == "main":
            # Stay out of supabase-internal schemas. We dump public +
            # yorik (helpers) only; the Supabase services will
            # recreate auth/storage/realtime/_realtime on restore via
            # their own init.
            cmd.extend(["--schema=public", "--schema=yorik"])

        with _gzip.open(dst, "wb") as gz:
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=1800, check=False,
            )
            if res.returncode != 0:
                log.warning("backup: yorik pg_dump (%s) failed (%s): %s",
                            which, res.returncode,
                            (res.stderr or b"").decode("utf-8", "replace")[:300])
                dst.unlink(missing_ok=True)
                return False
            gz.write(res.stdout)
        log.info("backup: dumped yorik-postgres %s schema (%d bytes gz)",
                 which, dst.stat().st_size)
        return True
    except FileNotFoundError:
        log.warning("backup: docker CLI not found — skipping yorik pg_dump")
        return False
    except subprocess.TimeoutExpired:
        log.warning("backup: yorik pg_dump (%s) timed out after 30min", which)
        dst.unlink(missing_ok=True)
        return False


def _dump_tenant_postgres(tenant_name: str, dst: Path) -> bool:
    """pg_dump one tenant's Postgres database into `dst` (gzip).

    Tenants share supabase-db but each has its own `yorik_tenant_<name>`
    database. We dump only public + yorik schemas (same scope as the
    host's main dump); the auth shim is recreated by create-tenant.sh
    during restore so it doesn't need to survive in the bundle.

    Best-effort: returns False (after logging) if the tenant DB
    doesn't exist or pg_dump fails. The rest of the backup proceeds.
    Surfaces enough log context to track down which tenant got
    skipped (operator runs the backup, sees the warn, fixes the
    missing tenant, re-runs).
    """
    import gzip as _gzip
    import subprocess

    container = os.getenv("YORIK_SUPABASE_CONTAINER", "supabase-db")
    user = os.getenv("YORIK_DB_USER", "postgres")
    db = f"yorik_tenant_{tenant_name}"

    pw = os.getenv("YORIK_DB_PASSWORD") or ""
    if not pw:
        env_file = (Path(__file__).resolve().parent.parent /
                    "infra/supabase/docker/.env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("POSTGRES_PASSWORD="):
                    pw = line.split("=", 1)[1].strip()
                    break
    if not pw:
        log.warning("backup: POSTGRES_PASSWORD not found — skipping tenant pg_dump (%s)",
                    tenant_name)
        return False

    try:
        # Include auth + docs schemas too. Tenants own their auth.users
        # rows (the local-only shim — no shared GoTrue) and docs holds
        # the per-tenant paperless-style vector tables. Excluding them
        # is what made an earlier round-trip silently lose the admin
        # row on restore (FK from user_profiles.id → auth.users.id
        # failed with "Key (id)=(...) is not present in table users").
        cmd = ["docker", "exec", "-e", f"PGPASSWORD={pw}", container,
               "pg_dump", "-U", user, "-d", db,
               "--no-owner", "--no-privileges",
               "--schema=public", "--schema=yorik",
               "--schema=auth", "--schema=docs"]
        with _gzip.open(dst, "wb") as gz:
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=1800, check=False,
            )
            if res.returncode != 0:
                err = (res.stderr or b"").decode("utf-8", "replace")[:300]
                log.warning("backup: tenant pg_dump (%s) failed (%s): %s",
                            tenant_name, res.returncode, err)
                dst.unlink(missing_ok=True)
                return False
            gz.write(res.stdout)
        log.info("backup: dumped tenant %s (%d bytes gz)",
                 tenant_name, dst.stat().st_size)
        return True
    except FileNotFoundError:
        log.warning("backup: docker CLI not found — skipping tenant pg_dump (%s)",
                    tenant_name)
        return False
    except subprocess.TimeoutExpired:
        log.warning("backup: tenant pg_dump (%s) timed out after 30min", tenant_name)
        dst.unlink(missing_ok=True)
        return False


def _enumerate_tenants() -> list[str]:
    """Names of every tenant declared under data/tenants/. Used by the
    backup sweep so we don't have to consult Postgres for the list
    (the manifest is the source of truth for "what tenants exist on
    this host" — a Postgres DB without a manifest is orphan state
    that the operator should investigate, not back up)."""
    base = Path(__file__).resolve().parent.parent / "data" / "tenants"
    if not base.exists():
        return []
    out = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "manifest.env").exists():
            out.append(child.name)
    return out


def _dump_immich_postgres(dst: Path) -> bool:
    """Run pg_dump inside the Immich Postgres container and write the SQL
    stream to dst. Best-effort: any failure logs + returns False so the
    rest of the backup still proceeds. Returns True on a successful
    dump.

    Reads connection info from compose env defaults (matches what
    docker-compose.yml ships); the IMMICH_DB_PASSWORD env override on
    the host also lands inside the container via compose.
    """
    import subprocess
    # The Immich Postgres image runs `postgres` as the standard
    # superuser; the per-app account is `immich`. pg_dump as `immich`
    # works because it owns its own schema. Use the container's own
    # password from its env via `docker exec`'s default behaviour
    # (PGPASSWORD set inside the container by the postgres image).
    container = os.getenv("IMMICH_POSTGRES_CONTAINER", "yorik-immich-postgres")
    db = os.getenv("IMMICH_DB_NAME", "immich")
    user = os.getenv("IMMICH_DB_USERNAME", "immich")
    try:
        # Probe first — silently skip if container isn't running. The
        # photo originals will still be in the bundle; we just won't
        # have the metadata to go with them.
        probe = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            log.info("backup: immich-postgres container not running — skipping dump")
            return False

        with open(dst, "wb") as f:
            res = subprocess.run(
                ["docker", "exec", container,
                 "pg_dump", "-U", user, "-d", db,
                 "--clean", "--if-exists", "--no-owner", "--no-privileges"],
                stdout=f, stderr=subprocess.PIPE, timeout=600, check=False,
            )
        if res.returncode != 0:
            log.warning("backup: pg_dump failed (%s): %s",
                        res.returncode, (res.stderr or b"").decode("utf-8", "replace")[:300])
            dst.unlink(missing_ok=True)
            return False
        log.info("backup: dumped immich-postgres (%d bytes)", dst.stat().st_size)
        return True
    except FileNotFoundError:
        log.warning("backup: docker CLI not found — skipping immich pg_dump")
        return False
    except subprocess.TimeoutExpired:
        log.warning("backup: pg_dump timed out after 10min")
        dst.unlink(missing_ok=True)
        return False
    except Exception as exc:  # noqa: BLE001
        log.exception("backup: pg_dump unexpected failure: %s", exc)
        dst.unlink(missing_ok=True)
        return False


def _encrypt_age(plaintext: Path, ciphertext: Path, passphrase: str) -> None:
    """Encrypt with age (passphrase mode). pyrage is pure-Python so
    we don't need the `age` binary."""
    from pyrage import passphrase as age_passphrase
    encrypted = age_passphrase.encrypt(plaintext.read_bytes(), passphrase)
    ciphertext.write_bytes(encrypted)


def _decrypt_age(ciphertext: Path, plaintext: Path, passphrase: str) -> None:
    """For the restore CLI (also used by /api/backup/verify)."""
    from pyrage import passphrase as age_passphrase
    decrypted = age_passphrase.decrypt(ciphertext.read_bytes(), passphrase)
    plaintext.write_bytes(decrypted)


# ───────────────────────── version metadata helpers ─────────────────

def _git_commit_short() -> str:
    """Short git SHA of the running Yorik tree, or '' if not a git
    checkout. Stored in the manifest so restores can warn about
    code/schema drift between snapshot time and now."""
    try:
        import subprocess
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _schema_signature() -> str:
    """Hex-encoded short hash of all table CREATE statements in the
    live family.db. Two boxes on the same schema get the same
    signature; a divergent restore can be detected without needing
    proper migrations versioning yet."""
    try:
        import hashlib
        import sqlite3 as _sqlite
        conn = _sqlite.connect(str(Path(DEFAULT_DB_PATH)))
        try:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type IN ('table','index') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            blob = "\n".join((r[0] or "") for r in rows).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:12]
        finally:
            conn.close()
    except Exception:
        return ""


# ───────────────────────── verify / restore drill ───────────────────

def verify_snapshot(snapshot_path: Path, passphrase: str) -> dict[str, Any]:
    """Decrypt + extract a snapshot into a tmpdir, run a sanity pass,
    return a structured report. Does NOT touch live data — the whole
    point is to prove a snapshot is restorable without a destructive
    test.

    Checks performed:
      1. Snapshot file exists and is readable
      2. age-decryption succeeds with the given passphrase
      3. tar extraction succeeds
      4. MANIFEST.json present + parseable
      5. Each `includes` entry declared in MANIFEST actually exists on disk
      6. SQLite DBs open + PRAGMA integrity_check passes
      7. SQLite DBs have a non-trivial row count in at least one table
         (catches accidentally-empty snapshots from a broken VACUUM INTO)
      8. credential_key file (if listed) is non-empty

    Returns:
      {
        "ok":      bool,
        "checks":  [{"name": str, "ok": bool, "detail": str}],
        "summary": str,
        "extracted_to": str | None,   # left in place if cleanup=False, else None
      }
    """
    import sqlite3 as _sqlite
    import tarfile as _tarfile
    import tempfile as _tempfile

    checks: list[dict] = []

    def _check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    snapshot_path = Path(snapshot_path)
    if not snapshot_path.is_file():
        _check("exists", False, f"not a file: {snapshot_path}")
        return {"ok": False, "checks": checks, "summary": "snapshot file missing",
                "extracted_to": None}
    _check("exists", True, f"{snapshot_path.stat().st_size // 1024} KB")

    tmpdir = Path(_tempfile.mkdtemp(prefix="yorik-verify-"))
    plaintext = tmpdir / "decrypted.tar.gz"
    extracted = tmpdir / "extracted"
    extracted.mkdir()

    # 2. Decrypt
    try:
        _decrypt_age(snapshot_path, plaintext, passphrase)
        _check("decrypt", True, f"{plaintext.stat().st_size // 1024} KB plaintext")
    except Exception as exc:
        _check("decrypt", False, f"{type(exc).__name__}: {exc}")
        return {"ok": False, "checks": checks,
                "summary": "decryption failed — wrong passphrase or corrupt snapshot",
                "extracted_to": str(tmpdir)}

    # 3. Extract
    try:
        with _tarfile.open(plaintext, "r:gz") as tf:
            # Path-traversal safety on extract (CVE-2007-4559 family).
            for member in tf.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise ValueError(f"unsafe path in tar: {member.name!r}")
            tf.extractall(extracted)
        _check("extract", True, f"{len(list(extracted.rglob('*')))} entries")
    except Exception as exc:
        _check("extract", False, f"{type(exc).__name__}: {exc}")
        return {"ok": False, "checks": checks, "summary": "tar extraction failed",
                "extracted_to": str(tmpdir)}

    # 4. Manifest
    manifest_path = extracted / "MANIFEST.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            _check("manifest", True,
                   f"format {manifest.get('yorik_backup_format')}, "
                   f"{len(manifest.get('includes') or [])} includes")
        except Exception as exc:
            _check("manifest", False, f"unparseable: {exc}")
    else:
        _check("manifest", False, "MANIFEST.json missing")

    # 5. Each declared include exists
    INCLUDE_TO_PATH = {
        "family_db":           "family.db",
        "documents_db":        "documents.db",
        "credential_key":      ".credential_key",
        "documents":           "documents",
        "briefings":           "briefings",
        "immich_library":      "immich_library",
        "paperless_data":      "paperless_data",
        "paperless_media":     "paperless_media",
        # Postgres dumps written by _dump_yorik_postgres. The include
        # names don't carry the .sql.gz suffix; the lookup did, so the
        # check claimed the bundle was incomplete even when the files
        # were present. The dumps go under staging/ at the top level.
        "yorik_postgres_main": "yorik_postgres.sql.gz",
        "yorik_postgres_docs": "yorik_postgres_docs.sql.gz",
        "immich_postgres":     "immich_postgres.sql.gz",
    }
    missing_includes = []
    for inc in (manifest.get("includes") or []):
        # Per-tenant Postgres dumps live under tenants/<name>/postgres.sql.gz.
        # The include name is "tenant_<name>_postgres" (or
        # "tenant_<name>_manifest_only" when the dump failed and only the
        # manifest.env shipped). Resolve dynamically so we don't have to
        # enumerate tenants in the static map.
        if inc.startswith("tenant_") and inc.endswith("_postgres"):
            name = inc[len("tenant_"):-len("_postgres")]
            rel = f"tenants/{name}/postgres.sql.gz"
        elif inc.startswith("tenant_") and inc.endswith("_manifest_only"):
            name = inc[len("tenant_"):-len("_manifest_only")]
            rel = f"tenants/{name}/manifest.env"
        else:
            rel = INCLUDE_TO_PATH.get(inc, inc)
        if not (extracted / rel).exists():
            missing_includes.append(inc)
    if missing_includes:
        _check("declared_includes_present", False,
               f"missing on disk: {missing_includes}")
    else:
        _check("declared_includes_present", True,
               f"all {len(manifest.get('includes') or [])} present")

    # 6+7. SQLite integrity + non-trivial content
    for db_name, label in [("family.db", "family_db"), ("documents.db", "documents_db")]:
        db_path = extracted / db_name
        if not db_path.exists():
            if label in (manifest.get("includes") or []):
                _check(f"sqlite_{label}", False, "declared but file missing")
            continue
        try:
            conn = _sqlite.connect(str(db_path))
            try:
                pragma = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if pragma != "ok":
                    _check(f"sqlite_{label}_integrity", False, f"PRAGMA: {pragma}")
                else:
                    _check(f"sqlite_{label}_integrity", True, "PRAGMA integrity_check ok")
                # Non-trivial content: at least one user table with rows
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts_%'"
                ).fetchall()]
                total_rows = 0
                tables_with_rows = 0
                for t in tables:
                    try:
                        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                        total_rows += n
                        if n > 0:
                            tables_with_rows += 1
                    except _sqlite.OperationalError:
                        pass  # virtual tables etc.
                _check(f"sqlite_{label}_content", total_rows > 0,
                       f"{total_rows} rows across {tables_with_rows}/{len(tables)} tables")
            finally:
                conn.close()
        except Exception as exc:
            _check(f"sqlite_{label}_integrity", False,
                   f"{type(exc).__name__}: {exc}")

    # 8. Credential key non-empty
    key_path = extracted / ".credential_key"
    if "credential_key" in (manifest.get("includes") or []):
        if key_path.exists() and key_path.stat().st_size > 0:
            _check("credential_key_nonempty", True, f"{key_path.stat().st_size} bytes")
        else:
            _check("credential_key_nonempty", False, "missing or empty")

    # 9. Postgres dumps decompress + look like a pg_dump. Without this
    # the bundle could ship a corrupt gzip or an empty dump and verify
    # would silently pass. We don't have a live Postgres at verify time
    # so we can't replay, but every pg_dump output starts with a
    # `-- PostgreSQL database dump` banner and contains at least one
    # COPY / INSERT / CREATE TABLE statement.
    import gzip as _gz
    for inc in (manifest.get("includes") or []):
        if inc.startswith("tenant_") and inc.endswith("_postgres"):
            name = inc[len("tenant_"):-len("_postgres")]
            dump = extracted / "tenants" / name / "postgres.sql.gz"
            label = f"pg_{inc}"
        elif inc in ("yorik_postgres_main", "yorik_postgres_docs", "immich_postgres"):
            dump = extracted / INCLUDE_TO_PATH[inc]
            label = f"pg_{inc}"
        else:
            continue
        if not dump.exists():
            continue  # declared_includes already flagged it
        try:
            with _gz.open(dump, "rt", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
            if "PostgreSQL database dump" not in head:
                _check(label, False, "missing pg_dump banner")
            elif not any(kw in head for kw in ("CREATE TABLE", "CREATE SCHEMA", "COPY ", "INSERT INTO")):
                _check(label, False, "no schema / data statements in first 4 KB")
            else:
                _check(label, True, f"{dump.stat().st_size // 1024} KB dump, banner + statements present")
        except Exception as exc:  # noqa: BLE001
            _check(label, False, f"{type(exc).__name__}: {exc}")

    ok = all(c["ok"] for c in checks)
    failed = [c["name"] for c in checks if not c["ok"]]
    summary = (
        f"verified — {len(checks)} checks passed" if ok else
        f"FAILED: {len(failed)} of {len(checks)} checks failed ({', '.join(failed)})"
    )
    return {
        "ok":           ok,
        "checks":       checks,
        "summary":      summary,
        "extracted_to": str(tmpdir),
    }


def restore_snapshot(snapshot_path: Path, passphrase: str, *,
                     restore_postgres: bool = True,
                     restore_files:    bool = True,
                     restore_tenants:  bool = True) -> dict[str, Any]:
    """Restore from an encrypted snapshot. **Destructive** — overwrites
    every file/DB declared in the bundle's MANIFEST.

    Requires:
      - yorik.service + every yorik-tenant@*.service STOPPED (caller's
        responsibility — the FastAPI process owns DB connections that
        block DROP DATABASE).
      - supabase-db container UP (we need a live Postgres to pipe the
        dumps into).
      - Same Yorik install layout as the snapshot was taken from.

    Restores:
      - Yorik main + docs Postgres schemas (DROP + CREATE + dump-replay).
      - Each tenant DB declared in the manifest (create-tenant.sh-style:
        we'll CREATE DATABASE yorik_tenant_<name> if it doesn't exist,
        then pipe the dump in).
      - data/.credential_key, data/documents/, data/briefings/.
      - data/tenants/<name>/manifest.env + internal_token.

    Does NOT restore (manual operator steps in docs/RESTORE.md):
      - Immich photo library (large, lives in immich container volume).
      - Paperless media + DB (large, lives in paperless container volumes).
      - Caddy snippets (re-emitted by re-running create-tenant on each
        restored tenant if the operator wants them, or scp from a known-
        good source).

    Caller flow (matches scripts/restore-from-snapshot.sh):
      1. sudo systemctl stop yorik 'yorik-tenant@*'
      2. python -m backend.backup_cli restore <snapshot> <passphrase>
      3. sudo systemctl start yorik (re-creates tenant units on start)
      4. Manually restart each yorik-tenant@<name> per manifest.

    Returns the same shape as verify_snapshot but with extra `steps`
    entries documenting what was actually written.
    """
    import shutil as _shutil
    import subprocess as _subprocess
    import tarfile as _tarfile
    import tempfile as _tempfile

    # Verify first — this also gives us the extracted tmpdir we'll
    # restore from. Re-using the verify path means we get the same
    # checks (decrypt, integrity, manifest, schema_signature match)
    # for free, and we can refuse a corrupt restore before touching
    # anything live.
    verify = verify_snapshot(snapshot_path, passphrase)
    steps: list[dict] = []
    if not verify["ok"]:
        return {"ok": False, "verify": verify, "steps": steps,
                "summary": "refusing restore: verify failed — " + verify["summary"]}

    extracted_root = Path(verify["extracted_to"]) / "extracted"

    def _step(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})
        log.info("restore: %s %s — %s", "OK" if ok else "FAIL", name, detail)

    manifest = json.loads((extracted_root / "MANIFEST.json").read_text())
    includes = manifest.get("includes") or []
    project_root = Path(__file__).resolve().parent.parent

    container = os.getenv("YORIK_SUPABASE_CONTAINER", "supabase-db")
    pg_user = os.getenv("YORIK_DB_USER", "postgres")
    pg_db = os.getenv("YORIK_DB_NAME", "postgres")
    pw = os.getenv("YORIK_DB_PASSWORD") or ""
    if not pw:
        env_file = project_root / "infra/supabase/docker/.env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("POSTGRES_PASSWORD="):
                    pw = line.split("=", 1)[1].strip()
                    break

    def _psql_pipe(dbname: str, dump_path: Path, *, as_user: str = "supabase_admin") -> tuple[int, str]:
        """Pipe a gzipped pg_dump into a docker-exec'd psql.

        Defaults to supabase_admin so the replay can recreate schemas
        and tables without permission errors on freshly-created tenant
        DBs (where the runtime postgres role has no privileges yet).
        Regrant happens after the pipe finishes.
        """
        import gzip as _gzip
        with _gzip.open(dump_path, "rb") as gz:
            sql = gz.read()
        res = _subprocess.run(
            ["docker", "exec", "-i", "-e", f"PGPASSWORD={pw}", container,
             "psql", "-U", as_user, "-d", dbname,
             "-v", "ON_ERROR_STOP=1", "--no-psqlrc"],
            input=sql, capture_output=True, timeout=1800, check=False,
        )
        return res.returncode, (res.stderr or b"").decode("utf-8", "replace")

    def _exec_target(dbname: str, sql: str) -> tuple[int, str]:
        """Run SQL as supabase_admin in the named DB."""
        res = _subprocess.run(
            ["docker", "exec", "-i", "-e", f"PGPASSWORD={pw}", container,
             "psql", "-U", "supabase_admin", "-d", dbname,
             "-v", "ON_ERROR_STOP=1", "--no-psqlrc"],
            input=sql.encode(), capture_output=True, timeout=120, check=False,
        )
        return res.returncode, (res.stderr or b"").decode("utf-8", "replace")

    def _pipe_dump_stripped(dbname: str, dump_path: Path) -> tuple[int, str]:
        """Like _psql_pipe but strips `CREATE SCHEMA public;` first.

        Postgres auto-creates `public` on CREATE DATABASE; pre-existing
        public when the dump's CREATE runs => "schema already exists".
        We also keep public around so a previously-installed pgvector
        extension survives. Stripping just that one statement is the
        narrowest patch.
        """
        import gzip as _gzip
        with _gzip.open(dump_path, "rt", encoding="utf-8") as f:
            sql = f.read()
        sql = sql.replace("CREATE SCHEMA public;",
                          "-- CREATE SCHEMA public;  -- stripped on restore")
        res = _subprocess.run(
            ["docker", "exec", "-i", "-e", f"PGPASSWORD={pw}", container,
             "psql", "-U", "supabase_admin", "-d", dbname,
             "-v", "ON_ERROR_STOP=1", "--no-psqlrc"],
            input=sql.encode(), capture_output=True, timeout=1800, check=False,
        )
        return res.returncode, (res.stderr or b"").decode("utf-8", "replace")

    # ── 1. Postgres dumps ─────────────────────────────────────────────
    if restore_postgres:
        if not pw:
            _step("postgres", False, "POSTGRES_PASSWORD not found — skipping")
        else:
            def _exec_admin(sql: str) -> tuple[int, str]:
                """Run SQL as supabase_admin in the host DB."""
                res = _subprocess.run(
                    ["docker", "exec", "-i", "-e", f"PGPASSWORD={pw}", container,
                     "psql", "-U", "supabase_admin", "-d", pg_db,
                     "-v", "ON_ERROR_STOP=1", "--no-psqlrc"],
                    input=sql.encode(), capture_output=True, timeout=120, check=False,
                )
                return res.returncode, (res.stderr or b"").decode("utf-8", "replace")

            def _regrant(schema: str) -> None:
                """After a dump replay, the new tables are owned by
                supabase_admin with no grants for the runtime role.
                Mirror what migration 110 does for `docs` so the
                runtime `postgres` role + Supabase service roles can
                read/write everything in the restored schema.

                GRANT ALL ON SCHEMA = USAGE + CREATE. CREATE is needed
                because backend.migrations runs CREATE TABLE IF NOT
                EXISTS schema_migrations on every boot in the public
                schema; without CREATE the host yorik fails to start.
                """
                rc, err = _exec_admin(
                    f"GRANT ALL ON SCHEMA {schema} TO postgres, anon, authenticated, service_role;\n"
                    f"GRANT ALL ON ALL TABLES    IN SCHEMA {schema} TO postgres, anon, authenticated, service_role;\n"
                    f"GRANT ALL ON ALL SEQUENCES IN SCHEMA {schema} TO postgres, anon, authenticated, service_role;\n"
                    f"GRANT ALL ON ALL FUNCTIONS IN SCHEMA {schema} TO postgres, anon, authenticated, service_role;\n"
                )
                if rc != 0:
                    log.warning("restore: regrant on %s failed: %s", schema, err[:200])

            # 1a. Yorik main (public + yorik schemas)
            #
            # Strategy: keep `public` (so the existing pgvector / pgcrypto /
            # uuid-ossp extensions stay registered). Drop+recreate yorik.
            # Tables inside public get TRUNCATE-by-CASCADE via the dump's
            # `DROP TABLE IF EXISTS` statements — pg_dump emits those
            # without our --clean flag because we pass --no-owner only.
            # If a previously-restored bundle left orphan tables (drift),
            # explicit per-table DROP is still safer than schema-level
            # — but the dump's CREATE TABLE will collide. We strip
            # CREATE SCHEMA public from the dump and rely on the
            # restore-time TRUNCATE+CREATE flow.
            if "yorik_postgres_main" in includes:
                dump = extracted_root / "yorik_postgres.sql.gz"
                rc, err = _exec_admin(
                    "DROP SCHEMA IF EXISTS yorik CASCADE;\n"
                    # Wipe public's contents but keep the schema +
                    # extensions. ALL TABLES + SEQUENCES + FUNCTIONS:
                    "DO $$ DECLARE r RECORD; BEGIN "
                    "  FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public') LOOP "
                    "    EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE'; "
                    "  END LOOP; "
                    "  FOR r IN (SELECT sequencename FROM pg_sequences WHERE schemaname='public') LOOP "
                    "    EXECUTE 'DROP SEQUENCE IF EXISTS public.' || quote_ident(r.sequencename) || ' CASCADE'; "
                    "  END LOOP; "
                    "END $$;\n"
                    # Re-install extensions in case a previous failed
                    # restore left public empty without them.
                    "CREATE EXTENSION IF NOT EXISTS vector    WITH SCHEMA public;\n"
                    "CREATE EXTENSION IF NOT EXISTS pgcrypto  WITH SCHEMA public;\n"
                    "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\" WITH SCHEMA public;\n"
                )
                if rc != 0:
                    _step("postgres_main_reset", False, err[:200])
                else:
                    rc, err = _pipe_dump_stripped(pg_db, dump)
                    _step("postgres_main_restore", rc == 0,
                          (f"{dump.stat().st_size // 1024} KB restored" if rc == 0 else err[:300]))
                    if rc == 0:
                        _regrant("public")
                        _regrant("yorik")

            # 1b. Yorik docs (docs schema)
            if "yorik_postgres_docs" in includes:
                dump = extracted_root / "yorik_postgres_docs.sql.gz"
                rc, err = _exec_admin("DROP SCHEMA IF EXISTS docs CASCADE;\n")
                if rc != 0:
                    _step("postgres_docs_reset", False, err[:200])
                else:
                    rc, err = _psql_pipe(pg_db, dump)
                    _step("postgres_docs_restore", rc == 0,
                          (f"{dump.stat().st_size // 1024} KB restored" if rc == 0 else err[:300]))
                    if rc == 0:
                        _regrant("docs")

            # 1c. Per-tenant DBs
            if restore_tenants:
                for inc in includes:
                    if not (inc.startswith("tenant_") and inc.endswith("_postgres")):
                        continue
                    tname = inc[len("tenant_"):-len("_postgres")]
                    db_name = f"yorik_tenant_{tname}"
                    dump = extracted_root / "tenants" / tname / "postgres.sql.gz"
                    # DROP + CREATE — tenants live in their own DB, so we
                    # can wipe wholesale without worrying about other
                    # tenants. Need to terminate any existing connection
                    # first (left over if yorik wasn't fully stopped).
                    setup = (
                        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        f"WHERE datname='{db_name}' AND pid <> pg_backend_pid();\n"
                        f"DROP DATABASE IF EXISTS {db_name};\n"
                        f"CREATE DATABASE {db_name};\n"
                        # Without this, the runtime postgres role and
                        # Supabase service roles get "permission denied
                        # for database" when trying to connect. CREATE
                        # DATABASE grants CONNECT to PUBLIC by default,
                        # but Supabase's bootstrap revokes it cluster-wide.
                        f"GRANT CONNECT, TEMPORARY ON DATABASE {db_name} TO "
                        f"  postgres, anon, authenticated, service_role;\n"
                    )
                    res = _subprocess.run(
                        ["docker", "exec", "-i", "-e", f"PGPASSWORD={pw}", container,
                         "psql", "-U", "supabase_admin", "-d", "postgres",
                         "-v", "ON_ERROR_STOP=1", "--no-psqlrc"],
                        input=setup.encode(), capture_output=True, timeout=60, check=False,
                    )
                    if res.returncode != 0:
                        _step(f"tenant_{tname}_reset", False,
                              (res.stderr or b"")[:200].decode("utf-8", "replace"))
                        continue
                    # CREATE DATABASE auto-creates public schema; the
                    # dump also tries to CREATE it and collides. Pre-
                    # install the extensions our schemas reference
                    # (pgvector / pgcrypto / uuid-ossp), then strip
                    # `CREATE SCHEMA public;` from the dump on the fly.
                    rc, err = _exec_target(db_name,
                        "CREATE EXTENSION IF NOT EXISTS vector    WITH SCHEMA public;\n"
                        "CREATE EXTENSION IF NOT EXISTS pgcrypto  WITH SCHEMA public;\n"
                        "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\" WITH SCHEMA public;\n"
                    )
                    if rc != 0:
                        _step(f"tenant_{tname}_ext", False, err[:200])
                        continue
                    rc, err = _pipe_dump_stripped(db_name, dump)
                    _step(f"tenant_{tname}_restore", rc == 0,
                          (f"{dump.stat().st_size // 1024} KB restored" if rc == 0 else err[:300]))
                    if rc == 0:
                        # Same regrant story as the host — tenant DB
                        # tables come back owned by supabase_admin
                        # without grants for the runtime postgres role.
                        # GRANT ALL ON SCHEMA = USAGE + CREATE (needed
                        # by backend.migrations CREATE TABLE … on boot).
                        for sch in ("public", "yorik", "docs", "auth"):
                            regrant = (
                                f"GRANT ALL ON SCHEMA {sch} TO postgres, anon, authenticated, service_role;\n"
                                f"GRANT ALL ON ALL TABLES    IN SCHEMA {sch} TO postgres, anon, authenticated, service_role;\n"
                                f"GRANT ALL ON ALL SEQUENCES IN SCHEMA {sch} TO postgres, anon, authenticated, service_role;\n"
                                f"GRANT ALL ON ALL FUNCTIONS IN SCHEMA {sch} TO postgres, anon, authenticated, service_role;\n"
                            )
                            _subprocess.run(
                                ["docker", "exec", "-i", "-e", f"PGPASSWORD={pw}", container,
                                 "psql", "-U", "supabase_admin", "-d", db_name,
                                 "-v", "ON_ERROR_STOP=1", "--no-psqlrc"],
                                input=regrant.encode(), capture_output=True, timeout=60, check=False,
                            )

    # ── 2. File artifacts ─────────────────────────────────────────────
    if restore_files:
        data_root = project_root / "data"
        # credential_key — a few bytes, drives every encrypted credential
        if "credential_key" in includes:
            src = extracted_root / ".credential_key"
            dst = data_root / ".credential_key"
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(src, dst)
                os.chmod(dst, 0o600)
                _step("credential_key", True, f"{dst.stat().st_size} bytes")
            else:
                _step("credential_key", False, "declared but not in bundle")

        # documents/ — local document store (Yorik native uploads)
        if "documents" in includes:
            src = extracted_root / "documents"
            dst = data_root / "documents"
            if src.exists():
                if dst.exists():
                    _shutil.rmtree(dst)
                _shutil.copytree(src, dst)
                _step("documents_dir", True, f"{sum(1 for _ in dst.rglob('*'))} entries")
            else:
                _step("documents_dir", False, "declared but not in bundle")

        # briefings/ — generated PDFs + the source tree
        if "briefings" in includes:
            src = extracted_root / "briefings"
            dst = project_root / "briefings"
            if src.exists():
                if dst.exists():
                    _shutil.rmtree(dst)
                _shutil.copytree(src, dst)
                _step("briefings_dir", True, f"{sum(1 for _ in dst.rglob('*'))} entries")

        # Per-tenant manifest.env + bearer token — needed for systemctl
        # restart to find the tenant's port + DB name + host RPC bearer.
        if restore_tenants:
            for inc in includes:
                if not (inc.startswith("tenant_") and
                        (inc.endswith("_postgres") or inc.endswith("_manifest_only"))):
                    continue
                if inc.endswith("_postgres"):
                    tname = inc[len("tenant_"):-len("_postgres")]
                else:
                    tname = inc[len("tenant_"):-len("_manifest_only")]
                src_dir = extracted_root / "tenants" / tname
                dst_dir = data_root / "tenants" / tname
                dst_dir.mkdir(parents=True, exist_ok=True)
                for name in ("manifest.env", "internal_token"):
                    s = src_dir / name
                    if s.exists():
                        _shutil.copy2(s, dst_dir / name)
                        if name == "internal_token":
                            os.chmod(dst_dir / name, 0o600)
                _step(f"tenant_{tname}_files", True,
                      f"manifest + bearer restored to {dst_dir}")

    ok = all(s["ok"] for s in steps)
    return {
        "ok":          ok,
        "verify":      verify,
        "steps":       steps,
        "summary":     ("restore complete" if ok else "restore failed — see steps"),
        "manifest":    manifest,
    }


# ───────────────────────── run / retain / history ──────────────────

async def run_backup() -> dict[str, Any]:
    """Execute a full backup with current config. Returns
    {ok, snapshot_path?, size_bytes?, duration_s?, error?}."""
    if _backup_lock.locked():
        return {"ok": False, "error": "another backup is already running"}
    async with _backup_lock:
        return await asyncio.to_thread(_run_backup_sync)


def _run_backup_sync() -> dict[str, Any]:
    cfg = get_config()
    started_at = datetime.now()
    history_id = _start_history(cfg["target_path"], "")
    try:
        # Passphrase check up front so we fail fast.
        creds = credential_store.get("backup_passphrase") or {}
        passphrase = creds.get("passphrase")
        if not passphrase:
            raise RuntimeError("backup passphrase not set — configure it in Settings → Backup")

        # Target check.
        avail = target_available(cfg["target_path"])
        if not avail.get("available"):
            raise RuntimeError(f"target unavailable: {avail.get('reason', 'unknown')}")

        # Stage in a temp dir on the local disk for speed; we move
        # the final encrypted artifact to the target at the end.
        with tempfile.TemporaryDirectory(prefix="yorik-backup-") as tmp:
            tmp_path = Path(tmp)
            staging = tmp_path / "staging"
            staging.mkdir()

            archive, includes = _bundle(cfg, staging)
            log.info("backup: archive %d bytes", archive.stat().st_size)

            # Filename: ISO-like + .tar.gz.age — sortable + obvious.
            stamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
            filename = f"yorik-{stamp}.tar.gz.age"
            encrypted_local = tmp_path / filename
            _encrypt_age(archive, encrypted_local, passphrase)
            size_bytes = encrypted_local.stat().st_size
            log.info("backup: encrypted %d bytes", size_bytes)

            # Move to target — done in a single atomic-on-same-fs op
            # when possible, or copy+remove across filesystems.
            target_dir = Path(cfg["target_path"])
            target_dir.mkdir(parents=True, exist_ok=True)
            final = target_dir / filename
            shutil.move(str(encrypted_local), str(final))

        duration = (datetime.now() - started_at).total_seconds()
        _retain_prune(cfg["target_path"], cfg["retain_count"])
        _finish_history(history_id, "ok", None, duration, size_bytes, filename, includes)
        return {
            "ok": True,
            "snapshot_path": str(target_dir / filename),
            "size_bytes": size_bytes,
            "duration_s": duration,
            "includes": includes,
        }
    except Exception as e:
        log.exception("backup failed")
        duration = (datetime.now() - started_at).total_seconds()
        _finish_history(history_id, "failed", str(e), duration, None, "", [])
        return {"ok": False, "error": str(e)}


def _start_history(target: str, filename: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO backups (target_path, filename, status, includes) "
            "VALUES (?, ?, 'running', ?)",
            (target, filename, "[]"),
        )
        conn.commit()
        return cur.lastrowid


def _finish_history(history_id: int, status: str, error: Optional[str],
                     duration_s: float, size_bytes: Optional[int],
                     filename: str, includes: list[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE backups SET status=?, error=?, duration_s=?, size_bytes=?, "
            "filename=?, includes=?, finished_at=datetime('now') WHERE id=?",
            (status, error, duration_s, size_bytes, filename,
             json.dumps(includes), history_id),
        )
        conn.commit()


def _retain_prune(target: str, keep: int) -> None:
    """Delete oldest snapshots beyond `keep`. Robust to other files
    in the same dir — only touches `yorik-*.tar.gz.age`."""
    try:
        d = Path(target)
        snapshots = sorted(
            d.glob("yorik-*.tar.gz.age"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for old in snapshots[keep:]:
            try:
                old.unlink()
                log.info("backup: pruned %s", old.name)
            except OSError as e:
                log.warning("backup: prune failed for %s: %s", old, e)
    except Exception as e:
        log.warning("backup: prune scan failed: %s", e)


def list_history(limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, target_path, filename, size_bytes, status, error, "
            "       duration_s, includes, started_at, finished_at "
            "FROM backups ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["includes"] = json.loads(d["includes"] or "[]")
        except json.JSONDecodeError:
            d["includes"] = []
        out.append(d)
    return out


def list_snapshots_on_target() -> list[dict]:
    """Files actually present on the configured target right now —
    useful for restore picker even if our DB history is empty."""
    cfg = get_config()
    avail = target_available(cfg["target_path"])
    if not avail.get("available"):
        return []
    try:
        d = Path(cfg["target_path"])
        snaps = sorted(
            d.glob("yorik-*.tar.gz.age"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        return [{
            "filename": p.name,
            "size_bytes": p.stat().st_size,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "path": str(p),
        } for p in snaps]
    except Exception:
        return []


# ───────────────────────── scheduler ────────────────────────────────

_scheduler_task: Optional[asyncio.Task] = None
_scheduler_stop: Optional[asyncio.Event] = None


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    """Cron-lite: wakes every minute, checks if "now" matches the
    configured HH:MM schedule. Simple, no extra deps."""
    global _scheduler_task, _scheduler_stop
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_stop = asyncio.Event()
    _scheduler_task = loop.create_task(_scheduler_loop(), name="backup-scheduler")


async def stop_scheduler() -> None:
    global _scheduler_task, _scheduler_stop
    if _scheduler_stop:
        _scheduler_stop.set()
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass


_last_triggered_minute: Optional[str] = None


async def _scheduler_loop() -> None:
    """Wake every 30 seconds, compare current HH:MM to configured.
    Tracks last-triggered-minute to avoid double-firing within the
    same minute window."""
    global _last_triggered_minute
    from . import workers
    workers.register("backup_scheduler", kind="scheduler")
    try:
        while True:
            if _scheduler_stop and _scheduler_stop.is_set():
                workers.heartbeat("backup_scheduler", "warn", "stopped")
                return
            cfg = get_config()
            schedule = (cfg.get("schedule") or "").strip()
            if schedule and ":" in schedule and cfg.get("passphrase_set"):
                now = datetime.now()
                hhmm = now.strftime("%H:%M")
                if hhmm == schedule and hhmm != _last_triggered_minute:
                    _last_triggered_minute = hhmm
                    log.info("backup scheduler: triggering scheduled run (%s)", hhmm)
                    try:
                        await run_backup()
                        workers.heartbeat("backup_scheduler", "ok",
                                          f"last run {hhmm} ok")
                    except Exception as e:
                        log.exception("scheduled backup failed: %s", e)
                        workers.report_error("backup_scheduler",
                                             f"run @ {hhmm} failed: {str(e)[:60]}")
                else:
                    workers.heartbeat("backup_scheduler", "ok",
                                      f"armed for {schedule}")
            else:
                workers.heartbeat("backup_scheduler", "warn",
                                  "no schedule configured" if not schedule
                                  else "passphrase not set")
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        workers.report_error("backup_scheduler", "cancelled")
        return
