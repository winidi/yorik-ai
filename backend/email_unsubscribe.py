"""List-Unsubscribe / List-Unsubscribe-Post handler — RFC 2369 + RFC 8058.

Three real-world tiers, picked by analyse() in this order:

  one_click — RFC 8058: https:// target + List-Unsubscribe-Post header
              carrying "List-Unsubscribe=One-Click". The provider has
              promised the POST will work without a consent page or
              CAPTCHA. Safe to fire automatically.

  mailto    — header has a mailto:list-unsubscribe@example.com target.
              Yorik sends an empty email through the existing SMTP
              path; subject + body conventionally just say "unsubscribe".

  http      — https URL but NO List-Unsubscribe-Post (legacy 2369).
              Could be a real opt-out page, could be a CAPTCHA, could
              demand a login. Safest path is: open in the user's
              browser. analyse() returns the URL so the frontend can
              window.open() it.

  none      — no header at all. analyse() returns method='none'; the
              caller should offer the spam-block path instead.

Per RFC 8058 §3, the one-click POST body MUST be
"List-Unsubscribe=One-Click" with Content-Type
application/x-www-form-urlencoded. We send exactly that.
"""

from __future__ import annotations

import logging
import re
import urllib.request
import urllib.error
from typing import Any, Optional

log = logging.getLogger("yorik.email_unsubscribe")

# RFC 2369 / 8058 target tokenisation: targets are wrapped in <…> and
# comma-separated. We pull each angle-bracketed URI out individually.
_TARGET_RE = re.compile(r"<([^>]+)>")
_ONE_CLICK_RE = re.compile(r"\blist-unsubscribe\s*=\s*one-click\b", re.IGNORECASE)


def _split_targets(header: Optional[str]) -> list[str]:
    if not header:
        return []
    return [m.group(1).strip() for m in _TARGET_RE.finditer(header) if m.group(1).strip()]


def analyse(
    list_unsubscribe: Optional[str],
    list_unsubscribe_post: Optional[str],
) -> dict[str, Any]:
    """Decide what method to use, given the two stored header values.
    Returns {method, target, all_targets} — target is the single URL
    or mailto: to act on; all_targets is the raw list (useful for
    debugging when a sender lists multiple options)."""
    targets = _split_targets(list_unsubscribe)
    if not targets:
        return {"method": "none", "target": None, "all_targets": []}

    https_targets  = [t for t in targets if t.lower().startswith(("https://", "http://"))]
    mailto_targets = [t for t in targets if t.lower().startswith("mailto:")]

    has_one_click = bool(list_unsubscribe_post and _ONE_CLICK_RE.search(list_unsubscribe_post))

    # Prefer one-click https — it's the bulletproof path (RFC 8058).
    if has_one_click and https_targets:
        return {
            "method": "one_click",
            "target": https_targets[0],
            "all_targets": targets,
        }
    # mailto: next — Yorik can send it without leaving the inbox.
    if mailto_targets:
        return {
            "method": "mailto",
            "target": mailto_targets[0],
            "all_targets": targets,
        }
    # Legacy https — open in browser, the user finishes the click there.
    if https_targets:
        return {
            "method": "http",
            "target": https_targets[0],
            "all_targets": targets,
        }
    # Some sender shoved something we don't recognise into <…>.
    return {"method": "none", "target": None, "all_targets": targets}


def execute_one_click(target_url: str) -> dict[str, Any]:
    """Fire the RFC 8058 one-click POST. 2xx → ok. Anything else, we
    don't retry — the user can fall back to opening the URL in a tab.
    No auth, no cookies, no follow-up; this is by design — RFC 8058
    explicitly says the POST is unauthenticated and the sender's server
    is the one that has to make it idempotent."""
    body = b"List-Unsubscribe=One-Click"
    req = urllib.request.Request(
        target_url,
        data=body,
        method="POST",
        headers={
            "Content-Type":   "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
            # A plain UA — some providers reject empty UAs.
            "User-Agent":     "Yorik/1.0 (+rfc8058-one-click)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return {"ok": 200 <= int(status) < 300, "status": int(status)}
    except urllib.error.HTTPError as e:
        # Some senders treat one-click as a redirect or 202; still log
        # but only treat 2xx as a hard success.
        log.warning("one-click unsubscribe HTTP %d for %s", e.code, target_url)
        return {"ok": False, "status": int(e.code), "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        log.warning("one-click unsubscribe failed for %s: %s", target_url, e)
        return {"ok": False, "status": None, "error": str(e)}


def execute_mailto(account_id: int, mailto_target: str) -> dict[str, Any]:
    """Send the empty unsubscribe email via the existing SMTP path.
    mailto: targets can carry subject/body hints in the query string
    (RFC 6068) — we honour them when present, else use "unsubscribe"
    for both. Empty body is acceptable to most providers."""
    # Strip the "mailto:" prefix, split off any ?subject=…&body=… hint.
    raw = mailto_target[len("mailto:"):] if mailto_target.lower().startswith("mailto:") else mailto_target
    addr, _, query = raw.partition("?")
    addr = addr.strip()
    if not addr or "@" not in addr:
        return {"ok": False, "error": "mailto: target had no address"}

    subject = "unsubscribe"
    body    = "unsubscribe"
    if query:
        from urllib.parse import parse_qs, unquote
        # mailto: queries are technically NOT form-encoded — spaces are
        # %20, not +. parse_qs treats + as space, so unquote() the
        # values ourselves.
        params = parse_qs(query, keep_blank_values=True)
        if "subject" in params and params["subject"]:
            subject = unquote(params["subject"][0])
        if "body" in params and params["body"]:
            body = unquote(params["body"][0])

    from . import email_sender
    result = email_sender.send(
        account_id=account_id,
        to=[addr],
        subject=subject,
        body_text=body,
    )
    return result
