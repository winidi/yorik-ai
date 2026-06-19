"""Document-series engine.

Sequential numbering for legally-numbered documents (Rechnungen, Angebote,
faktura, US invoices, …). Phase 1 of the "Compose as a Lexoffice
replacement" stack.

Design notes:
  - Two-phase allocation: `preview(series)` is non-destructive (drafts
    show the next number without burning it); `consume(series, ...)` is
    atomic + logged + irreversible. This avoids the cardinal sin of
    invoice numbering: gaps.
  - Year reset: the German convention is to restart the sequence each
    January (`R-2026-001`, `R-2026-002`, ...). When the current year
    changes from `current_year`, the next allocation resets `next_number`
    to 1 *atomically*, all under the same `BEGIN IMMEDIATE`.
  - Convention-mapping: known arg-key conventions (`rechnungsnummer`,
    `angebotsnummer`, `invoice_number`, ...) auto-match a series of the
    corresponding kind so plain templates don't need to be modified to
    benefit from numbering.
  - The audit log lives in `document_series_allocations` — every consumed
    number, with timestamp, actor, Paperless link, and PDF hash. That's
    the GoBD-tauglich trail.

Anything not in this module (the REST routes, the React picker, the
auto-fill in the renderer) just calls these public functions.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..database import conn_ctx, get_conn

log = logging.getLogger("yorik.compose.series")


# ─── Convention map: arg key → series kind ──────────────────────────────────
#
# When a template's default_args contains one of these keys (case-insensitive,
# underscores normalized), the renderer auto-fills it with the next number
# from the matching kind's default series. Add to this map carefully — it's
# the contract between hand-written templates and the numbering engine.

ARG_KEY_TO_KIND: Dict[str, str] = {
    # German
    "rechnungsnummer":       "rechnung",
    "rechnungs_nummer":      "rechnung",
    "rechnung_nummer":       "rechnung",
    "rechnung_nr":           "rechnung",
    "angebotsnummer":        "angebot",
    "angebots_nummer":       "angebot",
    "angebot_nr":            "angebot",
    "gutschriftsnummer":     "gutschrift",
    "gutschrift_nr":         "gutschrift",
    "mahnungsnummer":        "mahnung",
    "mahnung_nr":            "mahnung",
    # English / US
    "invoice_number":        "invoice",
    "invoice_no":            "invoice",
    "inv_no":                "invoice",
    "invoice_id":            "invoice",
    "quote_number":          "quote",
    "quote_no":              "quote",
    "estimate_number":       "quote",
    "receipt_number":        "receipt",
    "receipt_no":            "receipt",
    "credit_note_number":    "credit_note",
    # Polish / common EU
    "faktura_nr":            "faktura",
    "numer_faktury":         "faktura",
}


def kind_for_arg_key(key: str) -> Optional[str]:
    """Look up which series kind feeds an arg of this name. Returns None
    if the key isn't recognized — the renderer then leaves the placeholder
    untouched (no surprise side effects)."""
    norm = (key or "").lower().replace("-", "_").strip()
    return ARG_KEY_TO_KIND.get(norm)


# ─── Regional presets — used by the first-run wizard ────────────────────────
#
# Each preset is a list of series dicts the wizard creates in one click.
# Picking "Custom" skips this and shows the manual form.

REGIONAL_PRESETS: Dict[str, Dict[str, Any]] = {
    "de": {
        "label":       "🇩🇪 Germany",
        "description": "Rechnung / Angebot / Gutschrift / Mahnung — § 14 UStG-compliant numbering, restarts every January.",
        "series": [
            {"kind": "rechnung",   "name": "Rechnungen",   "scheme": "{year}-{seq}", "prefix": "",     "seq_padding": 3, "year_reset": True, "is_default": True},
            {"kind": "angebot",    "name": "Angebote",     "scheme": "A-{year}-{seq}", "prefix": "A-", "seq_padding": 3, "year_reset": True, "is_default": True},
            {"kind": "gutschrift", "name": "Gutschriften", "scheme": "G-{year}-{seq}", "prefix": "G-", "seq_padding": 3, "year_reset": True, "is_default": True},
            {"kind": "mahnung",    "name": "Mahnungen",    "scheme": "M-{year}-{seq}", "prefix": "M-", "seq_padding": 3, "year_reset": True, "is_default": True},
        ],
    },
    "us": {
        "label":       "🇺🇸 United States",
        "description": "Invoice + Quote with continuous numbering (the IRS-friendly default — sequence does NOT reset at year boundary).",
        "series": [
            {"kind": "invoice", "name": "Invoices", "scheme": "INV-{seq}", "prefix": "INV-", "seq_padding": 4, "year_reset": False, "is_default": True},
            {"kind": "quote",   "name": "Quotes",   "scheme": "Q-{seq}",   "prefix": "Q-",   "seq_padding": 4, "year_reset": False, "is_default": True},
        ],
    },
    "pl": {
        "label":       "🇵🇱 Poland",
        "description": "Faktura VAT — sequential numbering required, yearly reset is standard practice.",
        "series": [
            {"kind": "faktura", "name": "Faktury", "scheme": "FV/{year}/{seq}", "prefix": "FV/", "seq_padding": 3, "year_reset": True, "is_default": True},
        ],
    },
}


# ─── Series CRUD ────────────────────────────────────────────────────────────

def _row(r: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    d = dict(r)
    d["year_reset"] = bool(d["year_reset"])
    d["is_default"] = bool(d["is_default"])
    return d


def list_series(*, kind: Optional[str] = None, owner_user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM document_series WHERE 1=1"
    params: List[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if owner_user_id is not None:
        sql += " AND (owner_user_id IS NULL OR owner_user_id = ?)"
        params.append(owner_user_id)
    sql += " ORDER BY kind, is_default DESC, name"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [r for r in (_row(x) for x in rows) if r]


def get_series(series_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM document_series WHERE id = ?", (series_id,)).fetchone()
    return _row(row)


def default_for_kind(kind: str, *, owner_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Find the default series for a kind. Per-user series win over shared,
    and only series flagged `is_default` are eligible."""
    with get_conn() as conn:
        if owner_user_id is not None:
            row = conn.execute(
                "SELECT * FROM document_series WHERE kind = ? AND is_default = 1 "
                "AND (owner_user_id = ? OR owner_user_id IS NULL) "
                "ORDER BY (owner_user_id IS NOT NULL) DESC LIMIT 1",
                (kind, owner_user_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM document_series WHERE kind = ? AND is_default = 1 "
                "AND owner_user_id IS NULL LIMIT 1",
                (kind,),
            ).fetchone()
    return _row(row)


def create_series(
    *,
    kind: str,
    name: str,
    scheme: str = "{year}-{seq}",
    prefix: str = "",
    seq_padding: int = 3,
    starting_number: int = 1,
    year_reset: bool = True,
    is_default: bool = True,
    owner_user_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new series. `starting_number` is the FIRST number that will
    be allocated (so a business mid-2026 with 47 invoices already out
    should set this to 48). The internal `next_number` column stores that
    same "first to be allocated" value, so allocation just reads + bumps.
    """
    if starting_number < 1:
        raise ValueError("starting_number must be ≥ 1")
    if not _scheme_is_valid(scheme):
        raise ValueError(f"scheme must include {{seq}}; got '{scheme}'")
    with conn_ctx() as conn:
        # When this series is being flagged as default, clear is_default on
        # any sibling of the same kind+owner so there's only one default.
        # IS NOT DISTINCT FROM treats NULL == NULL as TRUE and works on
        # both SQLite (3.39+) and Postgres. The old COALESCE(owner_user_id,
        # -1) trick fell apart under Phase E because owner_user_id is UUID
        # on Postgres — `COALESCE(uuid, -1)` raises "COALESCE types uuid
        # and integer cannot be matched".
        if is_default:
            conn.execute(
                "UPDATE document_series SET is_default = 0 "
                "WHERE kind = ? AND owner_user_id IS NOT DISTINCT FROM ?",
                (kind, owner_user_id),
            )
        cur = conn.execute(
            "INSERT INTO document_series "
            "(kind, name, scheme, prefix, seq_padding, next_number, year_reset, is_default, owner_user_id, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, name, scheme, prefix, seq_padding, starting_number,
             1 if year_reset else 0, 1 if is_default else 0, owner_user_id, notes),
        )
        row = conn.execute("SELECT * FROM document_series WHERE id = ?", (cur.lastrowid,)).fetchone()
    out = _row(row)
    if not out:
        raise RuntimeError("series row vanished immediately after insert")
    return out


def update_series(
    series_id: int,
    *,
    name: Optional[str] = None,
    scheme: Optional[str] = None,
    prefix: Optional[str] = None,
    seq_padding: Optional[int] = None,
    next_number: Optional[int] = None,
    year_reset: Optional[bool] = None,
    is_default: Optional[bool] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Patch a series. Editing `next_number` is the "I started invoice
    numbering elsewhere and want Yorik to continue from N" lever — we
    allow it but log warnings if N is *below* the highest already-consumed
    number for this series (would cause duplicate numbers, never OK).
    """
    current = get_series(series_id)
    if not current:
        raise ValueError(f"series {series_id} not found")
    if scheme is not None and not _scheme_is_valid(scheme):
        raise ValueError(f"scheme must include {{seq}}; got '{scheme}'")
    if next_number is not None:
        max_used = _highest_used(series_id)
        if max_used is not None and next_number <= max_used:
            raise ValueError(
                f"next_number={next_number} would collide with an already-consumed "
                f"allocation (highest used is {max_used} in this series). "
                f"Set next_number > {max_used} to avoid duplicate document numbers."
            )

    fields: List[str] = []
    params: List[Any] = []
    def _set(col: str, val: Any) -> None:
        fields.append(f"{col} = ?")
        params.append(val)
    if name        is not None: _set("name", name)
    if scheme      is not None: _set("scheme", scheme)
    if prefix      is not None: _set("prefix", prefix)
    if seq_padding is not None: _set("seq_padding", seq_padding)
    if next_number is not None: _set("next_number", next_number)
    if year_reset  is not None: _set("year_reset", 1 if year_reset else 0)
    if notes       is not None: _set("notes", notes)
    if not fields and is_default is None:
        return current
    fields.append("updated_at = current_timestamp")

    with conn_ctx() as conn:
        if is_default is True:
            conn.execute(
                "UPDATE document_series SET is_default = 0 "
                "WHERE kind = ? AND owner_user_id IS NOT DISTINCT FROM ? AND id != ?",
                (current["kind"], current.get("owner_user_id"), series_id),
            )
            _set("is_default", 1)
        elif is_default is False:
            _set("is_default", 0)
        if fields:
            conn.execute(
                f"UPDATE document_series SET {', '.join(fields)} WHERE id = ?",
                (*params, series_id),
            )
    out = get_series(series_id)
    if not out:
        raise RuntimeError("series row vanished after update")
    return out


def delete_series(series_id: int) -> bool:
    """Delete only if no allocations exist — once a number's been issued,
    deleting the series would destroy the audit trail. Caller should
    'archive' (rename + flip is_default off) instead."""
    with conn_ctx() as conn:
        used = conn.execute(
            "SELECT COUNT(*) FROM document_series_allocations WHERE series_id = ?",
            (series_id,),
        ).fetchone()[0]
        if used:
            raise ValueError(
                f"cannot delete series {series_id}: {used} numbers have already been "
                f"allocated from it. Archive it (clear is_default + rename) instead so "
                f"the audit trail stays intact."
            )
        cur = conn.execute("DELETE FROM document_series WHERE id = ?", (series_id,))
        return cur.rowcount > 0


# ─── Preview / consume ──────────────────────────────────────────────────────

def _scheme_is_valid(scheme: str) -> bool:
    return "{seq}" in (scheme or "")


def _format_number(scheme: str, number: int, year: int, seq_padding: int, prefix: str) -> str:
    seq_str = str(number).zfill(max(1, seq_padding))
    return (scheme
            .replace("{prefix}", prefix or "")
            .replace("{year}", str(year))
            .replace("{seq}", seq_str))


def _highest_used(series_id: int) -> Optional[int]:
    """Highest `number` ever allocated from this series across all years."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(number) FROM document_series_allocations WHERE series_id = ?",
            (series_id,),
        ).fetchone()
    return row[0] if row and row[0] is not None else None


def preview_next(series_id: int) -> Dict[str, Any]:
    """Compute the next number string WITHOUT consuming. Used by the
    Compose draft endpoint to fill the args panel with what'll be
    allocated, and by the Save/Send dialogs to show the user."""
    s = get_series(series_id)
    if not s:
        raise ValueError(f"series {series_id} not found")
    year = datetime.now().year
    number = s["next_number"]
    if s["year_reset"] and s.get("current_year") and s["current_year"] != year:
        number = 1
    formatted = _format_number(s["scheme"], number, year, s["seq_padding"], s["prefix"])
    return {"series_id": series_id, "number": number, "year": year, "formatted": formatted}


def preview_next_for_kind(kind: str, *, owner_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    s = default_for_kind(kind, owner_user_id=owner_user_id)
    if not s:
        return None
    return preview_next(s["id"])


def consume(
    series_id: int,
    *,
    consumed_by_user_id: Optional[int] = None,
    title: Optional[str] = None,
    paperless_doc_id: Optional[int] = None,
    pdf_bytes: Optional[bytes] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically allocate the next number. Bumps `next_number`, handles
    year-rollover, inserts an audit row, returns the allocation. SQLite's
    default isolation gives us a single-writer guarantee — combined with
    the `BEGIN IMMEDIATE` we open via `conn_ctx`, two concurrent consumes
    will serialize. The UNIQUE(series_id, year, number) constraint is the
    belt to that suspender."""
    year = datetime.now().year
    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest() if pdf_bytes else None

    with conn_ctx() as conn:
        row = conn.execute("SELECT * FROM document_series WHERE id = ?", (series_id,)).fetchone()
        if not row:
            raise ValueError(f"series {series_id} not found")
        s = _row(row)
        assert s is not None
        # Year reset.
        if s["year_reset"] and s.get("current_year") and s["current_year"] != year:
            number = 1
        else:
            number = s["next_number"]
        formatted = _format_number(s["scheme"], number, year, s["seq_padding"], s["prefix"])
        # Insert the audit row FIRST — UNIQUE constraint will reject any
        # duplicate (same series, same year, same number) before we bump
        # the registry, which keeps the two tables in sync even under a
        # race.
        try:
            cur = conn.execute(
                "INSERT INTO document_series_allocations "
                "(series_id, number, formatted, year, document_kind, "
                " consumed_by_user_id, title, paperless_doc_id, pdf_sha256, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (series_id, number, formatted, year, s["kind"],
                 consumed_by_user_id, title, paperless_doc_id, pdf_hash, notes),
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                f"failed to allocate number {number} for series {series_id} year {year}: {exc}. "
                f"Concurrent consume on the same series? Retry."
            )
        alloc_id = cur.lastrowid
        conn.execute(
            "UPDATE document_series SET next_number = ?, current_year = ?, updated_at = current_timestamp "
            "WHERE id = ?",
            (number + 1, year, series_id),
        )
        row = conn.execute(
            "SELECT * FROM document_series_allocations WHERE id = ?", (alloc_id,)
        ).fetchone()

    out = dict(row) if row else {}
    log.info("series %s consumed: %s (alloc id=%s, doc=%s)",
             series_id, formatted, alloc_id, paperless_doc_id)
    return out


def list_allocations(series_id: int, *, limit: int = 50) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM document_series_allocations WHERE series_id = ? "
            "ORDER BY consumed_at DESC LIMIT ?",
            (series_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Wizard helper ──────────────────────────────────────────────────────────

def install_preset(preset_key: str, *, owner_user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Create all the series in a regional preset in one go. Skips a
    series whose kind already has a default for this owner (so re-running
    the wizard is non-destructive)."""
    preset = REGIONAL_PRESETS.get(preset_key)
    if not preset:
        raise ValueError(f"unknown preset: {preset_key}")
    out: List[Dict[str, Any]] = []
    for entry in preset["series"]:
        existing = default_for_kind(entry["kind"], owner_user_id=owner_user_id)
        if existing:
            log.info("install_preset: skipping %s (already has default for %s)",
                     preset_key, entry["kind"])
            continue
        created = create_series(
            kind=entry["kind"],
            name=entry["name"],
            scheme=entry["scheme"],
            prefix=entry.get("prefix", ""),
            seq_padding=entry.get("seq_padding", 3),
            starting_number=1,
            year_reset=entry.get("year_reset", True),
            is_default=entry.get("is_default", True),
            owner_user_id=owner_user_id,
        )
        out.append(created)
    return out
