"""Letterheads — how a person's letters, invoices and quotes look.

One JSON document per letterhead (`letterheads.data`), a logo beside it
on disk. A person can have several (private, business); the first one is
made from their profile the moment it is asked for, so nobody starts
with an empty sheet. The layouts (layouts.py) take a letterhead and the
content and return the page — the look is never the LLM's business.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..database import conn_ctx

LOGO_DIR = Path(os.getenv("YORIK_LETTERHEAD_DIR", "data/letterheads"))
MAX_UPLOAD = 8 * 1024 * 1024
LOGO_MAX_PX = (1200, 500)

# Fonts the PDF service (Gotenberg) has installed; the browser preview
# falls back to the metric-compatible font of the same look.
FONTS: Dict[str, Dict[str, str]] = {
    "liberation-sans":  {"label": "Klar (wie Arial)",          "css": '"Liberation Sans", Arial, Helvetica, sans-serif'},
    "carlito":          {"label": "Modern (wie Calibri)",      "css": 'Carlito, Calibri, "Liberation Sans", sans-serif'},
    "noto-sans":        {"label": "Freundlich (Noto Sans)",    "css": '"Noto Sans", "Segoe UI", "Liberation Sans", sans-serif'},
    "liberation-serif": {"label": "Klassisch (wie Times)",     "css": '"Liberation Serif", "Times New Roman", Times, serif'},
    "caladea":          {"label": "Elegant (wie Cambria)",     "css": 'Caladea, Cambria, "Liberation Serif", serif'},
}
LOGO_PLACES = ("right", "left", "center")
STYLES = ("auto", "private", "business")

# field → (default, max length). Everything is text except the three below.
FIELDS: Dict[str, tuple] = {
    "sender_name": ("", 80), "business_name": ("", 120),
    "street": ("", 120), "postcode": ("", 12), "city": ("", 80), "country": ("DE", 2),
    "phone": ("", 40), "email": ("", 120), "website": ("", 120),
    "bank_name": ("", 80), "iban": ("", 42), "bic": ("", 15),
    "tax_id": ("", 40), "vat_id": ("", 20), "register": ("", 120),
    "accent": ("#1f3a5f", 7), "font": ("liberation-sans", 24), "logo_place": ("right", 8),
    "closing": ("Mit freundlichen Grüßen", 80), "signature_name": ("", 80),
    # "auto": a private letter for a person without a business name, the
    # business letterhead otherwise; "private" / "business" to choose.
    "style": ("auto", 8),
    "payment_text": ("Bitte überweisen Sie den Betrag bis zum {faellig} auf das unten genannte Konto.", 300),
    "small_business_text": ("Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.", 200),
}
BOOL_FIELDS = {"small_business": False}
INT_FIELDS = {"payment_days": (14, 0, 365)}


def clean(raw: Any, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A well-formed letterhead from whatever came in; unknown keys are
    dropped, missing ones keep `base` (or the default)."""
    raw = raw if isinstance(raw, dict) else {}
    base = base or {}
    out: Dict[str, Any] = {}
    for key, (default, limit) in FIELDS.items():
        v = raw.get(key, base.get(key, default))
        out[key] = re.sub(r"[\x00-\x08\x0b-\x1f]", "", str(v if v is not None else "")).strip()[:limit]
    for key, default in BOOL_FIELDS.items():
        out[key] = bool(raw.get(key, base.get(key, default)))
    for key, (default, lo, hi) in INT_FIELDS.items():
        try:
            out[key] = max(lo, min(hi, int(raw.get(key, base.get(key, default)))))
        except (TypeError, ValueError):
            out[key] = default
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", out["accent"]):
        out["accent"] = FIELDS["accent"][0]
    if out["font"] not in FONTS:
        out["font"] = FIELDS["font"][0]
    if out["logo_place"] not in LOGO_PLACES:
        out["logo_place"] = "right"
    if out["style"] not in STYLES:
        out["style"] = "auto"
    out["country"] = (out["country"] or "DE").upper()
    return out


def is_private(lh: Dict[str, Any]) -> bool:
    """A private letter: no letterhead block at the top and no name in the
    footer — the name stands once in the sender line above the address
    and once under the letter. For a person without a business name
    unless the letterhead says otherwise."""
    style = lh.get("style") or "auto"
    return style == "private" or (style == "auto" and not (lh.get("business_name") or "").strip())


def _from_profile(user_id: str) -> Dict[str, Any]:
    with conn_ctx() as conn:
        r = conn.execute(
            "SELECT name, first_name, last_name, email, phone, country, address_street, address_postcode, address_city, "
            "       business_name, tax_id, iban FROM user_profiles WHERE id = ?", (user_id,)).fetchone()
    if not r:
        return clean({})
    # A letter is signed with the full name; the display name ("Beate")
    # is what the household calls you, not what goes under a letter.
    full = " ".join(p for p in ((r["first_name"] or "").strip(), (r["last_name"] or "").strip()) if p) or (r["name"] or "")
    return clean({
        "sender_name": full, "signature_name": full, "business_name": r["business_name"] or "",
        "street": r["address_street"] or "", "postcode": r["address_postcode"] or "", "city": r["address_city"] or "",
        "country": r["country"] or "DE", "phone": r["phone"] or "", "email": r["email"] or "",
        "tax_id": r["tax_id"] or "", "iban": r["iban"] or "",
    })


