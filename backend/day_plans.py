"""Day plans — the mechanics behind planning a day in Yorik.

A plan is a list of items for one date. Every item becomes a task; items
with a start and end also get a block in the user's "Plan" calendar,
linked both ways (tasks.event_id / events.task_id). Items carry a stable
`key`, so applying a revised plan updates, moves or removes what the
previous version created instead of duplicating it.

Applying a plan is one action: everything it created and changed is
recorded, and one pending card undoes the whole day (rollback kind
`unplan_day`). The draft that led to the plan is kept in `day_plans`
so a conversation can iterate on it and the evening review can compare
plan and reality.

No LLM in here. The planning conversation lives in the skills
(plan_my_day, plan_day, day_review); this module only reads and writes.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from .database import get_conn

log = logging.getLogger("yorik.day_plans")

PLAN_CALENDAR_NAME = "Plan"
MAX_ITEMS = 20
MAX_BLOCKS = 6

_KEY_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _KEY_RE.sub("-", (text or "").lower()).strip("-")[:40] or "item"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ─── plan calendar ──────────────────────────────────────────────────

def plan_calendar_id(user_id: str, user_name: Optional[str] = None) -> int:
    """The user's private 'Plan' calendar, created on first use. Time
    blocks go here so they never clutter the shared household calendar."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM calendars WHERE owner_user_id = ? AND kind = 'plan' "
            "AND archived_at IS NULL ORDER BY id LIMIT 1", (user_id,),
        ).fetchone()
    if row:
        return int(row["id"])
    from .calendars import create_calendar
    cid = create_calendar(name=PLAN_CALENDAR_NAME, owner_user_id=user_id, color="#f59e0b", kind="plan")
    log.info("plan calendar #%d created for user %s", cid, user_id)
    return cid


# ─── items ──────────────────────────────────────────────────────────

