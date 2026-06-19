"""HTML → PDF via the local Gotenberg container.

Gotenberg runs alongside Paperless (port 9080 on the host, port 3000 inside
the docker network). For a complete invoice/letter we wrap the body HTML in
a minimal print-styled shell so margins and base typography are consistent
across all templates.

Returns raw PDF bytes the caller can serve as a download or POST onward to
Paperless via /api/documents/post_document/.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

log = logging.getLogger("homeos.compose.pdf")

GOTENBERG_URL = os.getenv("HOMEOS_GOTENBERG_URL", "http://localhost:9080").rstrip("/")
DEFAULT_MARGINS_MM = (20, 18, 25, 18)  # top, right, bottom, left
TIMEOUT_S = 60


def _wrap(body_html: str, extra_css: str = "", page_size: str = "A4",
          margins_mm=DEFAULT_MARGINS_MM) -> str:
    top, right, bottom, left = margins_mm
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<style>
  @page {{ size: {page_size}; margin: {top}mm {right}mm {bottom}mm {left}mm; }}
  body {{ font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
          font-size: 10.5pt; line-height: 1.5; color: #1a1a1a; margin: 0; }}
  h1 {{ font-size: 18pt; margin: 0 0 10pt; font-weight: 600; }}
  h2 {{ font-size: 13pt; margin: 16pt 0 8pt; font-weight: 600; }}
  h3 {{ font-size: 11pt; margin: 14pt 0 6pt; font-weight: 600; }}
  p  {{ margin: 0 0 8pt; }}
  table {{ width: 100%; border-collapse: collapse; margin: 10pt 0; font-size: 10pt; }}
  th, td {{ border: 1px solid #d4d4d8; padding: 5pt 8pt; text-align: left; vertical-align: top; }}
  th {{ background: #f4f4f5; font-weight: 600; }}
  tfoot td {{ font-weight: 600; }}
  hr {{ border: none; border-top: 1px solid #d4d4d8; margin: 12pt 0; }}
  .small {{ font-size: 9pt; color: #52525b; }}
  .right {{ text-align: right; }}
  .total {{ font-size: 12pt; font-weight: 600; }}
  /* Editor-only blocks — usage hints, "not legal advice" disclaimers,
     review checklists that help the user FILL OUT the letter but
     have no business being in the version that gets sent. Templates
     wrap such content in <div class="editor-only">…</div>. */
  .editor-only {{ display: none !important; }}
  {extra_css}
</style>
</head>
<body>
{body_html}
</body>
</html>"""


def render_pdf(body_html: str, *,
               extra_css: str = "",
               page_size: str = "A4",
               margins_mm=DEFAULT_MARGINS_MM,
               filename: str = "document.pdf") -> Optional[bytes]:
    """Send wrapped HTML to Gotenberg's chromium converter, return PDF bytes."""
    html = _wrap(body_html, extra_css=extra_css, page_size=page_size, margins_mm=margins_mm)
    try:
        r = requests.post(
            f"{GOTENBERG_URL}/forms/chromium/convert/html",
            files={"index.html": ("index.html", html, "text/html")},
            timeout=TIMEOUT_S,
        )
        if not r.ok:
            log.warning("gotenberg %s: %s", r.status_code, r.text[:200])
            return None
        return r.content
    except requests.RequestException as exc:
        log.warning("gotenberg unreachable at %s: %s", GOTENBERG_URL, exc)
        return None
