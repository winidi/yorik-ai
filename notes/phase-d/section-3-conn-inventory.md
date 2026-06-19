# conn_ctx() / get_conn() call-site inventory — Phase D Section 3.1

Generated 2026-06-15T03:56:40+02:00. Used to confirm there are only two distinct
DB-path patterns (family.db + documents.db) before the database.py
dispatcher is wired.

## Distinct path-argument patterns to conn_ctx / get_conn

```
    249 get_conn()
    136 conn_ctx(DB_PATH)
     66 conn_ctx()
     27 conn_ctx(_DB)
     22 conn_ctx(DEFAULT_DB_PATH)
      2 conn_ctx(path)
      1 get_conn(path: str | None = None)
      1 get_conn(path)
      1 conn_ctx(path: str | None = None)
      1 conn_ctx(os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)
```

## DB_PATH symbol references

```
backend/space_routes.py:24:from .database import conn_ctx, DEFAULT_DB_PATH as _DB
backend/conversation_store.py:25:from .database import DEFAULT_DB_PATH, conn_ctx
backend/conversation_store.py:27:DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)
backend/conversation_store.py:56:        with conn_ctx(DB_PATH) as conn:
backend/conversation_store.py:65:        with conn_ctx(DB_PATH) as conn:
backend/conversation_store.py:84:        with conn_ctx(DB_PATH) as conn:
backend/conversation_store.py:105:        with conn_ctx(DB_PATH) as conn:
backend/conversation_store.py:117:        with conn_ctx(DB_PATH) as conn:
backend/users.py:19:from .database import conn_ctx, DEFAULT_DB_PATH, get_conn
backend/users.py:140:    with conn_ctx(DEFAULT_DB_PATH) as conn:
backend/users.py:219:        with conn_ctx(DEFAULT_DB_PATH) as conn:
backend/users.py:236:    with conn_ctx(DEFAULT_DB_PATH) as conn:
backend/users.py:288:        with conn_ctx(DEFAULT_DB_PATH) as conn:
backend/users.py:335:    with conn_ctx(DEFAULT_DB_PATH) as conn:
backend/users.py:413:    with conn_ctx(DEFAULT_DB_PATH) as conn:
backend/users.py:441:    with conn_ctx(DEFAULT_DB_PATH) as conn:
backend/immich_provisioning.py:30:from .database import conn_ctx, DEFAULT_DB_PATH as _DB
backend/whatsapp_semantic.py:32:from .database import get_conn, get_docs_conn, init_docs_db, DEFAULT_DOCS_DB_PATH
backend/whatsapp_semantic.py:37:DOCS_DB_PATH = os.getenv("HOMEOS_DOCS_DB_PATH", DEFAULT_DOCS_DB_PATH)
backend/whatsapp_semantic.py:46:    init_docs_db(DOCS_DB_PATH)
backend/whatsapp_semantic.py:47:    conn = get_docs_conn(DOCS_DB_PATH)
backend/whatsapp_semantic.py:87:    conn = get_docs_conn(DOCS_DB_PATH)
backend/whatsapp_semantic.py:106:    conn = get_docs_conn(DOCS_DB_PATH)
backend/whatsapp_semantic.py:137:    conn = get_docs_conn(DOCS_DB_PATH)
backend/whatsapp_semantic.py:213:    conn = get_docs_conn(DOCS_DB_PATH)
backend/whatsapp_semantic.py:251:        conn = get_docs_conn(DOCS_DB_PATH)
backend/briefing_snapshots.py:33:from .database import DEFAULT_DB_PATH, conn_ctx
backend/briefing_snapshots.py:37:DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)
backend/briefing_snapshots.py:48:    with conn_ctx(DB_PATH) as conn:
backend/briefing_snapshots.py:68:    with conn_ctx(DB_PATH) as conn:
backend/briefing_snapshots.py:95:    with conn_ctx(DB_PATH) as conn:
backend/briefing_snapshots.py:144:    with conn_ctx(DB_PATH) as conn:
backend/backup.py:44:from .database import get_conn, DEFAULT_DB_PATH, DEFAULT_DOCS_DB_PATH
backend/backup.py:197:    if Path(DEFAULT_DB_PATH).exists():
backend/backup.py:198:        _snapshot_sqlite(Path(DEFAULT_DB_PATH), staging / "family.db")
backend/backup.py:200:    if Path(DEFAULT_DOCS_DB_PATH).exists():
backend/backup.py:201:        _snapshot_sqlite(Path(DEFAULT_DOCS_DB_PATH), staging / "documents.db")
backend/backup.py:385:        conn = _sqlite.connect(str(Path(DEFAULT_DB_PATH)))
backend/paperless_ingest.py:33:from .database import DEFAULT_DOCS_DB_PATH, get_docs_conn, init_docs_db
backend/paperless_ingest.py:50:DOCS_DB_PATH = os.getenv("HOMEOS_DOCS_DB_PATH", DEFAULT_DOCS_DB_PATH)
```

## Direct sqlite3.connect() outside backend/database.py

```
backend/backup.py:180:    conn = sqlite3.connect(str(src))
backend/app_sdk.py:118:    conn = sqlite3.connect(str(base / "data.db"))
backend/app_sdk.py:180:    conn = sqlite3.connect(DEFAULT_DB_PATH)
backend/app_loader.py:301:    conn = sqlite3.connect(str(db_path))
backend/email_autodraft.py:172:        with sqlite3.connect(DEFAULT_DOCS_DB_PATH, timeout=5) as conn:
backend/agent/vanna_shim.py:333:        conn = sqlite3.connect(self.database_path)
```

## Files using  shape (most common pattern)

Total files: 34