def normalize_items(plan_date: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate and complete the items the planner hands over."""
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items per day")
    out: List[Dict[str, Any]] = []
    seen: set = set()
    blocks = 0
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("each item must be an object")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise ValueError("item without title")
        key = slug(str(raw.get("key") or title))
        if key in seen:
            raise ValueError(f"duplicate item key {key!r}")
        seen.add(key)
        start = _time_on(plan_date, raw.get("start"))
        end = _time_on(plan_date, raw.get("end"))
        if (start is None) != (end is None):
            raise ValueError(f"item {title!r}: start and end belong together")
        if start and end and end <= start:
            raise ValueError(f"item {title!r}: end before start")
        if start:
            blocks += 1
        est = raw.get("estimated_minutes")
        out.append({
            "key": key,
            "title": title[:200],
            "start": start,
            "end": end,
            "category": (str(raw.get("category") or "").strip() or None),
            "notes": (str(raw.get("notes") or "").strip() or None),
            "estimated_minutes": int(est) if isinstance(est, (int, float)) and est > 0 else None,
            "task_id": raw.get("task_id"),   # an existing open task the item stands for
        })
    if blocks > MAX_BLOCKS:
        raise ValueError(f"at most {MAX_BLOCKS} time blocks per day; leave the rest as tasks")
    return out


def _time_on(plan_date: str, value: Any) -> Optional[str]:
    """'10:00' or an ISO datetime → 'YYYY-MM-DDTHH:MM:SS' on plan_date."""
    if value in (None, ""):
        return None
    v = str(value).strip()
    if "T" in v:
        return v[:19] if len(v) >= 16 else None
    if re.match(r"^\d{1,2}:\d{2}$", v):
        h, m = v.split(":")
        return f"{plan_date}T{int(h):02d}:{int(m):02d}:00"
    raise ValueError(f"unreadable time {value!r} (use HH:MM)")


# ─── apply / rollback ───────────────────────────────────────────────

def apply_plan(*, user_id: str, plan_date: str, items: List[Dict[str, Any]],
               space_id: Optional[int] = None, user_name: Optional[str] = None) -> Dict[str, Any]:
    """Write the plan. Returns a summary plus the rollback args that
    restore the previous state."""
    items = normalize_items(plan_date, items)
    cal_id = plan_calendar_id(user_id, user_name)
    rb: Dict[str, Any] = {"created_tasks": [], "created_events": [], "task_before": {},
                          "event_before": {}, "deleted_tasks": [], "deleted_events": [],
                          "plan_date": plan_date, "user_id": user_id}
    summary = {"created": 0, "updated": 0, "removed": 0, "blocks": 0}
    with get_conn() as conn:
        existing = {
            r["plan_key"]: dict(r) for r in conn.execute(
                "SELECT * FROM tasks WHERE plan_date = ? AND plan_key IS NOT NULL "
                "AND created_by_user_id = ?", (plan_date, user_id),
            ).fetchall()
        }
        for it in items:
            task = existing.pop(it["key"], None)
            if task is None and it.get("task_id"):
                task = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(it["task_id"]),)).fetchone()
                task = dict(task) if task else None
            if task is None:
                cur = conn.execute(
                    "INSERT INTO tasks (title, due_date, done, notes, category, created_by_user_id, "
                    " space_id, estimated_minutes, plan_date, plan_key) "
                    "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
                    (it["title"], plan_date, it["notes"], it["category"], user_id, space_id,
                     it["estimated_minutes"], plan_date, it["key"]),
                )
                task_id = int(cur.lastrowid)
                rb["created_tasks"].append(task_id)
                summary["created"] += 1
            else:
                task_id = int(task["id"])
                rb["task_before"][str(task_id)] = {k: task.get(k) for k in
                    ("title", "due_date", "notes", "category", "estimated_minutes", "plan_date", "plan_key", "event_id")}
                conn.execute(
                    "UPDATE tasks SET title = ?, due_date = ?, notes = COALESCE(?, notes), "
                    "category = COALESCE(?, category), estimated_minutes = COALESCE(?, estimated_minutes), "
                    "plan_date = ?, plan_key = ? WHERE id = ?",
                    (it["title"], plan_date, it["notes"], it["category"], it["estimated_minutes"],
                     plan_date, it["key"], task_id),
                )
                summary["updated"] += 1
            # time block
            ev = conn.execute(
                "SELECT * FROM events WHERE plan_date = ? AND plan_key = ? AND owner_user_id = ?",
                (plan_date, it["key"], user_id),
            ).fetchone()
            if it["start"]:
                summary["blocks"] += 1
                if ev is None:
                    cur = conn.execute(
                        "INSERT INTO events (title, starts_at, ends_at, all_day, notes, calendar_id, "
                        " owner_user_id, visibility, space_id, task_id, plan_date, plan_key) "
                        "VALUES (?, ?, ?, 0, ?, ?, ?, 'private', ?, ?, ?, ?)",
                        (it["title"], it["start"], it["end"], it["notes"], cal_id, user_id, space_id,
                         task_id, plan_date, it["key"]),
                    )
                    event_id = int(cur.lastrowid)
                    rb["created_events"].append(event_id)
                else:
                    event_id = int(ev["id"])
                    rb["event_before"][str(event_id)] = {k: ev[k] for k in ("title", "starts_at", "ends_at", "notes")}
                    conn.execute(
                        "UPDATE events SET title = ?, starts_at = ?, ends_at = ?, notes = ? WHERE id = ?",
                        (it["title"], it["start"], it["end"], it["notes"], event_id),
                    )
                conn.execute("UPDATE tasks SET event_id = ? WHERE id = ?", (event_id, task_id))
            elif ev is not None:
                rb["deleted_events"].append(dict(ev))
                conn.execute("DELETE FROM events WHERE id = ?", (int(ev["id"]),))
                conn.execute("UPDATE tasks SET event_id = NULL WHERE id = ?", (task_id,))
        # items the new plan no longer has: drop them unless already done
        for key, task in existing.items():
            if int(task.get("done") or 0) == 1:
                continue
            ev = conn.execute("SELECT * FROM events WHERE task_id = ?", (int(task["id"]),)).fetchone()
            if ev:
                rb["deleted_events"].append(dict(ev))
                conn.execute("DELETE FROM events WHERE id = ?", (int(ev["id"]),))
            rb["deleted_tasks"].append(task)
            conn.execute("DELETE FROM tasks WHERE id = ?", (int(task["id"]),))
            summary["removed"] += 1
        conn.execute(
            "INSERT INTO day_plans (user_id, plan_date, status, draft_json, applied_at, updated_at) "
            "VALUES (?, ?, 'applied', ?, ?, ?) "
            "ON CONFLICT (user_id, plan_date) DO UPDATE SET status = 'applied', draft_json = EXCLUDED.draft_json, "
            "applied_at = EXCLUDED.applied_at, updated_at = EXCLUDED.updated_at",
            (user_id, plan_date, json.dumps({"items": items}, default=str), _now(), _now()),
        )
        conn.commit()
    log.info("day plan %s for %s: %s", plan_date, user_id, summary)
    return {"summary": summary, "items": items, "rollback_args": rb, "calendar_id": cal_id}


def rollback_plan(args: Dict[str, Any]) -> Dict[str, Any]:
    """Undo apply_plan: delete what it created, restore what it changed or removed."""
    with get_conn() as conn:
        for eid in args.get("created_events", []):
            conn.execute("DELETE FROM events WHERE id = ?", (int(eid),))
        for tid in args.get("created_tasks", []):
            conn.execute("DELETE FROM tasks WHERE id = ?", (int(tid),))
        for tid, before in (args.get("task_before") or {}).items():
            conn.execute(
                "UPDATE tasks SET title = ?, due_date = ?, notes = ?, category = ?, estimated_minutes = ?, "
                "plan_date = ?, plan_key = ?, event_id = ? WHERE id = ?",
                (before.get("title"), before.get("due_date"), before.get("notes"), before.get("category"),
                 before.get("estimated_minutes"), before.get("plan_date"), before.get("plan_key"),
                 before.get("event_id"), int(tid)),
            )
        for eid, before in (args.get("event_before") or {}).items():
            conn.execute("UPDATE events SET title = ?, starts_at = ?, ends_at = ?, notes = ? WHERE id = ?",
                         (before.get("title"), before.get("starts_at"), before.get("ends_at"),
                          before.get("notes"), int(eid)))
        for t in args.get("deleted_tasks", []):
            conn.execute(
                "INSERT INTO tasks (id, title, due_date, done, notes, category, created_by_user_id, space_id, "
                " estimated_minutes, plan_date, plan_key, event_id, priority, person) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t["id"], t["title"], t.get("due_date"), t.get("done") or 0, t.get("notes"), t.get("category"),
                 t.get("created_by_user_id"), t.get("space_id"), t.get("estimated_minutes"),
                 t.get("plan_date"), t.get("plan_key"), t.get("event_id"), t.get("priority"), t.get("person")),
            )
        for e in args.get("deleted_events", []):
            conn.execute(
                "INSERT INTO events (id, title, starts_at, ends_at, all_day, notes, calendar_id, owner_user_id, "
                " visibility, space_id, task_id, plan_date, plan_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (e["id"], e["title"], e["starts_at"], e["ends_at"], e.get("all_day") or 0, e.get("notes"),
                 e.get("calendar_id"), e.get("owner_user_id"), e.get("visibility") or "private",
                 e.get("space_id"), e.get("task_id"), e.get("plan_date"), e.get("plan_key")),
            )
        conn.execute("UPDATE day_plans SET status = 'draft', applied_at = NULL WHERE user_id = ? AND plan_date = ?",
                     (args.get("user_id"), args.get("plan_date")))
        conn.commit()
    return {"unplanned": args.get("plan_date")}


# ─── drafts + context ───────────────────────────────────────────────

def get_plan(user_id: str, plan_date: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM day_plans WHERE user_id = ? AND plan_date = ?",
                         (user_id, plan_date)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("draft_json", "context_json", "review_json"):
        try:
            d[k[:-5]] = json.loads(d.pop(k) or "null")
        except Exception:  # noqa: BLE001
            d[k[:-5]] = None
    return d


def save_draft(user_id: str, plan_date: str, items: List[Dict[str, Any]], context: Optional[Dict[str, Any]] = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO day_plans (user_id, plan_date, status, draft_json, context_json, updated_at) "
            "VALUES (?, ?, 'draft', ?, ?, ?) "
            "ON CONFLICT (user_id, plan_date) DO UPDATE SET draft_json = EXCLUDED.draft_json, "
            "context_json = COALESCE(EXCLUDED.context_json, day_plans.context_json), updated_at = EXCLUDED.updated_at",
            (user_id, plan_date, json.dumps({"items": items}, default=str),
             json.dumps(context, default=str) if context else None, _now()),
        )
        conn.commit()


def context_for(user_id: str, plan_date: str, role: str = "member") -> Dict[str, Any]:
    """Everything the planner needs from Yorik itself: fixed events of the
    day (not plan blocks), open tasks due by then, yesterday's review and
    the tasks yesterday's plan left open."""
    from .calendars import visible_event_filter
    where, params = visible_event_filter(user_id, role)
    day_start, day_end = f"{plan_date}T00:00:00", f"{plan_date}T23:59:59"
    with get_conn() as conn:
        events = [dict(r) for r in conn.execute(
            f"SELECT id, title, starts_at, ends_at, all_day, location, calendar_id, plan_key "
            f"FROM events WHERE ({where}) AND starts_at <= ? AND COALESCE(ends_at, starts_at) >= ? "
            f"ORDER BY starts_at", (*params, day_end, day_start),
        ).fetchall()]
        open_tasks = [dict(r) for r in conn.execute(
            "SELECT id, title, due_date, priority, category, estimated_minutes, plan_date, plan_key "
            "FROM tasks WHERE (done = 0 OR done IS NULL) AND created_by_user_id = ? "
            "AND (due_date IS NULL OR due_date <= ?) AND parent_task_id IS NULL "
            "ORDER BY due_date NULLS LAST, priority DESC NULLS LAST, id LIMIT 40",
            (user_id, plan_date),
        ).fetchall()]
    yday = (date.fromisoformat(plan_date) - timedelta(days=1)).isoformat()
    prev = get_plan(user_id, yday)
    carry = [t for t in open_tasks if t.get("plan_date") and t["plan_date"] < plan_date]
    return {
        "date": plan_date,
        "weekday": date.fromisoformat(plan_date).strftime("%A"),
        "fixed_events": [e for e in events if not e.get("plan_key")],
        "existing_blocks": [e for e in events if e.get("plan_key")],
        "open_tasks": open_tasks,
        "carry_over": carry,
        "yesterday_review": (prev or {}).get("review"),
    }


# ─── evening review ─────────────────────────────────────────────────

def review_day(user_id: str, plan_date: str) -> Dict[str, Any]:
    """Plan versus reality in numbers. The skill turns this into words."""
    plan = get_plan(user_id, plan_date)
    planned = ((plan or {}).get("draft") or {}).get("items") or []
    with get_conn() as conn:
        done_today = [dict(r) for r in conn.execute(
            "SELECT id, title, estimated_minutes, actual_minutes, plan_key, done_at FROM tasks "
            "WHERE created_by_user_id = ? AND done = 1 AND done_at >= ? AND done_at < ? ORDER BY done_at",
            (user_id, f"{plan_date}T00:00:00", f"{plan_date}T23:59:59"),
        ).fetchall()]
        still_open = [dict(r) for r in conn.execute(
            "SELECT id, title, estimated_minutes, plan_key FROM tasks "
            "WHERE created_by_user_id = ? AND (done = 0 OR done IS NULL) AND plan_date = ? ORDER BY id",
            (user_id, plan_date),
        ).fetchall()]
    planned_keys = {i["key"] for i in planned}
    done_planned = [t for t in done_today if t.get("plan_key") in planned_keys]
    done_extra = [t for t in done_today if t.get("plan_key") not in planned_keys]
    est = sum(int(t["estimated_minutes"] or 0) for t in done_today)
    act = sum(int(t["actual_minutes"] or 0) for t in done_today)
    review = {
        "date": plan_date,
        "planned": len(planned),
        "done_planned": len(done_planned),
        "done_extra": len(done_extra),
        "open": len(still_open),
        "estimated_minutes": est,
        "actual_minutes": act,
        "done": [{"title": t["title"], "estimated": t["estimated_minutes"], "actual": t["actual_minutes"]} for t in done_today],
        "left": [{"id": t["id"], "title": t["title"]} for t in still_open],
    }
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO day_plans (user_id, plan_date, status, draft_json, review_json, updated_at) "
            "VALUES (?, ?, 'reviewed', ?, ?, ?) "
            "ON CONFLICT (user_id, plan_date) DO UPDATE SET status = 'reviewed', review_json = EXCLUDED.review_json, "
            "updated_at = EXCLUDED.updated_at",
            (user_id, plan_date, json.dumps({"items": planned}, default=str), json.dumps(review, default=str), _now()),
        )
        conn.commit()
    return review
