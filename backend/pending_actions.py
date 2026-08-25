"""Pending-action staging for beta confirmation modal.

When the user has `confirm_mutations=true` (default during beta), every
create/update/delete skill stages its intended action here BEFORE
executing it. The frontend renders a modal asking "does this look
right?". On confirm we replay the action for real; on cancel we drop it.

The decision is logged per (skill, llm_model, language) so we can build
a per-model success-rate dashboard — the killer beta feature for picking
which LLM is reliable enough for tool use.

Storage: a single `pending_actions` table with a 1-hour TTL.
Stale rows are pruned lazily on each stage() call (no cron needed at
home-scale; tens of pending rows max).

The skill itself is responsible for calling `stage()` instead of doing
its work when confirmation is required. See add_calendar_event for the
canonical pattern.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime
from typing import Any, Dict, Optional

from .database import get_conn

log = logging.getLogger("homeos.pending_actions")

TTL_SECONDS = 3600           # 1h — pending rows older than this are dropped
PURGE_KEEP_DECISIONS_DAYS = 90  # quality telemetry retention


def _purge_stale() -> None:
    """Drop pending_actions older than TTL_SECONDS. Cheap — called on every stage()."""
    time_expr = (
        "to_char(now() - interval '" + str(int(TTL_SECONDS)) + " seconds', "
        "'YYYY-MM-DD HH24:MI:SS')"
    )
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM pending_actions "
            f"WHERE created_at < {time_expr}"
        )


def stage(
    *,
    skill: str,
    params: Dict[str, Any],
    preview: Dict[str, Any],
    user_id: str,
    llm_model: str = "",
    language: str = "en",
    rollback_kind: str = "",
    rollback_args: Optional[Dict[str, Any]] = None,
) -> str:
    """Persist a pending action; return its id.

    Two persistence modes:
      - Legacy (no rollback): `params` is what we'll re-run on confirm.
        Used when the skill defers the mutation entirely. Phased out.
      - Rollback (recommended): the skill applied its action immediately
        and we store what to undo on cancel/test in `rollback_kind` +
        `rollback_args`. Confirm is a no-op (action already applied).

    `preview` is the human-readable shape rendered in the chat / voice
    popover (what the user is being asked to approve).
    """
    _purge_stale()
    pid = secrets.token_urlsafe(16)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO pending_actions "
            "(id, user_id, skill, params_json, preview_json, llm_model, language, "
            " rollback_kind, rollback_args_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, user_id, skill,
             json.dumps(params, default=str),
             json.dumps(preview, default=str),
             llm_model, language,
             rollback_kind,
             json.dumps(rollback_args or {}, default=str)),
        )
    log.info("staged pending action %s for user=%s skill=%s model=%s rollback=%s",
             pid, user_id, skill, llm_model, rollback_kind or "(none)")
    return pid


def get(pending_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a pending action row by id, or None if missing/expired."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, skill, params_json, preview_json, llm_model, language, "
            "       rollback_kind, rollback_args_json, created_at "
            "FROM pending_actions WHERE id=?",
            (pending_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id":             row["id"],
        "user_id":        row["user_id"],
        "skill":          row["skill"],
        "params":         json.loads(row["params_json"]),
        "preview":        json.loads(row["preview_json"]),
        "llm_model":      row["llm_model"],
        "language":       row["language"],
        "rollback_kind":  row["rollback_kind"] or "",
        "rollback_args":  json.loads(row["rollback_args_json"] or "{}"),
        "created_at":     row["created_at"],
    }


def drop(pending_id: str) -> None:
    """Remove a pending action — called after confirm/cancel/test resolves it."""
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_actions WHERE id=?", (pending_id,))


def record_decision(
    *,
    skill: str,
    decision: str,
    user_id: str,
    llm_model: str = "",
    language: str = "en",
    params: Optional[Dict[str, Any]] = None,
) -> None:
    """Log a user decision for the per-model quality dashboard.

    `decision`:
      - "confirmed" — clicked Looks good or said ja/passt → counted as success
      - "cancelled" — clicked Cancel or said nein/abbrechen → counted as failure
      - "test"      — clicked Just testing → executed but excluded from success rate
      - "auto"      — user has confirm_mutations=False, no modal shown
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO skill_decisions (skill, llm_model, language, decision, user_id, params_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (skill, llm_model, language, decision, user_id,
             json.dumps(params or {}, default=str)),
        )


