"""Readable text from an HTML mail body.

Until 2026-10-05 a mail without a text part had its tags stripped with
a regex, which kept everything inside <style>: 1974 of 8118 mails (24 %)
carried CSS instead of words in the search index, and "snooze" never
found the Snuzone order confirmation (search test set). This drops the
parts no reader sees (style, script, head, comments — Outlook's
`<!--[if mso]>` blocks and `.ExternalClass` rules live there), keeps
line breaks where the layout had them (paragraphs, table rows, list
items), and gives a link's text rather than its tracking URL.
"""

from __future__ import annotations

import html as _html
import re
from typing import Optional

_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol",
          "blockquote", "pre", "section", "article", "header", "footer", "hr", "dd", "dt", "address"}
_DROP = {"style", "script", "head", "title", "noscript", "template", "iframe", "svg", "object"}
_CELL = {"td", "th"}

_CSS_RULE = re.compile(r"(?s)/\*.*?\*/|[.#@]?[\w\s,.:>\-\[\]=\"'()]*\{[^{}]*\}")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_TAG_BLOCK = re.compile(r"(?is)<(style|script|head|title|noscript|template)\b[^>]*>.*?</\1\s*>")
_TAG = re.compile(r"(?s)<[^>]+>")


def _fallback(src: str) -> str:
    """No lxml: regex, but the blocks nobody reads go first."""
    text = _COMMENT.sub(" ", src)
    text = _TAG_BLOCK.sub(" ", text)
    text = re.sub(r"(?i)<(?:br|/p|/div|/tr|/li|/h[1-6])\b[^>]*>", "\n", text)
    text = _TAG.sub(" ", text)
    return _tidy(_html.unescape(text))


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\r", "")
    # preheader padding: zero-width and joiner characters by the hundred
    text = re.sub(r"[\u200b-\u200f\u2060-\u2064\u034f\ufeff\u00ad]", "", text)
    text = _CSS_RULE.sub(" ", text) if "{" in text and ("}" in text) else text
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(src: Optional[str]) -> str:
    """The words of an HTML document, in reading order, one line per
    block. Empty for nothing."""
    if not src or not src.strip():
        return ""
    if "<" not in src:
        return _tidy(_html.unescape(src))
    try:
        import lxml.html
        from lxml import etree
    except ImportError:  # pragma: no cover — lxml ships with the app
        return _fallback(src)
    try:
        root = lxml.html.fromstring(src)
    except (etree.ParserError, ValueError, TypeError):
        return _fallback(src)
    parts: list[str] = []

    def walk(el) -> None:
        tag = el.tag if isinstance(el.tag, str) else None   # comments, processing instructions
        if tag is None:
            if el.tail:
                parts.append(el.tail)
            return
        tag = tag.lower()
        if tag in _DROP:
            if el.tail:
                parts.append(el.tail)
            return
        if tag in _BLOCK:
            parts.append("\n")
        elif tag in _CELL:
            parts.append(" ")
        if tag == "a" and el.get("href") and not (el.text_content() or "").strip():
            pass            # an image link: nothing to read
        if el.text:
            parts.append(el.text)
        for child in el:
            walk(child)
        if tag in _BLOCK:
            parts.append("\n")
        if el.tail:
            parts.append(el.tail)

    walk(root)
    return _tidy("".join(parts))


def looks_like_markup(text: Optional[str]) -> bool:
    """Stored text that is CSS or HTML rather than words — what the
    repair (scripts/repair_email_text.py) and the index health check
    look for."""
    if not text:
        return False
    return bool(re.search(r"/\*|!important|\.ExternalClass|#outlook|\{[^}]{0,80}:[^}]{0,80};|<!\[endif\]|<\w+[^>]*>", text))
