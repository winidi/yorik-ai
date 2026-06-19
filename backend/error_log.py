"""Persisted error log — every WARNING+ record from the logging
subsystem also lands in a SQLite table so the user can surface "what's
been breaking" without grepping log files.

Schema lives in migrations/002_error_log_table.sql so it's tracked +
versioned like any other schema change.

This module exposes:
  - SqliteErrorHandler — logging.Handler that inserts into error_log
  - recent(n) — read accessor for the API/UI
  - prune_old(max_rows) — keeps the table from growing unbounded

We avoid recursion into the logging system from inside the handler
itself: failures during insert are swallowed (best we can do — logging
an error about a failing error-logger would infinite-loop).
"""

from __future__ import annotations

import logging
import threading
import traceback as _traceback
from datetime import datetime, timezone
from typing import Any

from .database import get_conn

_MAX_TRACEBACK_CHARS = 4_000
_MAX_MSG_CHARS = 800

# We cap the table at this many rows; older entries are pruned when
# a new write pushes us over. 1000 errors is plenty to debug recent
# issues; older history is in the rotating log files anyway.
MAX_ROWS = 1_000

_lock = threading.Lock()


class SqliteErrorHandler(logging.Handler):
    """logging.Handler that mirrors WARNING+ records to the error_log
    table. Set the level via the standard setLevel(); the
    logging_setup module pins it at WARNING."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
            msg = self.format(record)[:_MAX_MSG_CHARS]
            tb = None
            if record.exc_info:
                tb = "".join(_traceback.format_exception(*record.exc_info))[:_MAX_TRACEBACK_CHARS]
            # request_path comes from a ContextVar set by the request
            # middleware (when available) — extracted via record.__dict__
            # so a non-HTTP log call (startup, background worker) just
            # writes NULL there.
            request_path = getattr(record, "request_path", None)
            # corr is stamped by CorrelationFilter for every record
            # emitted inside an HTTP request — lets the Logs UI link a
            # warning back to the full trace in data/logs/yorik.log.
            corr_id = getattr(record, "corr", None)

            with _lock, get_conn() as conn:
                conn.execute(
                    "INSERT INTO error_log "
                    "(ts, level, logger, message, traceback, request_path, corr_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ts, record.levelname, record.name, msg, tb, request_path, corr_id),
                )
                # Inline prune so we never blow past MAX_ROWS. Only when
                # we've actually added a row — saves the COUNT cost on
                # quiet boxes.
                conn.execute(
                    "DELETE FROM error_log WHERE id NOT IN "
                    "(SELECT id FROM error_log ORDER BY id DESC LIMIT ?)",
                    (MAX_ROWS,),
                )
                conn.commit()
        except Exception:
            # Never let a log-side failure propagate. Logging-about-
            # logging-failing has historically been a great way to
            # crash production. We have stderr as the canonical sink
            # for the same record already.
            pass

    def format(self, record: logging.LogRecord) -> str:
        # JSON formatter would put a JSON blob in the `message` column
        # which is hard to read on a Settings page; use a simple format
        # here. Tracebacks live in their own column.
        try:
            return record.getMessage()
        except Exception:
            return str(record.msg)


def recent(limit: int = 50, level: str | None = None) -> list[dict[str, Any]]:
    """Most-recent N error_log rows, newest first. Optionally filter
    by level (WARNING / ERROR / CRITICAL). Returns dicts shaped for
    direct JSON serialisation."""
    limit = max(1, min(int(limit), 500))
    sql = ("SELECT id, ts, level, logger, message, traceback, request_path, corr_id "
           "FROM error_log")
    params: list[Any] = []
    if level:
        sql += " WHERE level = ?"
        params.append(level.upper())
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def summary() -> dict[str, int]:
    """Counts by level, useful for a system-status badge."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT level, COUNT(*) AS n FROM error_log GROUP BY level"
        ).fetchall()
    return {r["level"]: r["n"] for r in rows}
