"""Phase D Section 3 — thin SQLite ↔ Postgres dialect adapter.

Yorik's backend was written sqlite3-style: `?` placeholders, dict-keyed
rows, `cur.lastrowid` after INSERTs, occasional `INSERT OR IGNORE` and
`datetime('now')`. This module lets the same call sites run against
both sqlite3 and psycopg by translating the SQL on the way through.

How it's wired:
  - `backend/database.py:conn_ctx()` checks `YORIK_DB_BACKEND`. If
    `postgres`, it wraps a psycopg connection in `PgConnection`.
  - `PgConnection` exposes the bits of sqlite3.Connection callers use:
    `.execute()`, `.executemany()`, `.commit()`, `.rollback()`,
    `.close()`, `.cursor()`.
  - `PgConnection.execute()` calls `translate_sql()` first, then runs
    via psycopg. Returns a `PgCursor` so callers can keep calling
    `.fetchone()` / `.fetchall()` / `.lastrowid` etc.
  - `PgCursor.lastrowid` is populated by auto-injected `RETURNING id`
    on INSERTs against tables that have an `id` column.

Translation scope (per the quirks catalogue in notes/phase-d/):
  - `?`               → `%s` (token-aware: skip inside string literals).
  - `datetime('now')` → `current_timestamp`
  - `date('now')`     → `current_date`
  - `IFNULL(`         → `COALESCE(`
  - `INSTR(a, b)`     → `position(b in a)` (positional)
  - `INSERT OR IGNORE`   → `INSERT … ON CONFLICT DO NOTHING`
  - `INSERT OR REPLACE`  → not auto-translated; raises with a clear
                            message so the caller updates the SQL.

Out of scope (for now):
  - DDL beyond what migrations already translate. Backend code rarely
    emits DDL at runtime; the few PRAGMA statements get filtered out.
  - SQLite triggers / virtual tables — handled at migration time.

This module is the only piece that needs to evolve when a backend
module surfaces a new SQLite quirk; the call sites stay untouched.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Iterable, Optional


# ─── Hybrid row (dict + positional indexing) ────────────────────────


class HybridRow(dict):
    """Row object that supports BOTH `row['col']` (psycopg dict_row
    style) AND `row[0]` (sqlite3.Row positional style).

    Yorik mixes both access styles across ~16 call sites. Without this
    hybrid, switching to psycopg's dict_row factory broke every
    `row[0]` site with a KeyError. sqlite3.Row supported both styles
    transparently; we restore that surface here so the shim is fully
    transparent."""

    __slots__ = ("_columns",)

    def __init__(self, mapping: dict) -> None:
        super().__init__(mapping)
        # Preserve column order for positional access. Python dicts
        # since 3.7 preserve insertion order, so the columns list
        # matches the SELECT order.
        self._columns = list(mapping.keys())

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, int):
            return super().__getitem__(self._columns[key])
        return super().__getitem__(key)

    def keys(self):  # type: ignore[override]
        return self._columns

    def __iter__(self):
        # Iterate over values in column order, like a tuple-row would.
        # Beware: this conflicts with a vanilla dict's "iterate over
        # keys" behaviour, but sqlite3.Row also iterates over values.
        # Yorik's call sites that do `for col in row:` expect values.
        for c in self._columns:
            yield super().__getitem__(c)


def _hybrid_row_factory(cursor):
    """psycopg row factory — call signature is
    `factory(cursor) -> callable(values) -> row`. Returns a callable
    that builds a HybridRow from the cursor's column metadata."""
    columns = [d.name for d in (cursor.description or [])]
    def make(values):
        return HybridRow(dict(zip(columns, values)))
    return make


# ─── SQL translation ────────────────────────────────────────────────


_RETURNING_TABLES_WITH_ID: set[str] | None = None  # populated lazily
_PK_COLUMNS: dict[str, list[str]] | None = None     # table → PK column names
_RETURNING_TABLES_LOCK = threading.Lock()


