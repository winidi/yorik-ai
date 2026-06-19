"""compute_group_price — receipt-style total for a group of people.

Deterministic math + a receipt-style chat card. LLM math is fine for
3 numbers but degrades fast on German decimal commas, so we let Python
do the addition and the LLM do the prose."""

from __future__ import annotations

from typing import Any, Optional


def _parse_amount(raw: Any) -> float:
    """Accept '5,90', '5.90', '5', 5.9, ints; return float. German LLMs
    often surface prices as strings with commas — be liberal."""
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw or "").strip()
    if not s:
        return 0.0
    # Strip currency markers + thin spaces.
    s = (s.replace("€", "").replace("EUR", "").replace(" ", " ")
           .replace(" ", "").strip())
    # German thousand separator (".") + decimal comma (",") → swap.
    if "," in s and s.count(",") == 1:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_count(raw: Any) -> int:
    if isinstance(raw, (int, float)):
        return max(0, int(raw))
    s = str(raw or "0").strip()
    try:
        return max(0, int(float(s)))
    except ValueError:
        return 0


async def execute(
    ctx,
    items: list[dict[str, Any]],
    title: Optional[str] = None,
    source_url: Optional[str] = None,
    currency: str = "EUR",
) -> dict[str, Any]:
    if not isinstance(items, list) or not items:
        return {
            "_llm_hint": (
                "compute_group_price needs at least one item "
                "(items=[{label, unit_eur, count}, ...]). Re-call with "
                "the matched price categories."
            ),
            "ok":          False,
            "total_eur":   0.0,
            "line_items":  [],
            "total_count": 0,
        }

    line_items: list[dict[str, Any]] = []
    total_eur = 0.0
    total_count = 0
    for entry in items:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip() or "(unbenannt)"
        unit = _parse_amount(entry.get("unit_eur") or entry.get("price")
                              or entry.get("amount") or 0)
        count = _parse_count(entry.get("count") or entry.get("qty") or 0)
        if count <= 0:
            continue
        subtotal = round(unit * count, 2)
        total_eur = round(total_eur + subtotal, 2)
        total_count += count
        line_items.append({
            "label":         label,
            "unit_eur":      round(unit, 2),
            "count":         count,
            "subtotal_eur":  subtotal,
        })

    # Receipt card for the chat — same pattern as compose_draft_created /
    # web_results. The frontend renders this as <PriceSummaryCard>.
    from backend.ui_tools import _append
    _append({
        "type":         "price_summary",
        "title":        title or "Eintrittspreise",
        "currency":     currency or "EUR",
        "line_items":   line_items,
        "total_eur":    total_eur,
        "total_count":  total_count,
        "source_url":   source_url,
    })

    pretty = ", ".join(
        f"{li['count']}× {li['label']} ({li['unit_eur']:.2f} €)"
        for li in line_items
    )
    hint = (
        f"shown_to_user: price summary card. Total {total_eur:.2f} € "
        f"for {total_count} Personen ({pretty}). "
        "Reply ONE short German/EN sentence acknowledging the total — "
        "the user sees the line items on the card."
        + (f" Cite the source: {source_url}." if source_url else "")
    )
    return {
        "_llm_hint":    hint,
        "ok":           True,
        "title":        title,
        "currency":     currency,
        "line_items":   line_items,
        "total_eur":    total_eur,
        "total_count":  total_count,
        "source_url":   source_url,
    }
