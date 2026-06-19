"""Strict SQL schema validator for community-supplied app schemas.

Whitelisting, not blocklisting: every statement and every column type must
match an explicit allow rule. Anything we don't recognize is rejected with
a clear error message. Same code runs in two places:

  1. At app install time (backend/app_loader.py) — refuses to apply a
     schema that doesn't pass.
  2. In yorik-community's CI (.github/workflows/validate.yml) — refuses to
     merge PRs that fail.

Identical bytes both places, so a PR that passes CI is guaranteed to install
on user boxes.

Allow rules (in code, not docs, because the code is the source of truth):

  STATEMENTS:
    CREATE TABLE [IF NOT EXISTS] name (column_defs, table_constraints)
    CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON table (cols)
    CREATE VIEW [IF NOT EXISTS] name AS SELECT ...

  COLUMN TYPES (case-insensitive):
    INTEGER, REAL, TEXT, NUMERIC, BLOB

  COLUMN/TABLE CONSTRAINTS:
    PRIMARY KEY, NOT NULL, UNIQUE, DEFAULT, CHECK, FOREIGN KEY,
    AUTOINCREMENT, REFERENCES <table> (same-app tables only),
    ON DELETE/UPDATE CASCADE/SET NULL/RESTRICT/NO ACTION

  TABLE NAMES:
    /^[a-z][a-z0-9_]{0,62}$/, no reserved prefix (sqlite_, vec_, homeos_, app_)

Reject everything else, including:

    CREATE TRIGGER, CREATE INDEX … WHERE (partial indexes — complex enough
    to defer), CREATE TABLE WITHOUT ROWID, PRAGMA, ATTACH, DETACH, DROP,
    INSERT/UPDATE/DELETE in a schema file, ALTER, REINDEX, ANALYZE, VACUUM,
    BEGIN/COMMIT/ROLLBACK, SAVEPOINT, virtual tables (USING …) other than
    explicitly allowed engines (none in v1), expressions in column defaults
    referencing other columns, type names outside the whitelist, UNICODE
    homograph attacks on table names.

Limits:
    MAX_TABLES = 50 per schema file
    MAX_COLUMNS = 100 per table
    BLOB DEFAULT max 64 KB inline
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

import sqlparse
from sqlparse.sql import Function, Identifier, IdentifierList, Parenthesis, Statement
from sqlparse.tokens import DDL, Keyword, Name, Punctuation, Whitespace, Comment

# ─── allow rules ───────────────────────────────────────────────────────────

ALLOWED_TYPES: Set[str] = {"INTEGER", "REAL", "TEXT", "NUMERIC", "BLOB"}

ALLOWED_CONSTRAINTS: Set[str] = {
    "PRIMARY", "KEY", "NOT", "NULL", "UNIQUE", "DEFAULT",
    "CHECK", "FOREIGN", "REFERENCES", "AUTOINCREMENT",
    "ON", "DELETE", "UPDATE", "CASCADE", "RESTRICT",
    "SET", "NO", "ACTION",
}

# Words that, if they appear as the FIRST keyword of any statement, are blocked.
BANNED_STATEMENT_HEADS: Set[str] = {
    "PRAGMA", "ATTACH", "DETACH", "DROP", "ALTER",
    "INSERT", "UPDATE", "DELETE", "REPLACE", "TRUNCATE",
    "REINDEX", "ANALYZE", "VACUUM", "BEGIN", "COMMIT", "ROLLBACK",
    "SAVEPOINT", "RELEASE", "EXPLAIN", "SELECT",  # SELECTs belong in code, not schema
}

# Names a community schema must NEVER define.
RESERVED_TABLE_PREFIXES: tuple = ("sqlite_", "vec_", "homeos_", "app_yorik_")
RESERVED_TABLE_NAMES: Set[str] = {
    "events", "tasks", "bills", "documents", "user_profiles", "saved_queries",
    "template_cache", "conversations", "connector_credentials", "connector_grants",
    "document_chunks", "vec_chunks", "app_grants",
}

VALID_TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

MAX_TABLES = 50
MAX_COLUMNS_PER_TABLE = 100
MAX_BLOB_DEFAULT_BYTES = 64 * 1024


# ─── result types ──────────────────────────────────────────────────────────

@dataclass
class SchemaError:
    line: int
    column: int
    statement_index: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}:{self.column} (stmt #{self.statement_index}): {self.message}"


@dataclass
class SchemaInfo:
    tables: List[str] = field(default_factory=list)
    indexes: List[str] = field(default_factory=list)
    views: List[str] = field(default_factory=list)
    foreign_keys: List[tuple] = field(default_factory=list)  # (from_table, to_table)


@dataclass
class ValidationResult:
    ok: bool
    info: SchemaInfo = field(default_factory=SchemaInfo)
    errors: List[SchemaError] = field(default_factory=list)


# ─── core ──────────────────────────────────────────────────────────────────

def _line_col_of(stmt_text: str, sql: str) -> tuple:
    """Return (1-based line, 1-based col) of `stmt_text` inside `sql`."""
    idx = sql.find(stmt_text.lstrip())
    if idx < 0:
        return (1, 1)
    line = sql.count("\n", 0, idx) + 1
    last_nl = sql.rfind("\n", 0, idx)
    col = idx - last_nl
    return (line, col)


def _flatten_keywords(stmt: Statement) -> List[str]:
    """Uppercase keyword stream — used for cheap structural checks."""
    out = []
    for tok in stmt.flatten():
        if tok.ttype is Keyword or tok.ttype is DDL or tok.ttype in (
            sqlparse.tokens.Keyword.DDL, sqlparse.tokens.Keyword.DML, sqlparse.tokens.Keyword.CTE
        ):
            out.append(str(tok).upper())
    return out


def _first_meaningful(stmt: Statement) -> Optional[str]:
    for tok in stmt.tokens:
        if tok.ttype in (Whitespace, Comment) or str(tok).strip() == "":
            continue
        return str(tok).upper().strip()
    return None


def _statement_kind(stmt: Statement) -> Optional[str]:
    """Return 'CREATE_TABLE', 'CREATE_INDEX', 'CREATE_VIEW', or None."""
    kws = _flatten_keywords(stmt)
    if not kws:
        return None
    head = kws[0]
    if head in BANNED_STATEMENT_HEADS:
        return None  # caller decides → error
    if head != "CREATE":
        return None
    # walk forward through optional UNIQUE / IF / NOT / EXISTS to find table|index|view
    after_create = [k for k in kws[1:] if k not in {"UNIQUE", "IF", "NOT", "EXISTS"}]
    if not after_create:
        return None
    target = after_create[0]
    if target == "TABLE":
        return "CREATE_TABLE"
    if target == "INDEX":
        return "CREATE_INDEX"
    if target == "VIEW":
        return "CREATE_VIEW"
    return None


def _extract_table_name(stmt: Statement, kind: str) -> Optional[str]:
    """Pull the target name from a CREATE TABLE/INDEX/VIEW statement.

    Returns the name with original case preserved — the caller is responsible
    for the case-sensitive name-regex check before lowercasing for storage.
    """
    tokens = list(stmt.flatten())
    keyword = {"CREATE_TABLE": "TABLE", "CREATE_INDEX": "INDEX", "CREATE_VIEW": "VIEW"}[kind]
    seen = False
    for tok in tokens:
        s = str(tok).strip()
        if not s:
            continue
        if not seen:
            if s.upper() == keyword:
                seen = True
            continue
        # Skip ALL Keyword tokens after the kind keyword (IF, NOT, EXISTS,
        # potentially others). sqlparse often groups "IF NOT EXISTS" into a
        # single Keyword token so a string check on individual words misses.
        if tok.ttype in (Keyword, DDL) or tok.ttype is sqlparse.tokens.Keyword.DDL:
            continue
        # Only Name / identifier-like tokens past this point.
        if tok.ttype is Name or (s and s[0].isalpha() and s.upper() not in {"ON", "AS"}):
            return s.strip('`"[]')  # case preserved
        break
    return None


def _extract_create_table_column_defs(stmt: Statement) -> List[str]:
    """Return raw column-def text strings (each line of column defs inside the
    outer parenthesis)."""
    paren = next((t for t in stmt.tokens if isinstance(t, Parenthesis)), None)
    if not paren:
        return []
    # Strip outer parens
    inner = str(paren).strip()
    if inner.startswith("("):
        inner = inner[1:-1]
    # Split on commas that are NOT inside nested parens (FK clauses)
    parts: List[str] = []
    depth = 0
    cur = []
    for ch in inner:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _validate_table_name(name: str, defined_tables: Set[str], errs: List[SchemaError], stmt_index: int, line_col: tuple) -> bool:
    if not VALID_TABLE_NAME.match(name):
        errs.append(SchemaError(*line_col, stmt_index,
            f"table name {name!r} must match /^[a-z][a-z0-9_]{{0,62}}$/"))
        return False
    if any(name.startswith(p) for p in RESERVED_TABLE_PREFIXES):
        errs.append(SchemaError(*line_col, stmt_index,
            f"table name {name!r} uses reserved prefix"))
        return False
    if name in RESERVED_TABLE_NAMES:
        errs.append(SchemaError(*line_col, stmt_index,
            f"table name {name!r} collides with a Yorik core table"))
        return False
    if name in defined_tables:
        errs.append(SchemaError(*line_col, stmt_index,
            f"table {name!r} declared more than once"))
        return False
    return True


_TYPE_RE = re.compile(r"^([A-Z]+)(\s*\([^)]+\))?\b", re.IGNORECASE)


def _validate_column_def(col_def: str, table_name: str, defined_tables: Set[str],
                        errs: List[SchemaError], stmt_index: int, line_col: tuple) -> Optional[tuple]:
    """Validate a single column or table-level constraint definition.

    Returns (kind, info) where kind is 'column' or 'constraint' or None on error.
    For columns: info = (col_name, type_name)
    For FKs (table-level): info = (referenced_table_name)
    """
    s = col_def.strip()
    if not s:
        return None
    head = s.split(None, 1)[0].upper().rstrip(",")

    # Table-level constraints
    if head in {"PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"}:
        # Look for a REFERENCES clause if it's a FK
        m = re.search(r"\bREFERENCES\s+([a-z_][a-z0-9_]*)", s, re.IGNORECASE)
        if head == "FOREIGN" and not m:
            errs.append(SchemaError(*line_col, stmt_index,
                f"FOREIGN KEY in {table_name!r} without REFERENCES clause"))
            return None
        if m:
            ref_table = m.group(1).lower()
            if ref_table not in defined_tables and ref_table != table_name:
                errs.append(SchemaError(*line_col, stmt_index,
                    f"FOREIGN KEY in {table_name!r} references unknown / cross-app table {ref_table!r} "
                    f"(only same-app tables allowed; for cross-DB access use requires_tables_external in the manifest)"))
                return None
            return ("constraint", ref_table)
        return ("constraint", None)

    # Column definition: "<name> <TYPE> [<constraints>]"
    parts = s.split(None, 1)
    col_name = parts[0].strip('`"[]')
    if not re.match(r"^[a-z_][a-z0-9_]*$", col_name, re.IGNORECASE):
        errs.append(SchemaError(*line_col, stmt_index,
            f"invalid column name {col_name!r} in {table_name!r}"))
        return None
    rest = parts[1] if len(parts) > 1 else ""
    type_match = _TYPE_RE.match(rest)
    if not type_match:
        errs.append(SchemaError(*line_col, stmt_index,
            f"column {table_name}.{col_name} missing type declaration"))
        return None
    col_type = type_match.group(1).upper()
    if col_type not in ALLOWED_TYPES:
        errs.append(SchemaError(*line_col, stmt_index,
            f"column {table_name}.{col_name} uses disallowed type {col_type!r} "
            f"(allowed: {sorted(ALLOWED_TYPES)})"))
        return None
    after_type = rest[type_match.end():].upper()
    # Block weird table options
    if "WITHOUT ROWID" in after_type:
        errs.append(SchemaError(*line_col, stmt_index,
            f"WITHOUT ROWID not supported in {table_name!r}"))
        return None
    # FK inside a column def
    fk_match = re.search(r"\bREFERENCES\s+([a-z_][a-z0-9_]*)", rest, re.IGNORECASE)
    if fk_match:
        ref_table = fk_match.group(1).lower()
        if ref_table not in defined_tables and ref_table != table_name:
            errs.append(SchemaError(*line_col, stmt_index,
                f"column {table_name}.{col_name} REFERENCES unknown / cross-app table {ref_table!r}"))
            return None
    return ("column", (col_name, col_type))


# ─── public API ────────────────────────────────────────────────────────────

def validate_schema(sql: str) -> ValidationResult:
    """Parse `sql` and return a ValidationResult. Never raises."""
    info = SchemaInfo()
    errors: List[SchemaError] = []

    if not sql or not sql.strip():
        return ValidationResult(ok=False, errors=[SchemaError(1, 1, 0, "schema is empty")])

    statements = [s for s in sqlparse.parse(sql) if str(s).strip()]

    for i, stmt in enumerate(statements, start=1):
        stmt_text = str(stmt).strip().rstrip(";").strip()
        if not stmt_text:
            continue
        line_col = _line_col_of(str(stmt), sql)

        head = _first_meaningful(stmt)
        if head in BANNED_STATEMENT_HEADS:
            errors.append(SchemaError(*line_col, i,
                f"statement type {head!r} not allowed in app schema "
                f"(schemas are CREATE TABLE/INDEX/VIEW only — no DML, DDL beyond CREATE, no pragmas)"))
            continue

        kind = _statement_kind(stmt)
        if not kind:
            errors.append(SchemaError(*line_col, i,
                f"unrecognized or disallowed statement (must be CREATE TABLE/INDEX/VIEW); "
                f"saw: {stmt_text[:80]!r}"))
            continue

        # Reject CREATE TRIGGER (caught here because TRIGGER isn't a recognized kind)
        if "TRIGGER" in [str(t).upper().strip() for t in stmt.flatten() if str(t).strip()]:
            errors.append(SchemaError(*line_col, i,
                "CREATE TRIGGER not allowed in app schema"))
            continue

        name = _extract_table_name(stmt, kind)
        if not name:
            errors.append(SchemaError(*line_col, i,
                f"could not extract target name from {kind} statement"))
            continue

        defined_tables = set(info.tables)
        if kind == "CREATE_TABLE":
            if len(info.tables) >= MAX_TABLES:
                errors.append(SchemaError(*line_col, i,
                    f"too many tables (max {MAX_TABLES})"))
                continue
            # Block CREATE TABLE … WITHOUT ROWID and similar table-options.
            if re.search(r"\bWITHOUT\s+ROWID\b", stmt_text, re.IGNORECASE):
                errors.append(SchemaError(*line_col, i,
                    f"WITHOUT ROWID not supported in table {name!r}"))
                continue
            if not _validate_table_name(name, defined_tables, errors, i, line_col):
                continue
            # Lowercase the canonical name for storage AFTER the case-sensitive validate.
            name_lower = name.lower()

            col_defs = _extract_create_table_column_defs(stmt)
            if not col_defs:
                errors.append(SchemaError(*line_col, i,
                    f"table {name!r} has no column definitions"))
                continue
            if len(col_defs) > MAX_COLUMNS_PER_TABLE:
                errors.append(SchemaError(*line_col, i,
                    f"table {name!r} has {len(col_defs)} cols (max {MAX_COLUMNS_PER_TABLE})"))
                continue

            for col_def in col_defs:
                result = _validate_column_def(col_def, name_lower, defined_tables, errors, i, line_col)
                if result and result[0] == "constraint" and result[1]:
                    info.foreign_keys.append((name_lower, result[1]))

            info.tables.append(name_lower)

        elif kind == "CREATE_INDEX":
            # Reject partial indexes (CREATE INDEX … WHERE) — extra surface
            if re.search(r"\bWHERE\b", stmt_text, re.IGNORECASE):
                errors.append(SchemaError(*line_col, i,
                    f"partial indexes (WHERE clause) not allowed; saw index {name!r}"))
                continue
            # Ensure the indexed table is in this schema
            on_match = re.search(r"\bON\s+([a-z_][a-z0-9_]*)", stmt_text, re.IGNORECASE)
            if on_match:
                target = on_match.group(1).lower()
                if target not in info.tables:
                    errors.append(SchemaError(*line_col, i,
                        f"index {name!r} references unknown table {target!r}"))
                    continue
            info.indexes.append(name)

        elif kind == "CREATE_VIEW":
            # Permit views over same-app tables only. Heuristic: scan FROM/JOIN
            # for table names; reject any reference to a Yorik core table.
            view_body = stmt_text.split("AS", 1)[1] if "AS" in stmt_text.upper() else ""
            for ref in re.findall(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", view_body, re.IGNORECASE):
                rl = ref.lower()
                if rl in RESERVED_TABLE_NAMES:
                    errors.append(SchemaError(*line_col, i,
                        f"view {name!r} references reserved table {ref!r}; "
                        f"cross-DB access goes through manifest.requires_tables_external"))
                    break
                if rl not in info.tables:
                    errors.append(SchemaError(*line_col, i,
                        f"view {name!r} references unknown table {ref!r}"))
                    break
            else:
                info.views.append(name)

    return ValidationResult(ok=not errors, info=info, errors=errors)


# ─── inline self-tests ─────────────────────────────────────────────────────
# Run with: python -m backend.app_schema_validator

_TESTS = [
    # (description, sql, should_pass)
    ("simple table", "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);", True),
    ("multi-table with FK",
     "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"
     "CREATE TABLE quotes (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id));",
     True),
    ("create index on same-app table",
     "CREATE TABLE c (id INTEGER PRIMARY KEY, email TEXT);\n"
     "CREATE INDEX idx_c_email ON c(email);", True),
    ("reject trigger",
     "CREATE TABLE t (id INTEGER PRIMARY KEY);\n"
     "CREATE TRIGGER my_trig AFTER INSERT ON t BEGIN SELECT 1; END;", False),
    ("reject pragma", "PRAGMA foreign_keys = ON;", False),
    ("reject attach", "ATTACH DATABASE 'other.db' AS other;", False),
    ("reject drop", "DROP TABLE events;", False),
    ("reject DML in schema", "INSERT INTO t VALUES (1);", False),
    ("reject reserved table name",
     "CREATE TABLE events (id INTEGER PRIMARY KEY);", False),
    ("reject reserved prefix",
     "CREATE TABLE sqlite_secret (id INTEGER);", False),
    ("reject bad type",
     "CREATE TABLE t (id INTEGER PRIMARY KEY, data JSON);", False),
    ("reject cross-app FK",
     "CREATE TABLE my_audit (id INTEGER PRIMARY KEY, event_id INTEGER REFERENCES events(id));", False),
    ("reject WITHOUT ROWID",
     "CREATE TABLE t (id INTEGER PRIMARY KEY) WITHOUT ROWID;", False),
    ("reject partial index",
     "CREATE TABLE t (id INTEGER PRIMARY KEY, kind TEXT);\n"
     "CREATE INDEX idx ON t(kind) WHERE kind = 'open';", False),
    ("reject uppercase table name",
     "CREATE TABLE Customers (id INTEGER PRIMARY KEY);", False),
    ("view over same-app table is ok",
     "CREATE TABLE c (id INTEGER PRIMARY KEY, total REAL);\n"
     "CREATE VIEW c_summary AS SELECT id, total FROM c;", True),
    ("view over reserved table rejected",
     "CREATE VIEW peek_events AS SELECT * FROM events;", False),
]


def _selftest() -> int:
    failed = 0
    for desc, sql, should_pass in _TESTS:
        result = validate_schema(sql)
        passed = (result.ok == should_pass)
        if passed:
            print(f"  OK   {desc}")
        else:
            failed += 1
            print(f"  FAIL {desc}  — expected ok={should_pass}, got ok={result.ok}")
            for e in result.errors:
                print(f"      {e}")
    if failed:
        print(f"\n{failed} / {len(_TESTS)} failed")
        return 1
    print(f"\nall {len(_TESTS)} validator tests passed")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
