"""Briefing REST endpoints — list / get / run templates + the day-tab
unified endpoint that powers /r/briefing's Yesterday/Today/Tomorrow/Recap
tab bar."""

from __future__ import annotations

import time as _time
from datetime import date as _date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth_sessions import current_user
from . import briefings, briefing_snapshots

router = APIRouter(prefix="/api/briefings", tags=["briefings"])

# In-memory cache for live today/tomorrow briefings. The expensive
# bit is the LLM-backed email + whatsapp summaries (~3-8s each on
# qwen3); without this every Cmd-R re-fires both. Cache keyed by
# (user_id, period, target_date) with 5-minute TTL.
#
# Past dates are handled by briefing_snapshots (DB) and never enter
# this cache — they're already cheap to re-serve.
#
# Refresh path: the frontend's RefreshCw button passes ?force=1
# which bypasses the cache.
_DAY_CACHE: dict[tuple[int, str, str], tuple[float, dict[str, Any]]] = {}
_DAY_CACHE_TTL = 300.0


def _cache_get(key: tuple[int, str, str]) -> Optional[dict[str, Any]]:
    hit = _DAY_CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if _time.monotonic() - ts > _DAY_CACHE_TTL:
        _DAY_CACHE.pop(key, None)
        return None
    return payload


def _cache_put(key: tuple[int, str, str], payload: dict[str, Any]) -> None:
    _DAY_CACHE[key] = (_time.monotonic(), payload)


# Map UI period → template id. Single source of truth so the frontend
# stays dumb (it just sends `?period=today`). Yesterday + recap both
# resolve to day-recap (the "what happened" template) — they differ
# only in the default date the route picks (today-1 vs today). This
# also means nightly snapshots taken as day-recap can be served for
# either tab when the user navigates into the past.
_PERIOD_TO_TEMPLATE = {
    "yesterday": "day-recap",
    "today":     "day-today",
    "tomorrow":  "day-tomorrow",
    "recap":     "day-recap",
}


@router.get("")
def list_briefings(user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    """All loaded briefing templates the user can run."""
    out = []
    for t in briefings.get_all():
        out.append({
            "id":          t.id,
            "name":        t.name,
            "description": t.description,
            "tags":        t.tags,
            "vertical":    t.vertical,
            "needs_apps":  t.needs_apps,
            "author":      t.author,
            "section_count": len(t.sections),
            "synthesizes":   bool(t.synthesize and t.synthesize.get("enabled")),
        })
    return out


# ── Literal routes BEFORE the wildcard /{template_id} routes ────────
# FastAPI matches by registration order; without this, /day would be
# eaten by /{template_id} with template_id="day" and 404.

@router.get("/day")
async def day_briefing(
    period: str = Query(..., description="yesterday|today|tomorrow|recap"),
    date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today"),
    force: bool = Query(False, description="Bypass the 5-min in-memory cache"),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """Unified endpoint for the day-tab bar. Powers /r/briefing.

    `period` selects the template; `date` reframes "today" to that ISO
    date (for the date-navigator). When the user asks for a past day,
    we prefer a saved snapshot over a live re-run — past data drifts
    (emails get re-categorised, WhatsApp messages disappear) so the
    snapshot is the source of truth for "what did the day feel like".
    """
    template_id = _PERIOD_TO_TEMPLATE.get(period)
    if not template_id:
        raise HTTPException(400, f"unknown period '{period}'. Use one of: {sorted(_PERIOD_TO_TEMPLATE.keys())}")

    today = _date.today()
    if date:
        try:
            target = _date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date must be YYYY-MM-DD, got {date!r}")
    else:
        if period == "yesterday":
            target = today - timedelta(days=1)
        elif period == "tomorrow":
            target = today + timedelta(days=1)
        else:
            target = today

    target_iso = target.isoformat()

    # Snapshot-first for past dates only — today's snapshot would be a
    # partial early-morning capture, not what the user wants.
    if target < today:
        snap = briefing_snapshots.get_snapshot(template_id, target_iso)
        if snap:
            snap["period"] = period
            snap["target_date"] = target_iso
            return snap

    # 5-min cache for live (current/future) briefings keyed by user.
    # Reload-spam → no LLM re-fire. RefreshCw button passes force=1.
    cache_key = (user["id"], period, target_iso)
    if not force:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    result = await briefings.run_briefing(
        template_id,
        user_id=user["id"],
        role=user["role"],
        for_date=target_iso,
    )
    if "error" in result and "template" not in result:
        raise HTTPException(404, result["error"])
    result["period"] = period
    result["target_date"] = target_iso
    _cache_put(cache_key, result)
    return result


@router.get("/snapshots/dates")
def list_snapshot_dates(
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """Dates we have a saved snapshot for. Used by the date navigator
    to only let users walk back to dates with content."""
    return {"dates": briefing_snapshots.list_snapshot_dates()}


@router.post("/snapshots/{target_date}")
async def manual_snapshot(
    target_date: str,
    template_id: str = Query("day-recap"),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """Trigger a snapshot for a specific date now. Useful for backfill
    (capture today before midnight) or testing the scheduler path."""
    try:
        _date.fromisoformat(target_date)
    except ValueError:
        raise HTTPException(400, f"target_date must be YYYY-MM-DD")
    return await briefing_snapshots.capture_snapshot(
        template_id, target_date,
        user_id=user["id"], role=user["role"],
    )


# ── Wildcard routes (templated id) — registered LAST ───────────────

@router.get("/{template_id}")
def get_briefing(template_id: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    t = briefings.get(template_id)
    if not t:
        raise HTTPException(404, f"briefing not found: {template_id}")
    return t.to_manifest()


@router.post("/{template_id}/run")
async def run_briefing(
    template_id: str,
    window_hours: Optional[int] = Query(None, description="Override 'hours'/'days' args across all sections"),
    user: dict = Depends(current_user),
):
    result = await briefings.run_briefing(
        template_id,
        user_id=user["id"],
        role=user["role"],
        window_hours_override=window_hours,
    )
    if "error" in result and "template" not in result:
        raise HTTPException(404, result["error"])
    return result
