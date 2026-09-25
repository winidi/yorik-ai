"""Pipelines — rows, history, the per-person switch.

A pipeline is its owner's alone: every read here takes the caller's
user id and answers None / nothing for someone else's pipeline (the
routes turn that into 404). No admin exception, see the audit of
2026-09-22.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ..database import conn_ctx

log = logging.getLogger("yorik.pipelines")

# Roles that count as parents/admins for the per-person switch. A
# restricted (child) account, a viewer or an assistant cannot switch
# anyone off.
ADULT_ROLES = ("platform_admin", "admin", "member")
ADMIN_ROLES = ("platform_admin", "admin")

ACTIVE_STATES = ("entwurf", "laeuft", "pausiert")


def now() -> datetime:
    return datetime.now(timezone.utc)


def _j(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return default


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def to_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ─── the per-person switch ──────────────────────────────────────────

def person_enabled(user_id: Any) -> bool:
    with conn_ctx() as c:
        row = c.execute("SELECT enabled FROM pipeline_person_settings WHERE user_id = ?",
                        (str(user_id),)).fetchone()
    return True if row is None else bool(row["enabled"])


def set_person_enabled(user_id: str, enabled: bool, changed_by: str) -> None:
    with conn_ctx() as c:
        c.execute(
            "INSERT INTO pipeline_person_settings (user_id, enabled, changed_by, changed_at) "
            "VALUES (?, ?, ?, now()) "
            "ON CONFLICT (user_id) DO UPDATE SET enabled = EXCLUDED.enabled, "
            "changed_by = EXCLUDED.changed_by, changed_at = now()",
            (user_id, bool(enabled), changed_by),
        )
        if enabled:
            # Pipelines held for the switch run on from where they were.
            c.execute(
                "UPDATE pipelines SET attention = NULL, attention_json = NULL, "
                "next_run_at = now(), updated_at = now() "
                "WHERE owner_user_id = ? AND attention = 'person_aus'",
                (user_id,),
            )
        else:
            # A question already waiting for the person (a possible answer,
            # an unclear send, …) stays; the engine holds those anyway and
            # clearing one could lead to a second send later.
            c.execute(
                "UPDATE pipelines SET attention = 'person_aus', attention_json = NULL, "
                "updated_at = now() WHERE owner_user_id = ? AND state = 'laeuft' "
                "AND (attention IS NULL OR attention IN ('schritt_faellig', 'kann_nicht_pruefen'))",
                (user_id,),
            )


def people_settings(caller_role: str = "") -> list[dict[str, Any]]:
    """Everyone in the household with their switch, for parents/admins.
    `can_change`: whether the caller may flip it (an admin's switch is for
    admins only)."""
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT p.id, p.name, p.first_name, lower(p.role) AS role, "
            "       COALESCE(s.enabled, TRUE) AS enabled, s.changed_at, "
            "       cb.name AS changed_by_name "
            "FROM user_profiles p "
            "LEFT JOIN pipeline_person_settings s ON s.user_id = p.id "
            "LEFT JOIN user_profiles cb ON cb.id = s.changed_by "
            "WHERE COALESCE(p.disabled, 0) = 0 "
            "ORDER BY p.name"
        ).fetchall()
    return [{"id": str(r["id"]), "name": r["first_name"] or r["name"], "role": r["role"],
             "enabled": bool(r["enabled"]), "changed_at": _iso(r["changed_at"]),
             "changed_by": r["changed_by_name"],
             "can_change": r["role"] not in ADMIN_ROLES or caller_role in ADMIN_ROLES} for r in rows]


def role_of(user_id: str) -> str:
    with conn_ctx() as c:
        row = c.execute("SELECT lower(role) AS role FROM user_profiles WHERE id = ?",
                        (user_id,)).fetchone()
    return (row["role"] if row else "") or ""


# ─── pipelines ──────────────────────────────────────────────────────

def _row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "kind": r["kind"],
        "title": r["title"],
        "goal": r["goal"],
        "state": r["state"],
        "mode": r["mode"],
        "attention": r["attention"],
        "attention_detail": _j(r["attention_json"], None),
        "origin": _j(r["origin_json"], {}),
        "features": _j(r["features_json"], {}),
        "config": _j(r["config_json"], {}),
        "result": _j(r["result_json"], None),
        "since_at": _iso(r["since_at"]),
        "next_run_at": _iso(r["next_run_at"]),
        "created_at": _iso(r["created_at"]),
        "updated_at": _iso(r["updated_at"]),
        "finished_at": _iso(r["finished_at"]),
        "owner_user_id": str(r["owner_user_id"]),
    }


def create(owner: str, *, kind: str, title: str, goal: str, origin: dict,
           features: dict, config: dict, since_at: datetime) -> int:
    with conn_ctx() as c:
        cur = c.execute(
            "INSERT INTO pipelines (owner_user_id, kind, title, goal, origin_json, "
            "features_json, config_json, since_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (owner, kind, title, goal, json.dumps(origin), json.dumps(features),
             json.dumps(config), since_at),
        )
        return int(cur.fetchone()["id"])


def get(pipeline_id: int, owner: Optional[str]) -> Optional[dict[str, Any]]:
    """The pipeline if `owner` owns it. owner=None is for the engine,
    which acts on the owner's behalf and never serves a request."""
    with conn_ctx() as c:
        if owner is None:
            r = c.execute("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,)).fetchone()
        else:
            r = c.execute("SELECT * FROM pipelines WHERE id = ? AND owner_user_id = ?",
                          (pipeline_id, owner)).fetchone()
    return _row_to_dict(r) if r else None


def list_for(owner: str) -> list[dict[str, Any]]:
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT * FROM pipelines WHERE owner_user_id = ? ORDER BY updated_at DESC",
            (owner,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


_UPDATABLE = {"title", "goal", "state", "mode", "attention", "attention_json", "origin_json",
              "features_json", "config_json", "result_json", "since_at", "next_run_at",
              "finished_at", "locked_until"}


def update(pipeline_id: int, **fields: Any) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _UPDATABLE:
            raise ValueError(f"not updatable: {k}")
        if k.endswith("_json") and v is not None and not isinstance(v, str):
            v = json.dumps(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    sets.append("updated_at = now()")
    with conn_ctx() as c:
        c.execute(f"UPDATE pipelines SET {', '.join(sets)} WHERE id = ?", (*vals, pipeline_id))


def delete(pipeline_id: int) -> None:
    with conn_ctx() as c:
        c.execute("DELETE FROM pipelines WHERE id = ?", (pipeline_id,))


# ─── steps ──────────────────────────────────────────────────────────

def step_hash(action: str, after_days: int, payload: dict) -> str:
    raw = json.dumps({"a": action, "d": int(after_days), "p": payload}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _step_dict(r: Any) -> dict[str, Any]:
    payload = _j(r["payload_json"], {})
    approved = bool(r["approved_at"]) and r["approved_hash"] == step_hash(
        r["action"], r["after_days"], payload)
    return {
        "id": int(r["id"]),
        "position": int(r["position"]),
        "action": r["action"],
        "after_days": int(r["after_days"]),
        "payload": payload,
        "approved": approved,
        "approved_at": _iso(r["approved_at"]) if approved else None,
        "status": r["status"],
        "done_at": _iso(r["done_at"]),
    }


def steps(pipeline_id: int) -> list[dict[str, Any]]:
    with conn_ctx() as c:
        rows = c.execute("SELECT * FROM pipeline_steps WHERE pipeline_id = ? ORDER BY position",
                         (pipeline_id,)).fetchall()
    return [_step_dict(r) for r in rows]


def replace_steps(pipeline_id: int, new_steps: list[dict[str, Any]]) -> None:
    """Write the step list. Steps keep their id per position; one whose
    action, days and text are unchanged keeps its approval, any change
    clears it. Steps already done are never rewritten."""
    old = {s["position"]: s for s in steps(pipeline_id)}
    with conn_ctx() as c:
        for pos, s in enumerate(new_steps):
            prev = old.get(pos)
            if prev and prev["status"] == "erledigt":
                continue
            payload = s.get("payload") or {}
            h = step_hash(s["action"], s["after_days"], payload)
            if prev is None:
                c.execute(
                    "INSERT INTO pipeline_steps (pipeline_id, position, action, after_days, payload_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pipeline_id, pos, s["action"], int(s["after_days"]), json.dumps(payload)),
                )
                continue
            same = step_hash(prev["action"], prev["after_days"], prev["payload"]) == h
            c.execute(
                "UPDATE pipeline_steps SET action = ?, after_days = ?, payload_json = ?"
                + ("" if same else ", approved_at = NULL, approved_hash = NULL")
                + " WHERE id = ?",
                (s["action"], int(s["after_days"]), json.dumps(payload), prev["id"]),
            )
        c.execute("DELETE FROM pipeline_steps WHERE pipeline_id = ? AND status = 'offen' AND position >= ?",
                  (pipeline_id, len(new_steps)))


def approve_step(pipeline_id: int, step_id: int, approved: bool) -> bool:
    with conn_ctx() as c:
        r = c.execute("SELECT * FROM pipeline_steps WHERE id = ? AND pipeline_id = ?",
                      (step_id, pipeline_id)).fetchone()
        if not r or r["status"] != "offen":
            return False
        if approved:
            h = step_hash(r["action"], r["after_days"], _j(r["payload_json"], {}))
            c.execute("UPDATE pipeline_steps SET approved_at = now(), approved_hash = ? WHERE id = ?",
                      (h, step_id))
        else:
            c.execute("UPDATE pipeline_steps SET approved_at = NULL, approved_hash = NULL WHERE id = ?",
                      (step_id,))
    return True


def mark_step_done(step_id: int) -> None:
    with conn_ctx() as c:
        c.execute("UPDATE pipeline_steps SET status = 'erledigt', done_at = now() WHERE id = ?",
                  (step_id,))


# ─── history ────────────────────────────────────────────────────────

def event(pipeline_id: int, kind: str, text: str, data: Optional[dict] = None) -> None:
    try:
        with conn_ctx() as c:
            c.execute("INSERT INTO pipeline_events (pipeline_id, kind, text, data_json) VALUES (?, ?, ?, ?)",
                      (pipeline_id, kind, text, json.dumps(data) if data else None))
    except Exception as exc:  # noqa: BLE001 — history must never break the work
        log.warning("pipeline %s: event not written: %s", pipeline_id, exc)


def events(pipeline_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT id, at, kind, text, data_json FROM pipeline_events WHERE pipeline_id = ? "
            "ORDER BY at DESC, id DESC LIMIT ?", (pipeline_id, limit)).fetchall()
    return [{"id": int(r["id"]), "at": _iso(r["at"]), "kind": r["kind"], "text": r["text"],
             "data": _j(r["data_json"], None)} for r in rows]


# ─── outward actions ────────────────────────────────────────────────

def action_begin(pipeline_id: int, step_id: int, idem_key: str, kind: str,
                 message_id: str) -> bool:
    """Claim the key before acting. False when it was claimed before —
    then nothing may be sent again."""
    with conn_ctx() as c:
        cur = c.execute(
            "INSERT INTO pipeline_actions (pipeline_id, step_id, idem_key, kind, status, message_id) "
            "VALUES (?, ?, ?, ?, 'sendet', ?) ON CONFLICT (idem_key) DO NOTHING RETURNING id",
            (pipeline_id, step_id, idem_key, kind, message_id),
        )
        return cur.fetchone() is not None


def action_finish(idem_key: str, status: str, error: Optional[str] = None) -> None:
    with conn_ctx() as c:
        c.execute("UPDATE pipeline_actions SET status = ?, error = ?, done_at = now() WHERE idem_key = ?",
                  (status, error, idem_key))


def actions(pipeline_id: int) -> list[dict[str, Any]]:
    with conn_ctx() as c:
        rows = c.execute("SELECT * FROM pipeline_actions WHERE pipeline_id = ? ORDER BY id",
                         (pipeline_id,)).fetchall()
    return [{"id": int(r["id"]), "step_id": r["step_id"], "idem_key": r["idem_key"],
             "kind": r["kind"], "status": r["status"], "message_id": r["message_id"],
             "error": r["error"], "created_at": _iso(r["created_at"]),
             "done_at": _iso(r["done_at"])} for r in rows]


def own_message_ids(pipeline_id: int) -> list[str]:
    """Message-IDs this pipeline sent (for 'is this our own mail')."""
    with conn_ctx() as c:
        rows = c.execute("SELECT message_id FROM pipeline_actions WHERE pipeline_id = ? "
                         "AND message_id IS NOT NULL", (pipeline_id,)).fetchall()
    return [r["message_id"] for r in rows]
