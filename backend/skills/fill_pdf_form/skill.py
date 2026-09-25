"""fill_pdf_form — fill an attached PDF's form fields from the user's
profile and what they said.

Pipeline:
  1. Resolve the PDF from chat_attachments (must be application/pdf).
  2. Discover AcroForm fields straight from pypdf's own decoded
     get_fields() — NOT via a per-character walk of /_States_, which
     silently mangles non-ASCII option text (found 2026-09-25 while
     hand-filling a real DK-FinTS registration form: a helper script
     read "Änderung" as a single garbled character). /Tx is free text;
     /Ch and /Btn both expose their options via /_States_ and are
     treated the same way — the model must copy one option string back
     verbatim, whether it's a dropdown or a radio/checkbox widget.
  3. Pull the calling user's own profile straight from the DB — the
     same columns read_my_profile serves — instead of round-tripping
     through a second skill call.
  4. One internal text-LLM call maps {field name/type/options} +
     {profile} + {notes} -> {field: value}. Mirrors
     compose_extract_args's JSON-extraction pattern.
  5. Fill deterministically with pypdf. A field whose target value
     already equals its current value is left untouched: re-setting an
     already-correct radio/checkbox regenerates a generic appearance
     instead of reusing the PDF's own on-state artwork, turning a
     filled dot into a blank square (found the same day, same form).
  6. Render the result and ask the vision-capable LLM to sanity-check
     it before returning — the same check that caught the bug above
     when done by hand; here it runs every time, not just once.
  7. Store the filled PDF as a new, unfiled chat attachment. Nothing
     is ever sent or filed anywhere automatically.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("yorik.skills.fill_pdf_form")

MAX_CHECK_PAGES = 5
LLM_TIMEOUT_S = 180

_PROFILE_COLS = (
    "name", "first_name", "last_name", "email", "phone",
    "address_street", "address_postcode", "address_city", "country",
    "business_name", "tax_id", "iban",
)


async def execute(ctx, attachment_id: int, notes: str = "") -> dict[str, Any]:
    from backend import chat_attachments as A

    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("fill_pdf_form needs a signed-in user")

    row = A.get(int(attachment_id), str(user_id))
    if not row:
        return {"ok": False, "_llm_hint": "REJECTED: there is no attachment with this number for this user. "
                                          "Ask them to attach the file again."}
    if (row.get("mime_type") or "") != "application/pdf":
        return {"ok": False, "_llm_hint": "REJECTED: this attachment is not a PDF, nothing to fill."}

    try:
        fields = await asyncio.to_thread(_discover_fields, row["path"])
    except Exception as exc:  # noqa: BLE001
        log.exception("fill_pdf_form: field discovery failed for attachment %s", attachment_id)
        return {"ok": False, "_llm_hint": f"Could not read this PDF's structure ({type(exc).__name__}). "
                                          f"Say so in one line; the file may be corrupt or encrypted."}

    if not fields:
        return {
            "ok": False,
            "warning": "no_fillable_fields",
            "_llm_hint": "This PDF has no fillable form fields — it's likely scanned or flat. Say so in one line "
                         "and offer read_attachment to read its content instead; filling a scanned/flat form isn't "
                         "supported yet.",
        }

    profile = await asyncio.to_thread(_load_profile, user_id)
    mapping, unresolved = await _map_fields(fields, profile, notes or "")

    if not mapping:
        return {
            "ok": False,
            "warning": "nothing_mapped",
            "_llm_hint": "Could not confidently fill any field from the user's profile or their notes. Ask them "
                         "for the missing values, or call again with `notes` carrying what they tell you.",
        }

    out_bytes, filled = await asyncio.to_thread(_fill_pdf, row["path"], fields, mapping)
    if not filled:
        return {
            "ok": False,
            "warning": "already_correct",
            "_llm_hint": "Every field the model could map already had that value — nothing changed. Say so in "
                         "one line; there's nothing new to review.",
        }

    concern = await _visual_check(out_bytes)

    conv = getattr(ctx, "conversation_id", None)
    new_row = A.store(
        user_id=str(user_id),
        filename=f"ausgefuellt-{row['filename']}",
        mime_type="application/pdf",
        data=out_bytes,
        conversation_id=str(conv) if conv else None,
    )

    hint = (
        f"Filled {len(filled)} field(s): {', '.join(f['field'] for f in filled)}. The result is attachment "
        f"#{new_row['id']}; tell the user in one line and remind them to check it before sending it anywhere — "
        f"nothing was sent or filed, this is a draft for them to review."
    )
    if unresolved:
        hint += f" Still open (ask the user, or pass in `notes`): {', '.join(unresolved)}."
    if concern:
        hint += f" The visual check flagged a possible issue: {concern}. Mention this so they look closely there."

    return {
        "ok":                   True,
        "filled_attachment_id": new_row["id"],
        "fields_filled":        filled,
        "fields_unresolved":    unresolved,
        "warning":              concern or None,
        "_llm_hint":            hint,
    }


# ---------------------------------------------------------------------------
# field discovery
# ---------------------------------------------------------------------------

def _discover_fields(pdf_path: str) -> list[dict[str, Any]]:
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    raw = reader.get_fields() or {}
    fields: list[dict[str, Any]] = []
    for name, f in raw.items():
        ft = f.get("/FT")
        current = f.get("/V")
        if ft == "/Tx":
            fields.append({"field": name, "type": "text", "current": current})
        elif ft in ("/Ch", "/Btn"):
            options = list(f.get("/_States_") or [])
            if not options:
                continue
            fields.append({"field": name, "type": "choice", "options": options, "current": current})
    return fields


def _field_pages(reader) -> dict[str, int]:
    """Map field name -> 0-based page index, needed because
    update_page_form_field_values operates per page."""
    pages: dict[str, int] = {}
    for i, page in enumerate(reader.pages):
        for annot in (page.get("/Annots") or []):
            obj = annot.get_object()
            name = obj.get("/T")
            if name is None:
                parent = obj.get("/Parent")
                if parent is not None:
                    name = parent.get_object().get("/T")
            if name is not None and str(name) not in pages:
                pages[str(name)] = i
    return pages


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

def _load_profile(user_id: Any) -> dict[str, Any]:
    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_PROFILE_COLS)} FROM user_profiles WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {}
    return {k: row[k] for k in _PROFILE_COLS if row[k]}


# ---------------------------------------------------------------------------
# field -> value mapping (one internal text-LLM call)
# ---------------------------------------------------------------------------

async def _map_fields(fields: list[dict[str, Any]], profile: dict[str, Any],
                      notes: str) -> tuple[dict[str, str], list[str]]:
    from backend.agent.llm import LlmClient

    lines = [
        "You fill in a PDF form. For each field below, decide the best value from the user's "
        "profile and their notes. Only answer fields you are confident about; omit anything "
        "you cannot determine — never invent a value.",
        "",
        "For a field with OPTIONS, copy your answer EXACTLY, character for character, from one "
        "of the listed option strings. These are literal widget keys, not descriptions — a "
        "close paraphrase will be rejected.",
        "",
        "=== PROFILE ===",
    ]
    if profile:
        lines.extend(f"{k}: {v}" for k, v in profile.items())
    else:
        lines.append("(nothing on file)")
    if notes.strip():
        lines.append("\n=== USER NOTES ===")
        lines.append(notes.strip())
    lines.append("\n=== FORM FIELDS ===")
    for f in fields:
        if f["type"] == "text":
            suffix = f", current value: {f['current']!r}" if f.get("current") else ""
            lines.append(f"- {f['field']} (free text){suffix}")
        else:
            suffix = f", current value: {f['current']!r}" if f.get("current") else ""
            lines.append(f"- {f['field']} (choose exactly ONE of: {f['options']!r}){suffix}")
    lines.append(
        "\n=== OUTPUT ===\n"
        "Strict JSON only: {\"values\": {\"<field>\": \"<value>\", ...}}. "
        "No markdown, no code fences, no commentary."
    )
    prompt = "\n".join(lines)

    client = LlmClient(
        model=os.getenv("HOMEOS_MODEL", "qwen3.8-27b"),
        base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        request_timeout=LLM_TIMEOUT_S,
    )
    try:
        resp = await asyncio.to_thread(
            client.chat,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("fill_pdf_form: mapping LLM call failed: %s", exc)
        return {}, [f["field"] for f in fields]

    parsed = _parse_json(resp.get("content") or "")
    values = parsed.get("values") if isinstance(parsed, dict) else None
    values = values if isinstance(values, dict) else {}

    by_name = {f["field"]: f for f in fields}
    mapping: dict[str, str] = {}
    for name, val in values.items():
        f = by_name.get(name)
        if not f or not isinstance(val, str) or not val.strip():
            continue
        if f["type"] == "choice" and val not in f["options"]:
            log.info("fill_pdf_form: dropping %r=%r — not an exact option match", name, val)
            continue
        mapping[name] = val
    unresolved = [f["field"] for f in fields if f["field"] not in mapping]
    return mapping, unresolved


def _parse_json(raw: str) -> dict[str, Any]:
    """Tolerant JSON extraction — strips code fences, falls back to the
    first {...} block. Same approach as compose_extract_args."""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# fill
# ---------------------------------------------------------------------------

def _fill_pdf(pdf_path: str, fields: list[dict[str, Any]],
             mapping: dict[str, str]) -> tuple[bytes, list[dict[str, str]]]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(pdf_path)
    writer = PdfWriter(clone_from=reader)
    field_pages = _field_pages(reader)
    by_field = {f["field"]: f for f in fields}

    by_page: dict[int, dict[str, str]] = {}
    filled: list[dict[str, str]] = []
    for name, value in mapping.items():
        f = by_field.get(name)
        if f is None:
            continue
        current = f.get("current") or ""
        if value == current:
            continue  # already correct — never re-touch (see module docstring, point 5)
        page_idx = field_pages.get(name)
        if page_idx is None:
            continue
        by_page.setdefault(page_idx, {})[name] = value
        filled.append({"field": name, "value": value})

    for page_idx, values in by_page.items():
        writer.update_page_form_field_values(writer.pages[page_idx], values, auto_regenerate=True)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), filled


# ---------------------------------------------------------------------------
# visual self-check
# ---------------------------------------------------------------------------

async def _visual_check(pdf_bytes: bytes) -> str:
    """Render the filled PDF and ask the vision-capable LLM to spot
    anything obviously wrong. Returns '' when it looks fine, otherwise
    a one-sentence description of the concern."""
    from backend import chat_attachments as A
    from backend.agent.llm import LlmClient

    with tempfile.TemporaryDirectory(prefix="fpf-") as tmpdir:
        tmp_pdf = os.path.join(tmpdir, "filled.pdf")
        Path(tmp_pdf).write_bytes(pdf_bytes)
        pages = await asyncio.to_thread(A._render_pdf_pages, tmp_pdf, tmpdir, MAX_CHECK_PAGES)
        if not pages:
            return ""

        content: list[dict[str, Any]] = [{"type": "text", "text": (
            "This is a filled-in PDF form. Look for anything clearly wrong: a value sitting in the "
            "wrong field, a checkbox or radio button that looks unselected when it should be selected "
            "(an empty box instead of a filled dot/check), garbled text, or an obviously misplaced "
            "value. If everything looks fine, reply with exactly OK. Otherwise describe the problem in "
            "one short sentence."
        )}]
        for p in pages:
            b64 = base64.b64encode(Path(p).read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

        client = LlmClient(
            model=os.getenv("HOMEOS_MODEL", "qwen3.8-27b"),
            base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
            request_timeout=LLM_TIMEOUT_S,
        )
        try:
            result = await asyncio.to_thread(
                client.chat, [{"role": "user", "content": content}], max_tokens=200, temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("fill_pdf_form: visual check failed: %s", exc)
            return ""

    reply = (result.get("content") or "").strip()
    return "" if reply.upper().startswith("OK") else reply
