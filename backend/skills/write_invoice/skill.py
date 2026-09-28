"""write_invoice — a draft invoice or quote in the Schreiben app. The
model brings customer and line items, the code the sums, the look and
later the number; what is missing is marked on the sheet."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


async def execute(ctx, kind: str, customer: str, lines: List[Dict[str, Any]], subject: Optional[str] = None,
                  intro: Optional[str] = None, service_from: Optional[str] = None, service_to: Optional[str] = None,
                  document_id: Optional[int] = None, small_business: Optional[bool] = None) -> Dict[str, Any]:
    from backend.writing import invoice as inv, letterhead as lh_mod, recipient as rcp_mod, routes, store
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("write_invoice needs a signed-in user")
    uid, role = str(user_id), getattr(ctx, "role", None)
    kind = {"rechnung": "invoice", "angebot": "quote", "offer": "quote"}.get((kind or "").strip().lower(), (kind or "").strip().lower())
    if kind not in ("invoice", "quote"):
        raise ValueError("kind must be invoice or quote")
    if kind not in store.kinds_for(role):
        raise ValueError("this account writes letters only")
    if isinstance(lines, dict):
        lines = [lines]
    content = routes._clean_content(kind, {"subject": subject, "intro": intro, "lines": lines if isinstance(lines, list) else [],
                                           "service_from": service_from, "service_to": service_to})

    lh = lh_mod.default_for(uid)
    if isinstance(small_business, bool):
        # the answer to the VAT question, kept for every later invoice
        lh = lh_mod.update(lh["id"], uid, data={**lh["data"], "small_business": small_business,
                                                "vat_confirmed": True}) or lh
    doc = store.get(int(document_id), uid) if document_id else None
    if doc and doc["status"] == "draft" and doc["kind"] == kind:
        same = (customer or "").strip().lower() == (doc["recipient"].get("name") or "").lower()
        kept = {k: v for k, v in doc["content"].items() if k not in content or not content[k]}
        doc = store.update(doc["id"], uid, title=content["subject"] or doc["title"], content={**kept, **{k: v for k, v in content.items() if v}},
                           recipient=None if same else rcp_mod.resolve(customer, role=role, user_id=uid, sender_country=lh["data"]["country"]))
    else:
        rcp = rcp_mod.resolve(customer, role=role, user_id=uid, sender_country=lh["data"]["country"])
        doc = store.create(uid, kind, title=content["subject"], recipient=rcp, content=content, letterhead_id=lh["id"])

    data = lh["data"]
    missing = inv.missing(kind, data, doc["recipient"], doc["content"])
    calc = inv.compute(doc["content"].get("lines"), small_business=data["small_business"],
                       default_vat=doc["content"].get("vat_percent", "19"))["totals"]
    # "8,55 % MwSt" when only the amount was given (2026-09-28): the rate too
    rates = sorted({f"{r['rate']:g} %".replace(".", ",") for r in calc.get("vat_rows") or [] if r["vat"]})
    money = lambda v: inv.money(v, data["country"])
    total, net, vat = money(calc["gross"]), money(calc["net"]), money(calc["vat"])
    vat_charged = bool(calc["vat"])
    label = "Rechnung" if kind == "invoice" else "Angebot"
    n_lines = len(doc["content"].get("lines") or [])
    # The breakdown stands on the card, computed here (2026-09-28).
    vat_rate = " / ".join(rates)
    preview = (f"{n_lines} Positionen · netto {net} + {vat_rate} MwSt {vat} = {total}" if vat_charged
               else f"{n_lines} Positionen · {total}" + (" (ohne MwSt, § 19 UStG)" if data["small_business"] else ""))
    from backend.ui_tools import _append
    _append({"type": "writing_draft_created", "document_id": doc["id"], "kind": kind, "recipient": doc["recipient"].get("name") or "",
             "subject": doc["title"] or label, "preview": preview, "missing": missing})
    ask_vat = not data.get("vat_confirmed")
    out = {"document_id": doc["id"], "net": net, "vat": vat, **({"vat_rate": vat_rate} if vat_charged else {}),
           "total": total, "missing": missing,
           "_llm_hint": (f"shown_to_user: the draft {label} (document_id={doc['id']}, total {total}, computed by the app) is on a card the user can open. "
                         + (f"Still marked as missing on the sheet: {', '.join(missing)}; mention it in half a sentence, do not ask for it. " if missing else "")
                         + "No number has been taken; the user finalises it in the app. Answer in one short sentence in the user's language. "
                         # both sentences approved by Dirk 2026-09-28
                         + ("Name net, VAT and total in one sentence when VAT is charged. " if vat_charged else "")
                         + ("Ask once whether they charge VAT or are a small business (§ 19 UStG, no VAT); "
                            "pass the answer as small_business on the next call. " if ask_vat else "")
                         + f"For changes call write_invoice again with document_id={doc['id']} and the complete list of lines.")}
    if ask_vat:
        out["vat_question"] = True
    return out