def _populate_id_tables(pg_conn) -> None:
    """One-shot probe of: (a) which tables have an `id` column (for
    auto-RETURNING) and (b) each table's primary-key columns (for
    `INSERT OR REPLACE` upsert translation). Cached for the lifetime
    of the process.

    Both queries cover the `public` + `docs` schemas; map keys are
    bare table names since the INSERT statement usually isn't
    schema-qualified."""
    global _RETURNING_TABLES_WITH_ID, _PK_COLUMNS
    if _RETURNING_TABLES_WITH_ID is not None and _PK_COLUMNS is not None:
        return
    with _RETURNING_TABLES_LOCK:
        if _RETURNING_TABLES_WITH_ID is not None and _PK_COLUMNS is not None:
            return
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema IN ('public', 'docs') AND column_name = 'id'"
            )
            _RETURNING_TABLES_WITH_ID = {r[0] if isinstance(r, tuple) else r["table_name"]
                                          for r in cur.fetchall()}
            # Per-table PK columns. information_schema doesn't expose
            # this directly; key_column_usage joined with
            # table_constraints does.
            cur.execute(
                "SELECT kcu.table_name, kcu.column_name, kcu.ordinal_position "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON kcu.constraint_name = tc.constraint_name "
                " AND kcu.table_schema   = tc.table_schema "
                "WHERE tc.constraint_type = 'PRIMARY KEY' "
                "  AND tc.table_schema IN ('public', 'docs') "
                "ORDER BY kcu.table_name, kcu.ordinal_position"
            )
            pks: dict[str, list[str]] = {}
            for row in cur.fetchall():
                t = row["table_name"] if isinstance(row, dict) else row[0]
                c = row["column_name"] if isinstance(row, dict) else row[1]
                pks.setdefault(t, []).append(c)
            _PK_COLUMNS = pks


_INSERT_TABLE_RE = re.compile(
    r"^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(?:[\"'`]?)([\w]+)(?:[\"'`]?)",
    re.IGNORECASE,
)


def _insert_target_table(sql: str) -> Optional[str]:
    """Bare table name an INSERT writes to, lowercased. None if not an
    INSERT or it's a non-trivial form we don't try to parse."""
    m = _INSERT_TABLE_RE.match(sql)
    return m.group(1).lower() if m else None


