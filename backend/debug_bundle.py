"""Debug bundle — user-initiated, paste-anywhere export of one
conversation for sharing in bug reports.

Yorik is local-first; there is no telemetry pipeline. The bundle is
built on demand, redacted server-side, and returned to the caller.
What the caller does with it (paste in a GitHub issue, send to a
maintainer, drop into a chat with their own assistant) is up to them
— Yorik never sends it anywhere.

The bundle carries:
  - The full conversation (messages_json from agent_conversations,
    which is already in OpenAI shape with inline tool_calls and
    tool results — that's the LLM's actual reasoning trace)
  - Environment fingerprint: model id, git sha, Yorik version, OS,
    Python version, generated timestamp.

Redaction is best-effort regex against well-shaped tokens (emails,
phone numbers, IBANs, IPv4 addresses) plus the secrets patterns
already used by SecretsFilter. We do NOT attempt to scrub free-form
content like names, addresses-in-prose, or message text — those
become the user's responsibility to review in the modal before they
copy / send.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from .database import get_conn
from .logging_setup import _SECRET_PATTERNS  # reuse the same patterns


# ─── redaction patterns ──────────────────────────────────────────────

# Email: standard RFC-loose form. The `\b` boundaries keep us from
# eating into surrounding punctuation in JSON-escaped strings.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Phone: international `+CC NNN...` OR German leading `0NNN...`. The
# minimum-length guard (8+ digits after the lead) keeps timestamps,
# order-numbers, and short codes from being mis-classified.
_PHONE_RE = re.compile(
    r"(?<![\d\w])"
    r"(?:\+\d{1,3}[\s\-()]?\d[\d\s\-()]{7,}\d"   # +49 …
    r"|0\d[\d\s\-()]{7,}\d)"                       # 030 … / 01577 …
    r"(?![\d\w])"
)

# IBAN: country letters + 2 check digits + 11-30 alnum (covers EU
# variants). Spaces inside the IBAN are accepted because users often
# paste them grouped (DE89 3704 0044 ...).
_IBAN_RE = re.compile(
    r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b"
)

# IPv4. Doesn't validate ranges (192.168.x.x vs 0.0.0.0 etc) — anything
# four-octets-shaped is suspect enough to redact.
_IPV4_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
)


def _scrub(s: str, counts: dict[str, int]) -> str:
    """Apply every redaction pattern to one string. Mutates `counts`
    with how many substitutions of each kind fired, so the caller can
    show the user a summary."""
    def _count_sub(pat: re.Pattern, repl: str, key: str, src: str) -> str:
        out, n = pat.subn(repl, src)
        if n:
            counts[key] = counts.get(key, 0) + n
        return out

    s = _count_sub(_EMAIL_RE, "<EMAIL>", "emails", s)
    s = _count_sub(_IBAN_RE,  "<IBAN>",  "ibans", s)
    s = _count_sub(_PHONE_RE, "<PHONE>", "phones", s)
    s = _count_sub(_IPV4_RE,  "<IP>",    "ips", s)
    # Secrets patterns from SecretsFilter — api_key, bearer, bcrypt.
    # subn ignores the (key, replacement, key) compound below — we run
    # each pattern manually so we can count consistently.
    for pat, repl in _SECRET_PATTERNS:
        s, n = pat.subn(repl, s)
        if n:
            counts["secrets"] = counts.get("secrets", 0) + n
    return s


def _walk(obj: Any, counts: dict[str, int]) -> Any:
    """Recursively redact every string leaf in a JSON-shaped value.
    Lists and dicts are walked in place; everything else passes
    through unchanged."""
    if isinstance(obj, str):
        return _scrub(obj, counts)
    if isinstance(obj, list):
        return [_walk(v, counts) for v in obj]
    if isinstance(obj, dict):
        return {k: _walk(v, counts) for k, v in obj.items()}
    return obj


# ─── environment fingerprint ─────────────────────────────────────────

def _git_sha() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip() or None
    except Exception:
        return None


def _environment() -> dict[str, Any]:
    """Anything that helps a maintainer reproduce: which model, which
    Yorik commit, OS + Python. Deliberately excludes credentials,
    paths, hostnames."""
    try:
        from . import ask as _agent
        model = _agent.LLM_MODEL
    except Exception:
        model = None
    return {
        "yorik_commit":  _git_sha(),
        "model":         model,
        "python":        sys.version.split()[0],
        "platform":      platform.platform(aliased=True, terse=True),
        "generated_at":  datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
    }


# ─── builder ─────────────────────────────────────────────────────────

class BundleError(Exception):
    """Raised when the requested conversation can't be assembled."""


def build_bundle(conversation_id: str, *, user_id: str, role: str,
                 redact: bool = True) -> dict[str, Any]:
    """Assemble + (optionally) redact the bundle. Owner-gated: the
    requesting user must own the conversation OR be an admin.

    Returns:
        {conversation_id, title, created_at, updated_at, message_count,
         messages, environment, redaction: {applied, counts}}
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, user_role, title, created_at, updated_at, "
            "       messages_json "
            "FROM agent_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    if not row:
        raise BundleError(f"conversation {conversation_id!r} not found")
    owner_id = row["user_id"]
    if owner_id is not None and owner_id != user_id and role not in ("platform_admin", "admin"):
        raise BundleError("only the conversation owner or an admin can export")

    try:
        messages = json.loads(row["messages_json"] or "[]")
    except json.JSONDecodeError as e:
        raise BundleError(f"messages_json is corrupt: {e}")

    bundle: dict[str, Any] = {
        "conversation_id": row["id"],
        "title":           row["title"] or "",
        "created_at":      row["created_at"],
        "updated_at":      row["updated_at"],
        "message_count":   len(messages),
        "messages":        messages,
        "environment":     _environment(),
    }

    counts: dict[str, int] = {}
    if redact:
        bundle["messages"] = _walk(bundle["messages"], counts)

    bundle["redaction"] = {
        "applied":     redact,
        "counts":      counts,
        "notes": (
            "Auto-redaction strips well-shaped tokens (emails, phone numbers, "
            "IBANs, IPv4 addresses, api keys / passwords / bearer tokens / "
            "bcrypt hashes). It does NOT strip names, free-text addresses, "
            "or message content. Review the bundle before sharing it."
        ),
    }
    return bundle