def _row(r) -> Dict[str, Any]:
    try:
        data = json.loads(r["data"] or "{}")
    except ValueError:
        data = {}
    lid = int(r["id"])
    return {"id": lid, "name": r["name"], "is_default": bool(r["is_default"]), "data": clean(data),
            "logo_url": f"/api/letterheads/{lid}/logo?v={re.sub(r'[^0-9]', '', r['logo_at'])[-10:]}" if r["logo_at"] and logo_path(lid).exists() else None,
            "updated_at": r["updated_at"]}


def list_for(user_id: str) -> List[Dict[str, Any]]:
    with conn_ctx() as conn:
        rows = conn.execute("SELECT * FROM letterheads WHERE user_id = ? ORDER BY is_default DESC, id", (str(user_id),)).fetchall()
    return [_row(r) for r in rows]


def get(letterhead_id: int, user_id: str) -> Optional[Dict[str, Any]]:
    """Only your own: a letterhead carries bank details."""
    with conn_ctx() as conn:
        r = conn.execute("SELECT * FROM letterheads WHERE id = ? AND user_id = ?", (int(letterhead_id), str(user_id))).fetchone()
    return _row(r) if r else None


def default_for(user_id: str) -> Dict[str, Any]:
    """The person's default letterhead; made from the profile on first use."""
    mine = list_for(user_id)
    if mine:
        return mine[0]
    return create(user_id, "Standard", _from_profile(str(user_id)), make_default=True)


def create(user_id: str, name: str, data: Dict[str, Any], *, make_default: bool = False) -> Dict[str, Any]:
    with conn_ctx() as conn:
        first = conn.execute("SELECT 1 FROM letterheads WHERE user_id = ?", (str(user_id),)).fetchone() is None
        if make_default and not first:
            conn.execute("UPDATE letterheads SET is_default = 0 WHERE user_id = ?", (str(user_id),))
        r = conn.execute(
            "INSERT INTO letterheads (user_id, name, is_default, data) VALUES (?, ?, ?, ?) RETURNING id",
            (str(user_id), (name or "Standard").strip()[:60], int(make_default or first), json.dumps(clean(data), ensure_ascii=False))).fetchone()
    return get(int(r["id"]), user_id)  # type: ignore[return-value]


def update(letterhead_id: int, user_id: str, *, data: Optional[Dict[str, Any]] = None, name: Optional[str] = None,
           make_default: bool = False) -> Optional[Dict[str, Any]]:
    current = get(letterhead_id, user_id)
    if not current:
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn_ctx() as conn:
        if data is not None:
            conn.execute("UPDATE letterheads SET data = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(clean(data, current["data"]), ensure_ascii=False), now, int(letterhead_id)))
        if name is not None and name.strip():
            conn.execute("UPDATE letterheads SET name = ? WHERE id = ?", (name.strip()[:60], int(letterhead_id)))
        if make_default:
            conn.execute("UPDATE letterheads SET is_default = 0 WHERE user_id = ?", (str(user_id),))
            conn.execute("UPDATE letterheads SET is_default = 1 WHERE id = ?", (int(letterhead_id),))
    return get(letterhead_id, user_id)


def delete(letterhead_id: int, user_id: str) -> bool:
    """The last one stays: every document needs a look."""
    mine = list_for(user_id)
    target = next((l for l in mine if l["id"] == int(letterhead_id)), None)
    if not target or len(mine) < 2:
        return False
    with conn_ctx() as conn:
        conn.execute("DELETE FROM letterheads WHERE id = ?", (int(letterhead_id),))
        if target["is_default"]:
            conn.execute("UPDATE letterheads SET is_default = 1 WHERE id = (SELECT MIN(id) FROM letterheads WHERE user_id = ?)", (str(user_id),))
    logo_path(letterhead_id).unlink(missing_ok=True)
    return True


# ── logo ────────────────────────────────────────────────────────────────

def logo_path(letterhead_id: int) -> Path:
    return LOGO_DIR / f"{int(letterhead_id)}.png"


def save_logo(letterhead_id: int, user_id: str, blob: bytes) -> Optional[Dict[str, Any]]:
    """Any image becomes a PNG (transparency kept), shrunk to print size."""
    from PIL import Image, ImageOps
    if not get(letterhead_id, user_id):
        return None
    if not blob:
        raise ValueError("empty upload")
    if len(blob) > MAX_UPLOAD:
        raise ValueError("image larger than 8 MB")
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(blob)))
        img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"not an image ({type(exc).__name__})")
    img.thumbnail(LOGO_MAX_PX, Image.LANCZOS)
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = logo_path(letterhead_id).with_suffix(".part")
    img.save(tmp, "PNG", optimize=True)
    tmp.replace(logo_path(letterhead_id))
    with conn_ctx() as conn:
        conn.execute("UPDATE letterheads SET logo_at = ? WHERE id = ?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(letterhead_id)))
    return get(letterhead_id, user_id)


def remove_logo(letterhead_id: int, user_id: str) -> Optional[Dict[str, Any]]:
    if not get(letterhead_id, user_id):
        return None
    logo_path(letterhead_id).unlink(missing_ok=True)
    with conn_ctx() as conn:
        conn.execute("UPDATE letterheads SET logo_at = NULL WHERE id = ?", (int(letterhead_id),))
    return get(letterhead_id, user_id)


def logo_data_uri(letterhead_id: Optional[int]) -> Optional[str]:
    """The logo inlined: the PDF service cannot fetch from Yorik's API."""
    if not letterhead_id:
        return None
    p = logo_path(letterhead_id)
    if not p.exists():
        return None
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")
