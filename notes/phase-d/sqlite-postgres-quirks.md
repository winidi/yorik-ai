# SQLite ↔ Postgres translation quirks — Phase D

Reference catalogue I consult when translating any module's SQL.

## 1. Date/time

| SQLite | Postgres |
|--------|----------|
| `datetime('now')` | `now()` or `current_timestamp` |
| `date('now')` | `current_date` |
| `strftime('%Y-%m-%d', col)` | `to_char(col, 'YYYY-MM-DD')` |

## 2. Upsert / conflict

| SQLite | Postgres |
|--------|----------|
| `INSERT OR IGNORE INTO t …` | `INSERT INTO t … ON CONFLICT DO NOTHING` |
| `INSERT OR REPLACE INTO t …` | `INSERT INTO t … ON CONFLICT (col) DO UPDATE SET …` |

Caveat: `ON CONFLICT` needs a unique constraint or index on the conflict column.

## 3. Identity columns

| SQLite | Postgres |
|--------|----------|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL PRIMARY KEY` or `GENERATED ALWAYS AS IDENTITY` |

Pick `BIGSERIAL` for new code (simpler), `GENERATED ALWAYS AS IDENTITY` if importing existing values (lets us preserve IDs).

## 4. String functions

| SQLite | Postgres |
|--------|----------|
| `INSTR(haystack, needle)` | `position(needle in haystack)` or `strpos(haystack, needle)` |
| `IFNULL(x, y)` | `COALESCE(x, y)` |
| `SUBSTR(s, i, n)` | same syntax works, also `substring(s from i for n)` |
| `LOWER` / `UPPER` | same |
| `LIKE` (case-insensitive by default for ASCII) | `ILIKE` for case-insensitive |

**Trap**: places in our code with naked `LIKE` need a Postgres review — switch to `ILIKE` or wrap both sides with `lower()`.

## 5. Booleans

SQLite stores 0/1; Postgres has real `boolean`. Recommendation: translate columns to `boolean` in migrations_pg, and confirm Python code uses `True/False` not `1/0` at insert sites. Most code already does.

## 6. Vector search

4 backend modules touch sqlite_vec:
- backend/paperless_ingest.py (paperless_vec)
- backend/whatsapp_semantic.py (wa_message_vec)
- backend/documents.py (probably native_documents vec — confirm)
- backend/database.py (loader)

Translation:
- `CREATE VIRTUAL TABLE foo_vec USING vec0(embedding float[768])` → drop the virtual table, add `embedding vector(384)` column on the parent table
- `WHERE embedding MATCH ? ORDER BY distance` → `ORDER BY embedding <=> $1::vector LIMIT $2`
- `sqlite_vec.serialize_float32(vec)` → pass a Python list `vec` directly; psycopg + pgvector-python register it
- Index: `CREATE INDEX ON paperless_chunks USING hnsw (embedding vector_cosine_ops);` (or ivfflat if HNSW is too slow on first build)

## 7. FTS

Grep shows no live FTS5 `USING fts5` virtual tables in the current backend — the Paperless FTS uses Paperless's own API, not a local FTS5 table. So no Postgres tsvector work needed.

## 8. Connection / transaction shape

- SQLite: `conn = sqlite3.connect(path)` — file handle, autocommit-unless-in-transaction
- Postgres: `conn = psycopg.connect(url)` — implicit transaction, `commit()` required to persist

Plan: `conn_ctx_pg` wraps psycopg so `__exit__` commits on success, rolls back on exception. Matches sqlite3 behaviour.

## 9. Row access (`row["col"]`)

SQLite: `conn.row_factory = sqlite3.Row` → row supports key access.
Postgres (psycopg v3): pool config with `row_factory=dict_row` → row is a plain `dict`.

Code that does `row["col"]` works on both. Code that does `row.col` (attribute access) doesn't — but Yorik consistently uses subscript so this is fine.

## 10. `lastrowid`

- SQLite: `cursor.lastrowid`
- Postgres: `INSERT … RETURNING id` then `cursor.fetchone()["id"]`

Plan: dispatch helper `last_insert_id(cursor, sql, params)` that does the right thing per backend, so the call sites read cleanly.

## 11. Schema introspection

SQLite: `PRAGMA table_info(t)`.
Postgres: `SELECT column_name FROM information_schema.columns WHERE table_name = 't'`.

A few migrations use PRAGMA for idempotency probes — those need translation.