def _replace_placeholders(sql: str) -> str:
    """Translate `?` to `%s`, escaping literal `%` characters so psycopg
    doesn't misread them as placeholders.

    The Postgres adapter (psycopg) requires `%s` for parameters; SQLite
    uses `?`. psycopg3 also scans INSIDE string literals for `%` looking
    for placeholders — so a `LIKE '%\\Trash%'` clause from IMAP-style
    code crashed with `only '%s', '%b', '%t' are allowed as placeholders,
    got '%\\'`. We escape every bare `%` (inside OR outside strings) to
    `%%`. SQL LIKE doesn't distinguish `%%` from `%` semantically
    (both match "any sequence of characters"), and psycopg unescapes
    `%%` back to `%` on the wire, so the behaviour is identical.

    Exception: `%s` is psycopg's placeholder syntax. Some code paths
    (paperless_ingest's pgvector search, postgres-native helpers) pass
    SQL with `%s` directly because they're postgres-only and never
    needed the SQLite-style `?` translation. Doubling those to `%%s`
    would produce a literal `%s` and psycopg would raise "the query
    has 0 placeholders". Leave `%s` (and `%(name)s`) alone.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    in_single = in_double = False
    while i < n:
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
        elif ch == "?" and not in_single and not in_double:
            out.append("%s")
        elif ch == "%":
            nxt = sql[i + 1] if i + 1 < n else ""
            if nxt == "s" or nxt == "(":
                out.append(ch)
            else:
                out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_SUBSTITUTIONS = [
    # (regex, replacement) applied in order. Each must be safe to run
    # over Postgres SQL too (idempotent — won't double-translate).
    #
    # IMPORTANT: SQLite's `datetime('now')` returns the string
    # `'YYYY-MM-DD HH:MM:SS'` (space separator, no timezone). All our
    # historical data is stored in TEXT columns using this format. We
    # use `to_char(now(), 'YYYY-MM-DD HH24:MI:SS')` to emit the same
    # shape on the Postgres side so comparisons like
    # `created_at <= datetime('now')` keep working over the imported
    # text data without explicit casts at every call site.
    (re.compile(r"\bdatetime\s*\(\s*'now'\s*\)", re.IGNORECASE),
     "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')"),
    (re.compile(r"\bdate\s*\(\s*'now'\s*\)", re.IGNORECASE),
     "to_char(now(), 'YYYY-MM-DD')"),
    # SQLite-style time offset: `datetime('now', '-90 seconds')` /
    # `datetime('now', '+5 minutes')` / `datetime('now', '-1 hours')`.
    # Postgres has no interval-string overload to datetime(); rewrite to
    # `to_char(now() ± interval 'N units', …)`. Preserves the text-format
    # output the SQLite path emitted, so comparisons against text columns
    # still work without explicit casts.
    (re.compile(
        r"\bdatetime\s*\(\s*'now'\s*,\s*'([+-])\s*(\d+)\s+(seconds?|minutes?|hours?|days?)'\s*\)",
        re.IGNORECASE,
     ),
     lambda m: (
        "to_char(now() {op} interval '{n} {unit}', "
        "'YYYY-MM-DD HH24:MI:SS')"
     ).format(op=m.group(1), n=m.group(2), unit=m.group(3).lower())),
    (re.compile(r"\bIFNULL\s*\(", re.IGNORECASE),                "COALESCE("),
    # INSTR(a, b) → position(b in a). Positional swap with a regex
    # works for the simple cases Yorik uses; if a future call passes
    # expressions with embedded commas, the call site has to use
    # position(...) directly.
    (re.compile(r"\bINSTR\s*\(\s*([^,()]+(?:\([^()]*\))?)\s*,\s*([^)]+)\)", re.IGNORECASE),
     r"position(\2 in \1)"),
]


def _translate_insert_or_ignore(sql: str) -> str:
    """Convert `INSERT OR IGNORE INTO …` to `INSERT INTO … ON CONFLICT
    DO NOTHING`. The conflict target defaults to the implicit unique
    constraint (PG picks all unique indexes); for our use that's the
    primary key, which is what callers want."""
    pattern = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE)
    if not pattern.search(sql):
        return sql
    sql = pattern.sub("INSERT", sql)
    # Append ON CONFLICT DO NOTHING before any RETURNING (we may add
    # RETURNING later) and before any trailing semicolon.
    sql = sql.rstrip().rstrip(";")
    if not re.search(r"\bON\s+CONFLICT\b", sql, re.IGNORECASE):
        sql += " ON CONFLICT DO NOTHING"
    return sql


_INSERT_OR_REPLACE_RE = re.compile(
    r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(?:[\"']?)([\w]+)(?:[\"']?)"
    r"\s*\(\s*([^)]+)\s*\)\s*VALUES",
    re.IGNORECASE,
)


def _translate_insert_or_replace(sql: str) -> str:
    """`INSERT OR REPLACE INTO t (cols…) VALUES (…)` →
       `INSERT INTO t (cols…) VALUES (…)
        ON CONFLICT (pk_cols…) DO UPDATE SET col=EXCLUDED.col …`

    Conflict target is read from the cached PK columns populated by
    `_populate_id_tables()`. The DO UPDATE SET clause assigns every
    non-PK column to its `EXCLUDED.<col>` counterpart so the upsert
    behaves the same as SQLite's OR REPLACE.

    Falls through with a ValueError when the PK isn't known (table
    not in cache) or the SQL shape doesn't match the simple
    `INSERT OR REPLACE INTO t (cols) VALUES (…)` form."""
    m = _INSERT_OR_REPLACE_RE.match(sql)
    if not m:
        if re.search(r"\bINSERT\s+OR\s+REPLACE\b", sql, re.IGNORECASE):
            raise ValueError(
                "Phase D shim: `INSERT OR REPLACE` with an unusual shape "
                "(missing column list, multi-row VALUES, SELECT subquery?). "
                "Rewrite the call site to explicit `ON CONFLICT (col) "
                "DO UPDATE SET …`. SQL was:\n" + sql[:300]
            )
        return sql
    table = m.group(1).lower()
    columns = [c.strip().strip('"').strip("'") for c in m.group(2).split(",")]
    if _PK_COLUMNS is None or table not in _PK_COLUMNS:
        raise ValueError(
            f"Phase D shim: `INSERT OR REPLACE INTO {table}` — no primary key "
            f"known for that table (cache miss). Rewrite the call site "
            f"to explicit `ON CONFLICT (col) DO UPDATE SET …`."
        )
    pk_cols = _PK_COLUMNS[table]
    # Drop OR REPLACE, append the conflict clause. The DO UPDATE SET
    # only touches non-PK columns; setting the PK to EXCLUDED.pk would
    # be redundant (it's identical) and Postgres rejects it as a
    # mutation of the conflict target.
    non_pk_cols = [c for c in columns if c not in pk_cols]
    sql = re.sub(r"\bINSERT\s+OR\s+REPLACE\b", "INSERT",
                 sql, count=1, flags=re.IGNORECASE)
    sql = sql.rstrip().rstrip(";")
    target = ", ".join(f'"{c}"' for c in pk_cols)
    if non_pk_cols:
        set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in non_pk_cols)
        sql += f" ON CONFLICT ({target}) DO UPDATE SET {set_clause}"
    else:
        # All columns are PK → nothing meaningful to update on conflict.
        sql += f" ON CONFLICT ({target}) DO NOTHING"
    return sql


def translate_sql(sql: str) -> str:
    """Run every substitution. Cheap; called per-execute(). Idempotent —
    Postgres SQL passing through unchanged is safe."""
    sql = _replace_placeholders(sql)
    for pat, rep in _SUBSTITUTIONS:
        sql = pat.sub(rep, sql)
    sql = _translate_insert_or_replace(sql)
    sql = _translate_insert_or_ignore(sql)
    return sql


def _should_add_returning_id(sql: str) -> Optional[str]:
    """If `sql` is a single-row INSERT into a table whose `id` column
    exists, return the bare table name. Else None.

    Caller-side rule: only single-statement INSERTs without an existing
    RETURNING clause are auto-augmented. Multi-statement scripts /
    INSERTs with bespoke RETURNING / INSERTs into id-less tables all
    bypass this."""
    if _RETURNING_TABLES_WITH_ID is None:
        return None
    if "returning" in sql.lower():
        return None
    table = _insert_target_table(sql)
    if table and table in _RETURNING_TABLES_WITH_ID:
        return table
    return None


# ─── Connection / cursor wrappers ───────────────────────────────────


class PgCursor:
    """sqlite3.Cursor-style facade over a psycopg cursor."""

    __slots__ = ("_cur", "_lastrowid")

    def __init__(self, cur, lastrowid: Optional[int] = None) -> None:
        self._cur = cur
        self._lastrowid = lastrowid

    @property
    def lastrowid(self) -> Optional[int]:
        return self._lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, n: int = 1):
        return self._cur.fetchmany(n)

    def __iter__(self):
        return iter(self._cur)

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:  # noqa: BLE001
            pass


class PgConnection:
    """sqlite3.Connection-style facade over a psycopg connection.

    `pool` (optional): when set, `close()` / `__exit__` return the
    underlying psycopg connection to the pool. Used by `get_conn()`
    callers that do `with get_conn() as c:` — they expect the close to
    free the resource. Without the putconn the pool exhausts under
    background load (paperless reconciler, drift detector, etc.)."""

    def __init__(self, pg_conn, pool=None) -> None:
        self._conn = pg_conn
        self._pool = pool
        # Populate the id-table cache the first time we see a connection.
        _populate_id_tables(pg_conn)

    # ---- DB-API surface callers use ----

    def execute(self, sql: str, params: Iterable[Any] = ()) -> PgCursor:
        sql_pg = translate_sql(sql)
        target = _should_add_returning_id(sql_pg)
        if target:
            sql_pg = sql_pg.rstrip().rstrip(";") + " RETURNING id"
        cur = self._conn.cursor()
        cur.execute(sql_pg, tuple(params) if params else None)
        last_id: Optional[int] = None
        if target:
            try:
                row = cur.fetchone()
                if row is not None:
                    # row may be a dict (dict_row factory) or tuple
                    if isinstance(row, dict):
                        last_id = int(row.get("id")) if row.get("id") is not None else None
                    else:
                        last_id = int(row[0])
            except Exception:  # noqa: BLE001
                # The INSERT may have hit ON CONFLICT DO NOTHING and
                # written zero rows; fetchone() returns None then.
                last_id = None
        return PgCursor(cur, last_id)

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> PgCursor:
        sql_pg = translate_sql(sql)
        cur = self._conn.cursor()
        cur.executemany(sql_pg, [tuple(p) for p in seq_of_params])
        return PgCursor(cur)

    def cursor(self) -> "PgCursor":
        return PgCursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        # If we own a pool reference (constructed by get_conn), putconn
        # the underlying psycopg connection back. Without this, every
        # `with get_conn() as c:` exit would leak a pool slot — drains
        # to PoolTimeout after max_size iterations.
        if self._pool is not None and self._conn is not None:
            try:
                self._pool.putconn(self._conn)
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
            self._pool = None

    # ---- sqlite3-isms we silently no-op so call sites don't have to care ----

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, _value) -> None:
        # psycopg row factory is set on the pool — late changes ignored.
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            # Always return the connection to the pool on context exit.
            # Required so `with get_conn() as c:` doesn't drain the pool.
            self.close()
        return False
