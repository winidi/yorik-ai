"""DDL block — the chat agent must never run schema-mutating SQL.

Pins the _is_ddl detector in backend/vanna_agent.py. The runner uses it
to reject CREATE/ALTER/DROP/etc. before any role filter or transaction
is touched. Schema changes belong to the migrations framework + the app
SDK, both of which leave an audit trail. Letting the LLM run DDL leads
to schema sprawl and creates a prompt-injection surface.

If anyone refactors the runner and accidentally drops the DDL check,
these tests fail loudly. Test the helper directly (no Vanna spin-up
needed) since the runner just forwards to it.
"""

from __future__ import annotations

import pytest

from backend.ask import _is_ddl


# Inputs that MUST be flagged as DDL.
DDL_CASES = [
    "CREATE TABLE foo (id INTEGER)",
    "create table foo (id integer)",
    "  CREATE   TABLE  foo  (id INTEGER)  ",   # leading/internal whitespace
    "CREATE TEMP TABLE t (id INTEGER)",
    "CREATE TEMPORARY TABLE t (id INTEGER)",
    "CREATE VIEW v AS SELECT 1",
    "CREATE INDEX idx ON foo(id)",
    "CREATE OR REPLACE VIEW v AS SELECT 1",
    "ALTER TABLE foo ADD COLUMN bar TEXT",
    "DROP TABLE foo",
    "DROP TABLE IF EXISTS foo",
    "DROP INDEX idx",
    "TRUNCATE foo",                            # not real SQLite but block anyway
    "ALTER TABLE foo RENAME TO bar",
    "RENAME TABLE foo TO bar",                 # MySQL syntax — block in case
    "ATTACH DATABASE 'evil.db' AS evil",       # exfil vector
    "DETACH DATABASE evil",
    "REINDEX foo",
    "VACUUM",
    "/* prompt-injection */ CREATE TABLE evil (x INTEGER)",
    "-- innocent looking comment\nCREATE TABLE evil (x INTEGER)",
    "/* multi */ /* leading */ DROP TABLE bills",
]

# Inputs that MUST NOT be flagged (the agent's legitimate SELECT/INSERT/etc.).
NOT_DDL_CASES = [
    "SELECT * FROM bills",
    "SELECT 'CREATE TABLE foo' AS x",          # DDL word inside a string literal
    "INSERT INTO events (title) VALUES ('Create a backup table')",
    "UPDATE tasks SET title = 'drop the laundry' WHERE id = 1",
    "DELETE FROM tasks WHERE id = 1",
    "REPLACE INTO bills (id, name) VALUES (1, 'Strom')",   # row-level upsert
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "",
    "   ",
    "  -- just a comment\n",
]


@pytest.mark.parametrize("sql", DDL_CASES)
def test_blocks_ddl(sql: str) -> None:
    assert _is_ddl(sql) is True, f"DDL slipped through: {sql!r}"


@pytest.mark.parametrize("sql", NOT_DDL_CASES)
def test_passes_non_ddl(sql: str) -> None:
    assert _is_ddl(sql) is False, f"non-DDL incorrectly blocked: {sql!r}"
