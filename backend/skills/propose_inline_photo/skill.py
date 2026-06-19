"""propose_inline_photo — visual photo picker for the Compose editor.

Calls find_photo with a sensible op (CLIP search when a query is given,
otherwise recent), grabs the top N hits, and emits a photo_picker
ui_action that the ComposeAgentChat panel renders as a thumbnail grid.
When the user clicks a thumbnail, the resume_message routes the LLM
back to compose_draft with `args.inline_image_url` set to the picked
photo's Yorik-proxy URL.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


def _yorik_proxy_url(asset_id: str) -> str:
    """Yorik's /api/photos/{id}/raw proxies the Immich `/original`
    endpoint through this server. The browser fetches it with the
    user's session cookie (no CORS issues); compose_draft fetches it
    server-side to embed bytes as a data URL for the rendered PDF."""
    return f"/api/photos/{asset_id}/raw"


async def execute(
    ctx,
    query: Optional[str] = None,
    contact_id: Optional[int] = None,
    template_id: Optional[str] = None,
    draft_id: Optional[int] = None,
    limit: int = 6,
) -> dict[str, Any]:
    # Sensible default: when no query, fall back to recent photos so
    # the user has SOMETHING to pick from. CLIP "recent" feels less
    # magical than a query-matched result but never returns empty.
    op = "search" if (query or "").strip() else "recent"

    try:
        limit_int = max(3, min(int(limit), 12))
    except (TypeError, ValueError):
        limit_int = 6

    # Reuse find_photo so we benefit from its op-auto-routing (e.g.
    # "Anna" → of_person if Anna is a labelled face in Immich) and
    # per-user credential handling. We DO NOT want find_photo's
    # `photos_found` ui_action to fire (that's the chat-app card,
    # not the picker), but the skill always emits one when photos
    # come back. We swallow it by reading + clearing the buffer.
    from backend.ui_tools import get_ui_actions, reset_ui_actions
    snapshot_before = list(get_ui_actions())

    try:
        from backend.skills.registry import get_registry
        reg = get_registry()
        result = await reg.invoke(
            "find_photo", ctx=ctx,
            query=query,
            op=op,
            take_count=limit_int,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "_llm_hint": (
                f"Couldn't search photos: {exc}. Tell the user Immich isn't "
                "reachable and ask them to either upload the photo directly "
                "into the editor (camera button in the toolbar) or skip the photo."
            ),
            "candidates": [],
            "ok": False,
        }

    # Replace the buffer so find_photo's photos_found ui_action doesn't
    # leak into the chat. The picker is the ONE card the user sees.
    after_photos_found = [
        a for a in get_ui_actions()
        if not (a.get("type") == "photos_found")
    ]
    # Restore minus the photos_found one.
    reset_ui_actions()
    from backend.ui_tools import _append as _ui_append
    for a in snapshot_before + after_photos_found:
        _ui_append(a)

    photos = result.get("photos") or []
    if not photos:
        return {
            "_llm_hint": (
                f"No photos matched query={query!r}. Ask the user if they want "
                "to try a different query (e.g. a date or person name), upload "
                "a photo directly via the camera toolbar button, or skip the photo."
            ),
            "candidates": [],
            "ok": True,
            "query": query,
        }

    # Compact candidate shape — keep the thumbnail_url Immich gave us
    # for the picker grid (it embeds the API key so the browser can
    # fetch directly). The "use this" URL is the Yorik proxy so the
    # server-side embed step works regardless of Immich reachability.
    candidates = []
    for p in photos[:limit_int]:
        pid = p.get("id")
        if not pid: continue
        candidates.append({
            "id":             pid,
            "thumbnail_url":  p.get("thumbnail_url"),  # browser preview
            "embed_url":      _yorik_proxy_url(pid),    # used by compose_draft
            "original_name":  p.get("original_name") or "",
            "taken_at":       p.get("taken_at") or "",
            "type":           p.get("type") or "IMAGE",
        })

    # Emit the picker. ComposeAgentChat intercepts photo_picker and
    # renders <PhotoPickerCard /> inline. The user clicks one →
    # frontend posts a [photo_picked] message that includes the
    # picked photo's embed_url + caption suggestion → LLM proceeds
    # to compose_draft with args.inline_image_url set.
    from backend.ui_tools import _append
    _append({
        "type":          "photo_picker",
        "source_skill":  "propose_inline_photo",
        "title":         f"Foto auswählen{(' für „' + query + '\"') if query else ''}",
        "context":       (
            f"Wähle ein Foto aus — es wird mittig in den Brief eingefügt. "
            f"Du kannst eine Beschriftung (optional) hinzufügen."
        ),
        "candidates":    candidates,
        "next_playbook_step": "compose_draft",
        "resume_skill":  "compose_draft",
        "resume_args": {
            k: v for k, v in {
                "contact_id":         contact_id,
                "template_id":        template_id,
                "existing_draft_id":  draft_id,
            }.items() if v is not None
        },
    })

    return {
        "_llm_hint": (
            f"shown_to_user: {len(candidates)} Foto-Vorschläge im Picker. "
            "Reply ONE short sentence in the user's language (e.g. "
            "'Welches Foto soll rein?'). Do NOT call compose_draft yet — "
            "wait for the [photo_picked] message, then proceed."
        ),
        "candidates":  candidates,
        "ok":          True,
        "query":       query,
    }
