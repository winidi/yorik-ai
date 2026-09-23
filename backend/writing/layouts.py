"""The three layouts — letter, invoice, quote — as code. They take a
letterhead and the content and return the page twice over: `html` plus
`footer_html` for the PDF service (the footer repeats on every page),
or, with preview=True, one self-contained page that draws the sheet on
screen. The content never brings its own look: a letter's text is
sanitised HTML from the editor, an invoice is data.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import invoice as inv
from .letterhead import FONTS, clean as clean_letterhead, is_private

KINDS = ("letter", "invoice", "quote")
_env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "layouts")),
                   autoescape=select_autoescape(enabled_extensions=("html.j2",), default=True),
                   trim_blocks=True, lstrip_blocks=True)


def sanitise(html: Any) -> str:
    """Editor HTML without scripts, styles, frames, event handlers or
    remote images: what is left is structure."""
    text = str(html or "").strip()
    if not text:
        return ""
    from lxml_html_clean import Cleaner
    cleaner = Cleaner(scripts=True, javascript=True, style=True, inline_style=True, links=True, meta=True,
                      page_structure=True, embedded=True, frames=True, forms=True, annoying_tags=True,
                      remove_unknown_tags=True, safe_attrs_only=True, add_nofollow=False,
                      kill_tags=["img", "svg", "video", "audio"])
    try:
        out = cleaner.clean_html(f"<div>{text}</div>")
    except Exception:  # noqa: BLE001 — unparseable input becomes plain text
        from markupsafe import escape
        return f"<p>{escape(text)}</p>"
    return out[5:-6] if out.startswith("<div>") and out.endswith("</div>") else out


def text_to_html(text: Any) -> str:
    """Plain text (what an LLM or a person types) → paragraphs."""
    from markupsafe import escape
    blocks = [b.strip() for b in str(text or "").replace("\r\n", "\n").split("\n\n")]
    return "".join(f"<p>{str(escape(b)).replace(chr(10), '<br>')}</p>" for b in blocks if b)


def _date_text(value: Any, country: str) -> str:
    if not value:
        return ""
    try:
        d = value if isinstance(value, date) else datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return str(value)
    return d.strftime("%d.%m.%Y") if country in ("DE", "AT", "CH") else d.isoformat()


def render(kind: str, letterhead: Dict[str, Any], recipient: Optional[Dict[str, Any]], content: Optional[Dict[str, Any]], *,
           logo: Optional[str] = None, preview: bool = False, today: Optional[date] = None) -> Dict[str, str]:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    lh = clean_letterhead(letterhead)
    country = lh["country"]
    content = dict(content or {})
    recipient = recipient or {}
    today = today or date.today()
    doc_date = content.get("date") or today.isoformat()
    to = {"name": str(recipient.get("name") or "").strip(),
          "address_lines": [str(x).strip() for x in (recipient.get("address_lines") or []) if str(x).strip()][:5]}
    info: List[tuple] = []
    c: Dict[str, Any] = {"subject": str(content.get("subject") or "").strip()[:200]}

    if kind == "letter":
        c["text_html"] = sanitise(content.get("text_html")) or text_to_html(content.get("text"))
        # a text that already signs off gets no second closing
        c["add_closing"] = bool(content.get("add_closing", False))
        info = [("Datum", _date_text(doc_date, country)), ("Ihr Zeichen", content.get("your_ref")), ("Unser Zeichen", content.get("our_ref"))]
        title = c["subject"] or "Brief"
    else:
        calc = inv.compute(content.get("lines"), small_business=lh["small_business"], default_vat=content.get("vat_percent", "19"))
        c["lines"] = [{**l, "qty_text": inv.number(l["qty"], country), "unit_price_text": inv.money(l["unit_price"], country),
                       "net_text": inv.money(l["net"], country)} for l in calc["lines"]]
        t = calc["totals"]
        c["totals"] = {"net_text": inv.money(t["net"], country), "gross_text": inv.money(t["gross"], country),
                       "vat_rows": [{"rate_text": inv.number(v["rate"], country), "vat_text": inv.money(v["vat"], country)} for v in t["vat_rows"]]}
        c["number"] = str(content.get("number") or "").strip()
        c["intro_html"] = sanitise(content.get("intro_html")) or text_to_html(content.get("intro"))
        c["closing_html"] = sanitise(content.get("closing_html")) or text_to_html(content.get("closing"))
        try:
            base = datetime.fromisoformat(str(doc_date)[:10]).date()
        except ValueError:
            base = today
        due = content.get("due_date") or (base + timedelta(days=lh["payment_days"])).isoformat()
        # nothing to pay, nothing to ask for
        c["payment_text"] = "" if t["gross"] <= 0 else lh["payment_text"].replace("{faellig}", _date_text(due, country)).replace("{betrag}", c["totals"]["gross_text"])
        c["valid_until_text"] = _date_text(content.get("valid_until") or (base + timedelta(days=30)).isoformat(), country)
        service = _date_text(content.get("service_from"), country)
        if content.get("service_to") and content.get("service_to") != content.get("service_from"):
            service = f"{service} – {_date_text(content.get('service_to'), country)}" if service else _date_text(content.get("service_to"), country)
        info = [("Angebotsnr." if kind == "quote" else "Rechnungsnr.", c["number"] or ("Entwurf" if kind == "invoice" else "")),
                ("Datum", _date_text(doc_date, country)), ("Kundennr.", content.get("customer_no")),
                (("Leistungszeitraum" if "–" in service else "Leistungsdatum") if kind == "invoice" else "", service if kind == "invoice" else ""),
                ("Fällig am" if kind == "invoice" and t["gross"] > 0 else "", _date_text(due, country) if kind == "invoice" else "")]
        title = f"{'Angebot' if kind == 'quote' else 'Rechnung'} {c['number']}".strip()

    font_css = FONTS[lh["font"]]["css"]
    # The stylesheet holds nothing a person typed freely: accent, font and
    # logo place are validated in letterhead.clean(), so it may go in unescaped.
    from markupsafe import Markup
    css = Markup(_env.get_template("base.css.j2").render(lh=lh, font_css=font_css, preview=preview))
    template = _env.get_template("letter.html.j2" if kind == "letter" else "invoice.html.j2")
    # Invoices and quotes always carry the business head; a letter from a
    # person is a private letter (see letterhead.is_private).
    private = kind == "letter" and is_private(lh)
    html = template.render(kind=kind, lh=lh, to=to, c=c, info=[(a, b) for a, b in info if a], css=css, logo=logo,
                           preview=preview, title=title, private=private)
    cols = _env.get_template("_footer_cols.html.j2").render(lh=lh, private=private)
    footer = _env.get_template("footer.html.j2").render(lh=lh, font_css=font_css, cols=cols)
    return {"html": html, "footer_html": footer, "title": title}


def email_body(letterhead: Dict[str, Any], content: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """A letter as the text of a mail instead of a PDF: the letter's own
    text, the closing and the name once — no letterhead, no address
    block, no footer. Returns {"text", "html"}."""
    import html as _html
    lh = clean_letterhead(letterhead)
    content = dict(content or {})
    body = sanitise(content.get("text_html")) or text_to_html(content.get("text"))
    name = lh["signature_name"] or lh["sender_name"]
    if content.get("add_closing", False):
        body += f"<p>{_html.escape(lh['closing'])}<br>{_html.escape(name)}</p>"
    text = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.I)
    text = _html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return {"text": text, "html": f"<div style=\"font-family: sans-serif; font-size: 14px; line-height: 1.5\">{body}</div>"}


def sample_content(kind: str) -> Dict[str, Any]:
    """What the letterhead preview in the settings shows."""
    if kind == "letter":
        return {"subject": "Ihr Schreiben vom 3. September", "add_closing": True,
                "text": "Sehr geehrte Frau Beispiel,\n\nvielen Dank für Ihre Nachricht. So sieht ein Brief mit Ihrem Briefpapier aus: "
                        "oben Ihr Name oder Logo, darunter das Adressfeld für den Fensterumschlag, rechts das Datum.\n\n"
                        "Den Text schreiben Sie selbst oder lassen ihn von Yorik schreiben — das Aussehen bleibt immer dieses."}
    return {"number": "2026-014" if kind == "invoice" else "A-2026-007", "subject": "Wartung Heizungsanlage", "customer_no": "K-1042",
            "service_from": "2026-09-01", "service_to": "2026-09-12", "intro": "vielen Dank für Ihren Auftrag. Wir berechnen wie folgt:" if kind == "invoice" else "gern bieten wir Ihnen an:",
            "lines": [{"text": "Wartung Heizungsanlage\nInspektion, Reinigung, Funktionsprüfung", "qty": 1, "unit": "pauschal", "unit_price": "180"},
                      {"text": "Ersatzteil Umwälzpumpe", "qty": 1, "unit": "Stk.", "unit_price": "129,90"},
                      {"text": "Arbeitszeit Monteur", "qty": "2,5", "unit": "Std.", "unit_price": "68"}]}


SAMPLE_RECIPIENT = {"name": "Erika Beispiel", "address_lines": ["Musterstraße 12", "12345 Beispielstadt"]}
