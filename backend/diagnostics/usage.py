"""The daily counts (tiers 1 and 2): one row a day, numbers in buckets,
nothing that names anyone. Tier 1 says how big the house is and how
much the chat is used; tier 2 adds which kinds of features get used
and what is connected. Written by the outbox tick; the row is queued
right away — there is no content to review.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from backend.database import get_conn

from . import SCHEMA_VERSION, enabled
from .report import B_COUNT, B_TURNS, B_USERS, bucket, environment
from . import registry

log = logging.getLogger("yorik.diagnostics")

# skill id → feature kind (tier 2 counts features by kind, never by name of anything in the house)
FEATURE_OF = {
    "email": ("read_email", "send_email", "email_draft", "draft_reply", "search_email", "email_unsubscribe", "list_emails", "email_"),
    "whatsapp": ("whatsapp", "wa_"),
    "documents": ("search_documents", "paperless", "read_document", "file_"),
    "calendar": ("calendar", "event", "check_calendar", "plan_my_day"),
    "tasks": ("task", "check_tasks", "todo"),
    "contacts": ("contact", "find_known_provider"),
    "bank": ("bank", "finance", "bill", "invoice_", "payment"),
    "search": ("universal_search", "web_search", "search"),
    "letters": ("write_", "compose", "letter"),
    "recordings": ("record", "transcript"),
    "voice": ("voice", "speak", "tts"),
}


def feature_of(skill_id: str) -> str:
    sid = (skill_id or "").lower()
    for feature, needles in FEATURE_OF.items():
        if any(n in sid for n in needles):
            return feature
    return "other"


def _count(conn, sql: str, params=()) -> int:
    try:
        r = conn.execute(sql, params).fetchone()
        return int((r[0] if not hasattr(r, "keys") else list(r.values())[0]) or 0) if r else 0
    except Exception:  # noqa: BLE001
        return 0


def build(day: date) -> Dict[str, Any]:
    """The payload for one day, before registry.serialise."""
    start, end = day.isoformat(), (day + timedelta(days=1)).isoformat()
    with get_conn() as conn:
        users = _count(conn, "SELECT count(*) AS n FROM user_profiles WHERE COALESCE(disabled, 0) = 0")
        turns = _count(conn, "SELECT count(*) AS n FROM skill_invocations WHERE created_at >= ? AND created_at < ? AND COALESCE(source,'chat') = 'chat'", (start, end))
        up = _count(conn, "SELECT count(*) AS n FROM turn_feedback WHERE rating > 0 AND created_at >= ? AND created_at < ?", (start, end))
        down = _count(conn, "SELECT count(*) AS n FROM turn_feedback WHERE rating < 0 AND created_at >= ? AND created_at < ?", (start, end))
        failures = _count(conn, "SELECT count(*) AS n FROM skill_invocations WHERE success = 0 AND created_at >= ? AND created_at < ?", (start, end))
        features: Dict[str, int] = {}
        try:
            for r in conn.execute("SELECT skill_id, count(*) AS n FROM skill_invocations WHERE created_at >= ? AND created_at < ? "
                                  "GROUP BY skill_id", (start, end)).fetchall():
                f = feature_of(str(r["skill_id"]))
                features[f] = features.get(f, 0) + int(r["n"] or 0)
        except Exception:  # noqa: BLE001
            pass
        mailboxes = _count(conn, "SELECT count(*) AS n FROM email_accounts")
        whatsapp = _count(conn, "SELECT count(*) AS n FROM wa_chats") > 0
        documents = _count(conn, "SELECT count(*) AS n FROM docs.paperless_documents")
        contacts = _count(conn, "SELECT count(*) AS n FROM contacts WHERE status IN ('active','pending')")
        bank = _count(conn, "SELECT count(*) AS n FROM bank_accounts") > 0
    return {
        "schema": SCHEMA_VERSION, "kind": "usage_daily", "report_id": str(uuid.uuid4()), "day": day.isoformat(),
        "env": environment(),
        "counts": {"users": bucket(users, B_USERS), "chat_turns": bucket(turns, B_TURNS),
                   "feedback_up": up, "feedback_down": down, "skill_failures": failures},
        "features": {k: bucket(v, B_TURNS) for k, v in features.items()},
        "setup": {"mailboxes": bucket(mailboxes, B_COUNT), "whatsapp": whatsapp, "documents": bucket(documents, B_COUNT),
                  "contacts": bucket(contacts, B_COUNT), "bank": bank},
    }


def collect_daily(today: Optional[date] = None) -> Optional[str]:
    """Yesterday's counts, once. Returns the report id when a row was
    queued, None when nothing was due or nothing is switched on."""
    if not enabled(1) and not enabled(2):
        return None
    day = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    with get_conn() as conn:
        done = conn.execute("SELECT 1 FROM diag_usage_daily WHERE day = ? AND metric = '_queued'", (day.isoformat(),)).fetchone()
    if done:
        return None
    payload = build(day)
    clean, _ = registry.serialise(payload, max_tier=2 if enabled(2) else 1)
    rid = clean.get("report_id") or payload["report_id"]
    with get_conn() as conn:
        for k, v in (clean.get("counts") or {}).items():
            conn.execute("INSERT INTO diag_usage_daily (day, metric, value) VALUES (?, ?, ?) ON CONFLICT (day, metric) DO UPDATE SET value = EXCLUDED.value",
                         (day.isoformat(), f"counts.{k}", str(v)))
        conn.execute("INSERT INTO diag_usage_daily (day, metric, value) VALUES (?, '_queued', ?) ON CONFLICT DO NOTHING", (day.isoformat(), rid))
        conn.execute("INSERT INTO diag_reports (id, kind, trigger, status, payload, next_attempt_at) VALUES (?, 'usage_daily', 'daily', 'queued', ?, now())",
                     (rid, json.dumps(clean, ensure_ascii=False)))
        conn.commit()
    log.info("diagnostics: daily counts for %s queued: %s", day.isoformat(), json.dumps(clean, ensure_ascii=False))
    return rid