def quality_matrix() -> list[Dict[str, Any]]:
    """Per-(skill, llm_model) success rates for the Settings → Quality dashboard.

    Success rate excludes "test" + "auto" decisions — only honest user
    confirmations vs cancellations count. Returns rows ordered by
    skill, then by descending success rate.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                skill,
                llm_model,
                SUM(CASE WHEN decision = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
                SUM(CASE WHEN decision = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN decision = 'test' THEN 1 ELSE 0 END) AS test_count,
                SUM(CASE WHEN decision = 'auto' THEN 1 ELSE 0 END) AS auto_count,
                COUNT(*) AS total
            FROM skill_decisions
            WHERE ts > datetime('now', '-90 days')
            GROUP BY skill, llm_model
            HAVING total >= 1
            ORDER BY skill ASC, (CAST(confirmed AS REAL) / NULLIF(confirmed + cancelled, 0)) DESC
        """).fetchall()
    out = []
    for r in rows:
        confirmed = r["confirmed"] or 0
        cancelled = r["cancelled"] or 0
        decided = confirmed + cancelled
        out.append({
            "skill":         r["skill"],
            "llm_model":     r["llm_model"] or "(unknown)",
            "confirmed":     confirmed,
            "cancelled":     cancelled,
            "test_count":    r["test_count"] or 0,
            "auto_count":    r["auto_count"] or 0,
            "total":         r["total"] or 0,
            "success_rate":  (confirmed / decided) if decided > 0 else None,
        })
    return out


# ─────────────────────────── rollback (cancel/test path) ────────────────

