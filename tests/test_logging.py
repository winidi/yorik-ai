"""Unit tests for backend.logging_setup (SecretsFilter + JsonFormatter)
and backend.error_log (SQLite handler + read accessors).

Pure unit tests against the logging primitives — no FastAPI app
needed for the filter/formatter ones. The error_log handler tests
use a tmp DB via the existing fresh_app fixture so they don't
pollute the maintainer's real data.
"""

from __future__ import annotations

import json
import logging
import pytest


# ─── SecretsFilter ────────────────────────────────────────────────────

@pytest.fixture
def secrets_filter():
    from backend.logging_setup import SecretsFilter
    return SecretsFilter()


def _make_record(msg: str, args=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname="t.py", lineno=1,
        msg=msg, args=args, exc_info=None,
    )


def test_secrets_filter_redacts_api_key(secrets_filter):
    rec = _make_record("config: api_key=sk-1234567890abcdef")
    secrets_filter.filter(rec)
    assert "sk-1234567890abcdef" not in rec.msg
    assert "REDACTED" in rec.msg


def test_secrets_filter_redacts_bearer_token(secrets_filter):
    rec = _make_record("Authorization: Bearer abc123xyz789longenough")
    secrets_filter.filter(rec)
    assert "abc123xyz789longenough" not in rec.msg
    assert "<REDACTED>" in rec.msg


def test_secrets_filter_redacts_password_in_kv(secrets_filter):
    rec = _make_record('Login attempt: email=foo@bar.com password="supersecret123"')
    secrets_filter.filter(rec)
    assert "supersecret123" not in rec.msg


def test_secrets_filter_redacts_bcrypt_hash(secrets_filter):
    """bcrypt hashes leaked into logs would help an attacker confirm
    the algorithm and round count even without the plaintext."""
    rec = _make_record("user 42 hash: $2b$12$ABCDEFGHIJKLMNOPQRSTUVabcdefghijklmnopqrstuvwxyz01234")
    secrets_filter.filter(rec)
    assert "$2b$12$" not in rec.msg
    assert "BCRYPT-HASH-REDACTED" in rec.msg


def test_secrets_filter_leaves_innocuous_messages_alone(secrets_filter):
    rec = _make_record("started uvicorn on port 8000")
    secrets_filter.filter(rec)
    assert rec.msg == "started uvicorn on port 8000"


def test_secrets_filter_scrubs_string_args(secrets_filter):
    """Args passed to logger.info('user %s', token_str) must be
    scrubbed too — not just the format string."""
    rec = _make_record("token for user: %s", ("api_key=verysecret999long",))
    secrets_filter.filter(rec)
    assert "verysecret999long" not in str(rec.args)


# ─── JsonFormatter ───────────────────────────────────────────────────

def test_json_formatter_one_record_per_line():
    from backend.logging_setup import JsonFormatter
    fmt = JsonFormatter()
    rec = _make_record("hello world")
    out = fmt.format(rec)
    # Single line, parses as JSON, has the required fields
    assert "\n" not in out
    parsed = json.loads(out)
    assert parsed["msg"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test"
    assert "ts" in parsed and parsed["ts"].endswith("Z")


def test_json_formatter_includes_extras():
    """logger.info('foo', extra={'request_id': 'x'}) must end up as
    a top-level field on the JSON record."""
    from backend.logging_setup import JsonFormatter
    fmt = JsonFormatter()
    rec = _make_record("foo")
    rec.request_id = "req-abc"
    rec.user_id = 42
    out = fmt.format(rec)
    parsed = json.loads(out)
    assert parsed["request_id"] == "req-abc"
    assert parsed["user_id"] == 42


def test_json_formatter_renders_traceback():
    from backend.logging_setup import JsonFormatter
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="t.py", lineno=1,
            msg="something blew up", args=None, exc_info=sys.exc_info(),
        )
    out = fmt.format(rec)
    parsed = json.loads(out)
    assert "traceback" in parsed
    assert "ValueError: boom" in parsed["traceback"]


def test_json_formatter_skips_unserialisable_extras():
    """Custom objects that can't be JSON-encoded should be stringified
    rather than crash the formatter."""
    from backend.logging_setup import JsonFormatter
    class Weird:
        def __repr__(self): return "<Weird obj>"
    fmt = JsonFormatter()
    rec = _make_record("test")
    rec.thing = Weird()
    out = fmt.format(rec)
    parsed = json.loads(out)
    assert parsed["thing"] == "<Weird obj>"


# ─── SqliteErrorHandler ───────────────────────────────────────────────

def test_sqlite_handler_persists_warnings(fresh_app):
    """A WARNING-level record must end up in the error_log table."""
    # Importing backend.error_log requires the DB schema to exist —
    # fresh_app's fixture sets HOMEOS_DB_PATH + init_db()'s migrations
    # apply 002_error_log_table.sql automatically.
    from backend import error_log as el

    handler = el.SqliteErrorHandler()
    handler.setLevel(logging.WARNING)
    rec = _make_record("disk almost full")
    rec.levelname = "WARNING"
    rec.levelno = logging.WARNING
    handler.emit(rec)

    rows = el.recent(limit=10)
    assert any("disk almost full" in r["message"] for r in rows)


def test_sqlite_handler_records_traceback(fresh_app):
    from backend import error_log as el

    handler = el.SqliteErrorHandler()
    handler.setLevel(logging.WARNING)
    try:
        raise RuntimeError("oh no")
    except RuntimeError:
        import sys
        rec = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="t.py", lineno=1,
            msg="caught it", args=None, exc_info=sys.exc_info(),
        )
    handler.emit(rec)

    rows = el.recent(limit=10)
    matching = [r for r in rows if r["message"] == "caught it"]
    assert matching, "error_log row not found"
    assert "RuntimeError: oh no" in (matching[0]["traceback"] or "")


def test_sqlite_handler_swallows_db_errors():
    """If the DB is unreachable mid-write, the handler must NOT
    propagate — a failing log handler should never crash the caller."""
    from backend import error_log as el

    handler = el.SqliteErrorHandler()
    handler.setLevel(logging.WARNING)
    # Monkey-patch get_conn to raise; emit should still return cleanly.
    import backend.error_log
    original = backend.error_log.get_conn
    def boom(*a, **kw): raise sqlite3.OperationalError("db is gone")
    import sqlite3
    backend.error_log.get_conn = boom
    try:
        rec = _make_record("test")
        rec.levelname = "WARNING"
        rec.levelno = logging.WARNING
        # Must not raise
        handler.emit(rec)
    finally:
        backend.error_log.get_conn = original


def test_error_log_recent_filters_by_level(fresh_app):
    """recent(level='ERROR') must only return ERROR rows, not WARNING."""
    from backend import error_log as el

    handler = el.SqliteErrorHandler()
    handler.setLevel(logging.WARNING)

    rec_w = _make_record("a warning")
    rec_w.levelname = "WARNING"; rec_w.levelno = logging.WARNING
    handler.emit(rec_w)

    rec_e = _make_record("an error")
    rec_e.levelname = "ERROR"; rec_e.levelno = logging.ERROR
    handler.emit(rec_e)

    errors_only = el.recent(limit=10, level="ERROR")
    assert all(r["level"] == "ERROR" for r in errors_only)
    assert any("an error" in r["message"] for r in errors_only)
