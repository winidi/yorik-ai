"""Per-conversation entity ledger.

When the user says "block the Anfahrt for THAT meeting" or "make the
draft friendlier", the LLM has to resolve a pronoun to a row id. Today
the only place that id lives is buried in a tool_result JSON blob from
3 turns ago. Smaller models miss it; bigger ones over-think and pick
the wrong row from training data or seed data.

This module maintains a compact ledger of entities mentioned/created
in the current conversation. Each turn the rendered ledger is injected
as a second system message:

    RECENT ENTITIES IN THIS CONVERSATION
    events:
      - id=99  "Zahnarzt bei Dr. Müller"  2026-06-12 10:00
      - id=92  "Sommerfest im Kindergarten"  2026-06-12 10:00
    drafts:
      - id=18  to "Lilian Wende"  "Besuchsankündigung"
    ...

    If the user references an entity NOT in this list, look it up via
    check_calendar / find_contact / etc. — do NOT guess from the list.

The ledger is populated automatically by observing the ui_actions a
skill emits (compose_draft_created → draft, show_calendar → event,
contacts_found → contacts, etc.) so no skill needs to be modified.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# Per-type caps. Keep the rendered block bounded so the LLM context
# doesn't bloat after a long conversation. Total cap ~20.
_PER_TYPE_CAP = 5

# Maps ui_action.type -> (ledger_bucket, fn(action) -> list[entity_dict])
# Each entity dict has at minimum: {"id": <int>, "label": <str>}.
# Optional: "subtitle" (one-line context).
def _from_compose_draft_created(a: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "id": a.get("draft_id"),
        "label": f'to "{a.get("recipient") or "?"}"',
        "subtitle": (a.get("subject") or "").strip() or None,
    }] if a.get("draft_id") else []


def _from_show_calendar(a: Dict[str, Any]) -> List[Dict[str, Any]]:
    # show_calendar carries event ids that the skill wants highlighted;
    # we don't have titles here. The check_calendar / tasks_found paths
    # carry richer data — this is the fallback when only an id leaks.
    ids = a.get("highlight_event_ids") or []
    return [{"id": int(i), "label": "(event)"} for i in ids if isinstance(i, int)]


def _from_tasks_found(a: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for t in (a.get("tasks") or [])[:5]:
        tid = t.get("id")
        if tid is None:
            continue
        out.append({
            "id": int(tid),
            "label": (t.get("title") or "").strip() or "(task)",
            "subtitle": _fmt_task_subtitle(t),
        })
    return out


def _from_contacts_found(a: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for c in (a.get("contacts") or [])[:5]:
        cid = c.get("id")
        if cid is None:
            continue
        out.append({
            "id": int(cid),
            "label": (c.get("display_name") or c.get("name") or "(contact)").strip(),
            "subtitle": (c.get("kind") or None),
        })
    return out


def _from_contact_picker(a: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Same shape as contacts_found
    return _from_contacts_found(a)


def _from_documents_found(a: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for d in (a.get("documents") or [])[:5]:
        did = d.get("id")
        if did is None:
            continue
        out.append({
            "id": int(did),
            "label": (d.get("title") or d.get("filename") or "(document)").strip(),
        })
    return out


# Maps "thing being acted on" inside pending_confirmation.preview to
# our ledger bucket. Add_* skills emit a pending_confirmation with
# action='create' and the new id under a typed key — that's how
# bills / fresh events / fresh tasks reach the ledger when their skill
# doesn't emit a dedicated *_found card.
_PENDING_BUCKETS = {
    "bill_id":    "bills",
    "event_id":   "events",
    "task_id":    "tasks",
    "contact_id": "contacts",
}


def _from_pending_confirmation(a: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Returns a single-entry list when the preview carries one of the
    known id fields. Used in conjunction with bucket inference via
    `pending_bucket_of`; see absorb() for the wiring."""
    preview = a.get("preview") or {}
    if preview.get("action") not in ("create", "update"):
        return []
    for id_key, _ in _PENDING_BUCKETS.items():
        if id_key in preview:
            label = (
                preview.get("name")
                or preview.get("title")
                or preview.get("display_name")
                or preview.get("recipient")
                or ""
            ).strip() or f"({id_key.removesuffix('_id')})"
            sub_bits = []
            if preview.get("amount") is not None:
                cur = preview.get("currency") or "EUR"
                sub_bits.append(f"{preview['amount']} {cur}")
            if preview.get("due_date"):
                sub_bits.append(preview["due_date"])
            if preview.get("starts_at"):
                sub_bits.append(str(preview["starts_at"])[:16])
            return [{
                "id": int(preview[id_key]),
                "label": label,
                "subtitle": " · ".join(sub_bits) or None,
                "_pending_bucket": _PENDING_BUCKETS[id_key],
            }]
    return []