def rollback(pending_id: str) -> Dict[str, Any]:
    """Undo the action staged under `pending_id`.

    Dispatches on `rollback_kind`. The skill chose what kind of rollback
    to record when it staged. Returns a dict describing what was undone
    so the API can surface it (e.g. ui_actions to refresh the calendar).
    """
    row = get(pending_id)
    if not row:
        raise KeyError(f"pending action {pending_id} not found or expired")

    kind = row.get("rollback_kind") or ""
    args = row.get("rollback_args") or {}

    if kind.startswith(APPLY_PREFIX):
        # Confirm-before-apply row: nothing happened yet, so cancelling
        # is just discarding the card.
        return {"discarded": kind[len(APPLY_PREFIX):]}

    if kind == "delete_event":
        # Reverses add_calendar_event by deleting the inserted row.
        event_id = args.get("event_id")
        if not isinstance(event_id, int) or event_id <= 0:
            raise ValueError(f"invalid event_id in rollback_args: {args!r}")
        from .database import get_conn as _get_conn
        with _get_conn() as conn:
            row_before = conn.execute(
                "SELECT id, starts_at, title FROM events WHERE id=?", (event_id,),
            ).fetchone()
            conn.execute("DELETE FROM events WHERE id=?", (event_id,))
            conn.commit()
        # Tell the calendar to refetch (highlight clears with the row).
        from .ui_tools import _append
        if row_before:
            _append({
                "type":        "show_calendar",
                "view":        "month",
                "anchor_date": (row_before["starts_at"] or "")[:10],
                "reason":      f"reverted: {row_before['title']}",
            })
        return {"undone": "delete_event", "event_id": event_id}

    if kind == "restore_event":
        # Reverses delete_calendar_event by re-inserting the row.
        # Also restores any "Anfahrt:" buffer events that were
        # cascade-deleted alongside the main one.
        row_json = args.get("event_row") or {}
        if not row_json.get("id"):
            raise ValueError("restore_event needs event_row with id")
        linked_rows = args.get("linked_rows") or []
        to_restore = [row_json] + [r for r in linked_rows if r.get("id")]
        from .database import get_conn as _get_conn
        with _get_conn() as conn:
            for r in to_restore:
                conn.execute(
                    "INSERT OR REPLACE INTO events "
                    "(id, title, starts_at, ends_at, all_day, person, notes, "
                    " calendar_id, owner_user_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["id"], r.get("title"),
                     r.get("starts_at"), r.get("ends_at"),
                     1 if r.get("all_day") else 0,
                     r.get("person"), r.get("notes"),
                     r.get("calendar_id"), r.get("owner_user_id")),
                )
            conn.commit()
        from .ui_tools import _append
        _append({
            "type":                "show_calendar",
            "view":                "month",
            "anchor_date":         (row_json.get("starts_at") or "")[:10],
            "highlight_event_ids": [r["id"] for r in to_restore],
            "reason":              f"restored: {row_json.get('title')}",
        })
        return {
            "undone":         "restore_event",
            "event_id":       row_json["id"],
            "linked_event_ids": [r["id"] for r in linked_rows if r.get("id")],
        }

    if kind == "revert_event_fields":
        # Reverses update_calendar_event by restoring captured field values.
        event_id = args.get("event_id")
        before = args.get("before") or {}
        if not event_id or not before:
            raise ValueError(f"revert_event_fields needs event_id + before")
        cols = [c for c in before if c in ("title", "starts_at", "ends_at", "all_day", "person", "notes")]
        if not cols:
            return {"undone": "revert_event_fields", "no_fields": True}
        from .database import get_conn as _get_conn
        with _get_conn() as conn:
            set_clause = ", ".join(f"{c}=?" for c in cols)
            params = [before[c] for c in cols] + [event_id]
            conn.execute(f"UPDATE events SET {set_clause} WHERE id=?", params)
            conn.commit()
        from .ui_tools import _append
        _append({
            "type":                "show_calendar",
            "view":                "month",
            "anchor_date":         (before.get("starts_at") or "")[:10],
            "highlight_event_ids": [event_id],
            "reason":              "reverted update",
        })
        return {"undone": "revert_event_fields", "event_id": event_id}

    # ── tasks ──────────────────────────────────────────────────────
    if kind == "delete_task":
        task_id = args.get("task_id")
        if not isinstance(task_id, int) or task_id <= 0:
            raise ValueError(f"invalid task_id: {args!r}")
        from .database import get_conn as _get_conn
        with _get_conn() as conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            conn.commit()
        from .ui_tools import _append
        _append({"type": "refresh_data", "table": "tasks", "reason": "reverted task create"})
        return {"undone": "delete_task", "task_id": task_id}

    if kind == "restore_task":
        task_row = args.get("task_row") or {}
        if not task_row.get("id"):
            raise ValueError("restore_task needs task_row with id")
        from .database import get_conn as _get_conn
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, title, due_date, done, person, notes, category, "
                " created_by_user_id, space_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_row["id"], task_row.get("title"), task_row.get("due_date"),
                 1 if task_row.get("done") else 0,
                 task_row.get("person"), task_row.get("notes"),
                 task_row.get("category"),
                 task_row.get("created_by_user_id"),
                 task_row.get("space_id")),
            )
            conn.commit()
        from .ui_tools import _append
        _append({"type": "refresh_data", "table": "tasks", "highlight_id": task_row["id"],
                 "reason": f"restored task: {task_row.get('title')}"})
        return {"undone": "restore_task", "task_id": task_row["id"]}

    if kind == "revert_task_fields":
        task_id = args.get("task_id")
        before = args.get("before") or {}
        if not task_id or not before:
            raise ValueError("revert_task_fields needs task_id + before")
        cols = [c for c in before if c in ("title", "due_date", "done", "person", "notes", "category")]
        if not cols:
            return {"undone": "revert_task_fields", "no_fields": True}
        from .database import get_conn as _get_conn
        with _get_conn() as conn:
            set_clause = ", ".join(f"{c}=?" for c in cols)
            params = [before[c] for c in cols] + [task_id]
            conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", params)
            conn.commit()
        from .ui_tools import _append
        _append({"type": "refresh_data", "table": "tasks", "highlight_id": task_id,
                 "reason": "reverted task update"})
        return {"undone": "revert_task_fields", "task_id": task_id}

    # ── bills ──────────────────────────────────────────────────────
    if kind == "delete_bill":
        bill_id = args.get("bill_id")
        if not isinstance(bill_id, int) or bill_id <= 0:
            raise ValueError(f"invalid bill_id: {args!r}")
        from .database import get_conn as _get_conn
        with _get_conn() as conn:
            conn.execute("DELETE FROM bills WHERE id=?", (bill_id,))
            conn.commit()
        from .ui_tools import _append
        _append({"type": "refresh_data", "table": "bills", "reason": "reverted bill create"})
        return {"undone": "delete_bill", "bill_id": bill_id}

    if kind == "restore_bill":
        bill_row = args.get("bill_row") or {}
        if not bill_row.get("id"):
            raise ValueError("restore_bill needs bill_row with id")
        from .database import get_conn as _get_conn
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bills "
                "(id, name, amount, currency, due_date, recurring, paid, notes, space_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (bill_row["id"], bill_row.get("name"), bill_row.get("amount"),
                 bill_row.get("currency", "EUR"), bill_row.get("due_date"),
                 bill_row.get("recurring"), 1 if bill_row.get("paid") else 0,
                 bill_row.get("notes"), bill_row.get("space_id")),
            )
            conn.commit()
        from .ui_tools import _append
        _append({"type": "refresh_data", "table": "bills", "highlight_id": bill_row["id"],
                 "reason": f"restored bill: {bill_row.get('name')}"})
        return {"undone": "restore_bill", "bill_id": bill_row["id"]}

    if kind == "revert_bill_fields":
        bill_id = args.get("bill_id")
        before = args.get("before") or {}
        if not bill_id or not before:
            raise ValueError("revert_bill_fields needs bill_id + before")
        cols = [c for c in before if c in ("name", "amount", "currency", "due_date", "recurring", "paid", "notes")]
        if not cols:
            return {"undone": "revert_bill_fields", "no_fields": True}
        from .database import get_conn as _get_conn
        with _get_conn() as conn:
            set_clause = ", ".join(f"{c}=?" for c in cols)
            params = [before[c] for c in cols] + [bill_id]
            conn.execute(f"UPDATE bills SET {set_clause} WHERE id=?", params)
            conn.commit()
        from .ui_tools import _append
        _append({"type": "refresh_data", "table": "bills", "highlight_id": bill_id,
                 "reason": "reverted bill update"})
        return {"undone": "revert_bill_fields", "bill_id": bill_id}

    # ── contacts ──────────────────────────────────────────────────────
    if kind == "delete_contact":
        # Reverses add_contact (and add_contact_channel / _address if any
        # were attached at create time). args = {"contact_id": N}.
        contact_id = args.get("contact_id")
        if not isinstance(contact_id, int) or contact_id <= 0:
            raise ValueError(f"invalid contact_id in rollback_args: {args!r}")
        from . import contacts as _contacts
        try:
            _contacts.delete(contact_id)
        except ValueError:
            pass  # already gone; rollback is a no-op
        return {"undone": "delete_contact", "contact_id": contact_id}

    if kind == "restore_contact":
        # Reverses delete_contact by re-creating the row + its channels +
        # addresses from the captured snapshot. args = {"snapshot": {...}}.
        snap = args.get("snapshot") or {}
        if not snap.get("display_name"):
            raise ValueError("restore_contact needs snapshot.display_name")
        from . import contacts as _contacts
        new_id = _contacts.create(
            display_name=snap["display_name"],
            kind=snap.get("kind") or "person",
            status=snap.get("status") or "active",
            aliases=snap.get("aliases"),
            relation=snap.get("relation"),
            birthday=snap.get("birthday"),
            language_pref=snap.get("language_pref"),
            salutation_pref=snap.get("salutation_pref"),
            legal_name=snap.get("legal_name"),
            tax_id=snap.get("tax_id"),
            iban=snap.get("iban"),
            payment_terms_days=snap.get("payment_terms_days"),
            default_currency=snap.get("default_currency"),
            notes=snap.get("notes"),
            tags=snap.get("tags"),
            space_id=snap.get("space_id"),
            source=snap.get("source") or "manual",
        )
        for ch in snap.get("channels") or []:
            try:
                _contacts.add_channel(
                    new_id,
                    kind=ch.get("kind"), value=ch.get("value"),
                    label=ch.get("label"), source=ch.get("source") or "restore",
                )
            except Exception:
                continue  # another contact may have claimed this channel meanwhile
        for ad in snap.get("addresses") or []:
            try:
                _contacts.add_address(
                    new_id,
                    kind=ad.get("kind") or "home",
                    line1=ad.get("line1"), line2=ad.get("line2"),
                    postcode=ad.get("postcode"), city=ad.get("city"),
                    region=ad.get("region"), country=ad.get("country"),
                    label=ad.get("label"), source=ad.get("source") or "restore",
                )
            except Exception:
                continue
        return {"undone": "restore_contact", "contact_id": new_id}

    if kind == "revert_contact_fields":
        # Reverses update_contact by restoring captured field values.
        contact_id = args.get("contact_id")
        before = args.get("before") or {}
        if not contact_id or not before:
            raise ValueError("revert_contact_fields needs contact_id + before")
        from . import contacts as _contacts
        # Only restore fields that contacts.update() accepts; drop unknowns.
        allowed = {
            "display_name", "aliases", "kind", "status", "relation", "birthday",
            "language_pref", "salutation_pref", "legal_name", "tax_id", "iban",
            "payment_terms_days", "default_currency", "notes", "tags",
            "last_used_at", "last_interaction_at", "space_id",
        }
        clean = {k: v for k, v in before.items() if k in allowed}
        if clean:
            _contacts.update(contact_id, **clean)
        return {"undone": "revert_contact_fields", "contact_id": contact_id}

    if kind == "remove_contact_channel":
        ch_id = args.get("channel_id")
        if not ch_id:
            raise ValueError("remove_contact_channel needs channel_id")
        from . import contacts as _contacts
        _contacts.remove_channel(int(ch_id))
        return {"undone": "remove_contact_channel", "channel_id": ch_id}

    if kind == "remove_contact_address":
        ad_id = args.get("address_id")
        if not ad_id:
            raise ValueError("remove_contact_address needs address_id")
        from . import contacts as _contacts
        _contacts.remove_address(int(ad_id))
        return {"undone": "remove_contact_address", "address_id": ad_id}

    if kind == "restore_compose_draft":
        # Reverses delete_compose_draft by re-inserting the row with its
        # original id. compose_draft_versions cascade-deleted with the
        # original row and are NOT recreated — see skill.md.
        snap = args.get("snapshot") or {}
        if not snap.get("id"):
            raise ValueError("restore_compose_draft needs snapshot.id")
        from .database import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO compose_drafts (id, user_id, kind, template_id, "
                "  recipient, subject, body_html, args_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snap["id"], snap.get("user_id"),
                    snap.get("kind") or "letter",
                    snap.get("template_id"),
                    snap.get("recipient"), snap.get("subject"),
                    snap.get("body_html") or "",
                    snap.get("args_json") or "{}",
                    snap.get("created_at"), snap.get("updated_at"),
                ),
            )
            conn.commit()
        return {"undone": "restore_compose_draft", "draft_id": snap["id"]}

    if kind == "":
        # Legacy stage() without rollback info — nothing to undo.
        return {"undone": None}

    raise ValueError(f"unknown rollback_kind: {kind!r}")


