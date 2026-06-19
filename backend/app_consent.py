"""Phase E §7 — consent preflight: render a manifest v2 into a
human-readable scopes summary the frontend can display verbatim on
the install dialog.

The frontend ALSO renders its own version of the same data (the
React component owns layout). This module's job is to make sure the
backend has a single, testable source of truth for:
  * Which plain-English scope lines to show.
  * Which "this app CANNOT" lines to show in the negative-space block.
  * Whether the manifest is even installable (validation).

`preflight` takes a manifest dict and returns the structured summary.
The frontend turns it into the dialog; the backend logs it as the
exact scopes shown to the user before they clicked Install.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


def summarize_for_consent(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a v2 manifest into the structured payload the dialog renders.

    Returns:
        {
          "app": { id, name, version, author, license, homepage },
          "owned_schema": "app_notes",
          "owned_tables": ["notes", "note_attachments"],
          "scopes": [
            {"kind": "read",  "table": "contacts", "columns": [...], "purpose": "..."},
            {"kind": "skill", "skill": "find_person"},
            {"kind": "realtime", "table": "contacts"},
            ...
          ],
          "network": {
            "talks_only_to_yorik": bool,
            "outbound": [{"url": "https://api.stripe.com", "purpose": "..."}],
          },
          "cannot": [
            "Write to any of your existing Yorik data",
            "See data outside the scopes listed above",
            ...
          ],
        }
    """
    if manifest.get("manifest_version") != 2:
        # v1 manifests aren't gated by Phase E consent — they used the
        # earlier per-table grants system. Render a minimal payload so
        # the frontend can fall through.
        return {
            "app": _app_block(manifest),
            "owned_schema": None,
            "owned_tables": [],
            "scopes": [],
            "network": {"talks_only_to_yorik": True, "outbound": []},
            "cannot": [],
            "manifest_version": 1,
        }

    perms = manifest.get("permissions") or {}
    scopes: List[Dict[str, Any]] = []

    for r in perms.get("reads") or []:
        scopes.append({
            "kind": "read",
            "table": r["table"],
            "columns": r.get("columns") or [],
            "purpose": r.get("purpose") or "",
        })
    for w in perms.get("writes") or []:
        # v1 platform forbids writes — surface it so the consent
        # screen makes the rejection visible if anyone gets this far.
        scopes.append({
            "kind": "write_rejected",
            "table": w.get("table") if isinstance(w, dict) else str(w),
        })
    for sk in perms.get("invokes_skills") or []:
        scopes.append({"kind": "skill", "skill": sk})
    for c in perms.get("uses_connectors") or []:
        scopes.append({"kind": "connector", "connector": c})
    for t in perms.get("realtime_subscriptions") or []:
        scopes.append({"kind": "realtime", "table": t})
    for s in perms.get("scheduled") or []:
        scopes.append({
            "kind": "scheduled",
            "cron": s.get("cron"), "invokes": s.get("invokes"),
            "purpose": s.get("purpose") or "",
        })
    for wh in perms.get("webhooks") or []:
        scopes.append({
            "kind": "webhook",
            "path": wh.get("path"), "purpose": wh.get("purpose") or "",
        })

    outbound_raw = (manifest.get("network") or {}).get("outbound") or []
    outbound: List[Dict[str, str]] = []
    for o in outbound_raw:
        if isinstance(o, str):
            outbound.append({"url": o, "purpose": ""})
        elif isinstance(o, dict):
            outbound.append({
                "url": o.get("url", ""),
                "purpose": o.get("purpose", ""),
            })

    # Negative space: tell the user what the app CAN'T do, given the
    # specific manifest. Each line is conditional on the manifest
    # state — we don't show "cannot write" if writes are declared, etc.
    cannot: List[str] = []
    if not perms.get("writes"):
        cannot.append("Write to any of your existing Yorik data")
    cannot.append("See data outside the scopes listed above")
    cannot.append("Run background tasks on your computer")
    if not outbound:
        cannot.append("Contact external servers (no outbound network)")

    return {
        "app": _app_block(manifest),
        "owned_schema": manifest.get("owned_schema"),
        "owned_tables": manifest.get("owned_tables") or [],
        "scopes": scopes,
        "network": {
            "talks_only_to_yorik": not outbound,
            "outbound": outbound,
        },
        "cannot": cannot,
        "manifest_version": 2,
    }


def _app_block(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": manifest.get("id"),
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "author": manifest.get("author"),
        "license": manifest.get("license"),
        "homepage": manifest.get("homepage"),
        "description": manifest.get("description"),
    }


def iframe_csp_for(manifest: Dict[str, Any], *, supabase_origin: str) -> str:
    """Compute the iframe Content-Security-Policy header value.

    The browser becomes the sandbox: anything not listed in connect-src
    fails fetch() with a CSP violation. `supabase_origin` is the URL
    third-party apps hit for PostgREST/Auth/Realtime
    (e.g. "http://localhost:8400") and is always allowed.

    Outbound origins from manifest.network.outbound are added to
    connect-src so the app's declared external dependencies work.
    """
    origins = {supabase_origin.rstrip("/")}
    for o in (manifest.get("network") or {}).get("outbound") or []:
        url = o if isinstance(o, str) else o.get("url")
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
    connect_src = " ".join(sorted(origins | {"'self'"}))
    return (
        # `default-src 'self'` blocks scripts/images/anything from
        # foreign origins by default. `connect-src` opens up exactly
        # the network endpoints the manifest declared (plus Supabase).
        # `frame-ancestors 'self'` means only Yorik may embed it,
        # not a random page someone loads in another tab.
        f"default-src 'self'; "
        f"script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        f"style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: blob:; "
        f"connect-src {connect_src}; "
        f"frame-ancestors 'self';"
    )