def _fmt_task_subtitle(t: Dict[str, Any]) -> Optional[str]:
    bits = []
    if t.get("due_date"):
        bits.append(t["due_date"])
    if t.get("person"):
        bits.append(t["person"])
    return " · ".join(bits) or None


# Maps ui_action.type → (bucket name, extractor function).
# `pending_confirmation` is special-cased — its bucket depends on which
# id field the preview carries; see absorb() for the dispatch.
_EXTRACTORS = {
    "compose_draft_created": ("drafts",    _from_compose_draft_created),
    "show_calendar":         ("events",    _from_show_calendar),
    "tasks_found":           ("tasks",     _from_tasks_found),
    "contacts_found":        ("contacts",  _from_contacts_found),
    "contact_picker":        ("contacts",  _from_contact_picker),
    "documents_found":       ("documents", _from_documents_found),
    "pending_confirmation":  (None,        _from_pending_confirmation),
}


# ── Public API ───────────────────────────────────────────────────────

def empty() -> Dict[str, List[Dict[str, Any]]]:
    """Fresh ledger structure — a dict of bucket-name → list-of-entries."""
    return {}


def from_json(blob: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    if not blob:
        return {}
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def to_json(ledger: Dict[str, List[Dict[str, Any]]]) -> str:
    return json.dumps(ledger, ensure_ascii=False)


def absorb(
    ledger: Dict[str, List[Dict[str, Any]]],
    ui_actions: List[Dict[str, Any]],
) -> None:
    """Walk the ui_actions emitted this turn and update the ledger.

    Mutates ledger in place. Newest entries land at the front of each
    bucket; older duplicates (same id) are removed before the new entry
    is prepended; bucket is then truncated to _PER_TYPE_CAP.
    """
    for action in ui_actions or []:
        atype = action.get("type")
        if atype not in _EXTRACTORS:
            continue
        bucket_name, extract = _EXTRACTORS[atype]
        for entry in extract(action) or []:
            eid = entry.get("id")
            if eid is None:
                continue
            # pending_confirmation infers its bucket from the preview
            # (bills go to "bills", events to "events", etc.); the rest
            # use the static mapping above.
            bname = bucket_name or entry.pop("_pending_bucket", None)
            if not bname:
                continue
            bucket = ledger.setdefault(bname, [])
            # Merge with any existing entry for the same id: prefer the
            # richer label/subtitle so a later id-only mention (e.g.
            # show_calendar with just highlight_event_ids) doesn't
            # overwrite a previous turn's "Zahnarzt bei Dr. Müller"
            # entry with a placeholder "(event)".
            existing = next((b for b in bucket if b.get("id") == eid), None)
            if existing:
                merged = dict(existing)
                for k in ("label", "subtitle"):
                    new_v = (entry.get(k) or "").strip()
                    old_v = (merged.get(k) or "").strip()
                    if new_v and (not old_v or old_v.startswith("(") or len(new_v) > len(old_v)):
                        merged[k] = new_v
                bucket[:] = [b for b in bucket if b.get("id") != eid]
                bucket.insert(0, merged)
            else:
                bucket.insert(0, entry)
            ledger[bname] = bucket[:_PER_TYPE_CAP]


def remove(
    ledger: Dict[str, List[Dict[str, Any]]],
    bucket_name: str,
    entity_id: int,
) -> None:
    """Drop a single entity from the ledger. Used after delete skills
    to keep the ledger from pointing at rows that no longer exist."""
    bucket = ledger.get(bucket_name)
    if not bucket:
        return
    ledger[bucket_name] = [b for b in bucket if b.get("id") != entity_id]


def render_for_llm(ledger: Dict[str, List[Dict[str, Any]]]) -> str:
    """Format the ledger as a system-message string. Returns empty when
    the ledger has no entries (so the caller can skip injection)."""
    if not any(ledger.get(k) for k in ledger):
        return ""
    lines = ["RECENT ENTITIES IN THIS CONVERSATION (most-recent first)"]
    # Stable order: events, drafts, tasks, contacts, documents, anything else.
    order = ["events", "drafts", "tasks", "contacts", "documents"]
    seen = set()
    for key in order + [k for k in ledger.keys() if k not in order]:
        if key in seen:
            continue
        seen.add(key)
        entries = ledger.get(key) or []
        if not entries:
            continue
        lines.append(f"{key}:")
        for e in entries:
            label = (e.get("label") or "?").strip()
            sub = (e.get("subtitle") or "").strip()
            extra = f"  ({sub})" if sub else ""
            lines.append(f"  - id={e.get('id')}  {label}{extra}")
    lines.append("")
    lines.append(
        "If the user references an entity NOT in this list, look it up "
        "via check_calendar / find_person / check_tasks / search_documents "
        "first. Do NOT guess an id from this list when the user names "
        "something that isn't here."
    )
    return "\n".join(lines)