# ─────────────────────────── helper for skills ───────────────────────────

def should_confirm(ctx: Any) -> bool:
    """Skills call this to decide whether to stage or execute directly.

    Reads the user's `confirm_mutations` preference from user_profiles.
    Defaults to True during beta (= the column default). Voice and chat
    flows both populate ctx.user_id.
    """
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        # No identified user → default to confirm (safer)
        return True
    with get_conn() as conn:
        row = conn.execute(
            "SELECT confirm_mutations FROM user_profiles WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return True
    # SQLite stores as 0/1; column is BOOLEAN DEFAULT 1
    return bool(row["confirm_mutations"])


def stage_with_rollback(
    *,
    skill: str,
    preview: Dict[str, Any],
    ctx: Any,
    rollback_kind: str,
    rollback_args: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """Apply-then-confirm pattern: the skill has ALREADY applied its
    change to the DB and is calling this to register the rollback.

    Side effects:
      - INSERT row into pending_actions with rollback metadata
      - Emit a `pending_confirmation` UI action so the chat / voice
        UI can render inline confirm buttons attached to the message

    Returns the pending_id.
    """
    from . import ask as vanna_agent
    from .ui_tools import _append

    pending_id = stage(
        skill=skill,
        params=params or {},
        preview=preview,
        user_id=getattr(ctx, "user_id", 1),
        llm_model=vanna_agent.LLM_MODEL,
        language=getattr(ctx, "language", "en"),
        rollback_kind=rollback_kind,
        rollback_args=rollback_args,
    )

    _append({
        "type":       "pending_confirmation",
        "pending_id": pending_id,
        "skill":      skill,
        "preview":    preview,
    })
    return pending_id


# Legacy alias — older code still references this name. Kept thin so
# skills that haven't migrated yet still compile, but it now requires
# rollback info.
def stage_and_signal(*, skill, params, preview, ctx, rollback_kind="", rollback_args=None) -> Dict[str, Any]:
    pending_id = stage_with_rollback(
        skill=skill, preview=preview, ctx=ctx,
        rollback_kind=rollback_kind, rollback_args=rollback_args or {},
        params=params,
    )
    return {
        "pending":    True,
        "pending_id": pending_id,
        "skill":      skill,
        "message":    f"Action staged — awaiting user confirmation: {skill}",
    }


def confirm_then_apply(
    *, skill: str, ctx: Any, preview: Dict[str, Any],
    rollback_kind: str, rollback_args: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Single entry-point destructive skills should call after applying
    their change. Stages a rollback + emits `pending_confirmation` when
    confirm_mutations is on; no-ops otherwise.

    Wrapping every delete_*/cancel_* in this one call (instead of each
    skill open-coding `if should_confirm(): stage_with_rollback(...)`)
    keeps the confirm-card contract impossible to forget.

    Returns the pending_id (or None when confirm is off).
    """
    if not should_confirm(ctx):
        return None
    return stage_with_rollback(
        skill=skill, ctx=ctx, preview=preview,
        rollback_kind=rollback_kind, rollback_args=rollback_args,
        params=params,
    )


# ─── confirm-BEFORE-apply (destructive skills) ───────────────────────
#
# Deletes never run on the LLM's say-so. The skill stages what it would
# delete, the card shows it, and only the user's tap on "Delete" runs the
# statement (apply()). Cancel discards the row; nothing to roll back.
# Creates and updates keep the apply-then-undo flow above — a wrong
# insert is cheap to undo, a wrong delete is not.

APPLY_PREFIX = "apply:"


def is_deferred(row: Dict[str, Any]) -> bool:
    return (row.get("rollback_kind") or "").startswith(APPLY_PREFIX)


def stage_before_apply(
    *, skill: str, ctx: Any, preview: Dict[str, Any],
    apply_kind: str, apply_args: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """Stage a destructive action WITHOUT applying it. Emits the same
    `pending_confirmation` UI action the undo flow uses; the preview
    carries `mode: confirm_before` so the card renders Delete / Keep
    instead of ✓ done / Undo. Returns the pending_id."""
    from . import ask as vanna_agent
    from .ui_tools import _append

    preview = dict(preview or {})
    preview["mode"] = "confirm_before"
    pending_id = stage(
        skill=skill,
        params=params or {},
        preview=preview,
        user_id=getattr(ctx, "user_id", 1),
        llm_model=vanna_agent.LLM_MODEL,
        language=getattr(ctx, "language", "en"),
        rollback_kind=APPLY_PREFIX + apply_kind,
        rollback_args=apply_args,
    )
    _append({
        "type":       "pending_confirmation",
        "pending_id": pending_id,
        "skill":      skill,
        "preview":    preview,
    })
    return pending_id


def apply(pending_id: str) -> Dict[str, Any]:
    """Run the deferred action staged under `pending_id`. Called by the
    confirm route only after the ownership check."""
    row = get(pending_id)
    if not row:
        raise KeyError(f"pending action {pending_id} not found or expired")
    if not is_deferred(row):
        return {"applied": None}
    kind = (row.get("rollback_kind") or "")[len(APPLY_PREFIX):]
    args = row.get("rollback_args") or {}
    from .database import get_conn as _get_conn
    from .ui_tools import _append

    if kind == "delete_event":
        event_id = int(args["event_id"])
        linked_ids = [int(x) for x in (args.get("linked_ids") or [])]
        with _get_conn() as conn:
            before = conn.execute(
                "SELECT id, title, starts_at FROM events WHERE id=?", (event_id,),
            ).fetchone()
            cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
            deleted = cur.rowcount
            if linked_ids:
                conn.execute(
                    f"DELETE FROM events WHERE id IN ({','.join('?' * len(linked_ids))})",
                    linked_ids,
                )
            conn.commit()
        _append({
            "type":        "show_calendar",
            "view":        "month",
            "anchor_date": ((before["starts_at"] if before else "") or "")[:10],
            "reason":      f"deleted event: {before['title'] if before else event_id}",
        })
        return {"applied": kind, "event_id": event_id, "deleted": deleted,
                "cascaded": len(linked_ids)}

    if kind == "delete_task":
        task_id = int(args["task_id"])
        with _get_conn() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            deleted = cur.rowcount
            conn.commit()
        _append({"type": "refresh_data", "table": "tasks",
                 "reason": f"deleted task: {args.get('title') or task_id}"})
        return {"applied": kind, "task_id": task_id, "deleted": deleted}

    if kind == "delete_contact":
        from . import contacts as C
        contact_id = int(args["contact_id"])
        snapshot = C.delete(contact_id)
        _append({"type": "refresh_data", "table": "contacts",
                 "reason": f"deleted contact: {snapshot.get('display_name')}"})
        return {"applied": kind, "contact_id": contact_id}

    if kind == "delete_compose_draft":
        draft_id = int(args["draft_id"])
        with _get_conn() as conn:
            cur = conn.execute("DELETE FROM compose_drafts WHERE id=?", (draft_id,))
            deleted = cur.rowcount
            conn.commit()
        _append({"type": "refresh_data", "table": "compose_drafts",
                 "reason": f"deleted draft: {args.get('subject') or draft_id}"})
        return {"applied": kind, "draft_id": draft_id, "deleted": deleted}

    raise ValueError(f"unknown deferred action kind: {kind!r}")
