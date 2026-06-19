"""yorik_help — return Yorik's own how-to docs for a topic.

The corpus lives at `docs/help/*.md`. Each file has YAML frontmatter
(`title`, optional `nav_app` / `nav_query`, `summary`) and a markdown
body. This skill loads them once, keys by topic id (filename with the
`NN-` prefix stripped, `.md` removed), and returns the body verbatim
plus a navigation hint.

The agent calls this whenever the user asks how Yorik works or how to
set something up — instead of guessing. Curated truth beats freestyle
hallucination, and the corpus stays in lockstep with what the app
actually ships.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger("yorik.yorik_help")

# docs/help/ sits next to backend/ — three parents up from this file.
_DOCS_DIR = Path(__file__).resolve().parents[3] / "docs" / "help"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_TOPIC_FROM_FILENAME = re.compile(r"^(?:\d+-)?(.+)\.md$")

# Loaded lazily on first call, kept in module memory after that.
_CORPUS: dict[str, dict[str, Any]] | None = None


def _load_corpus() -> dict[str, dict[str, Any]]:
    """Walk docs/help/*.md and parse frontmatter + body for each file.

    Returns a dict keyed by topic id. README.md and any non-numbered
    files except the topic stubs are skipped.
    """
    global _CORPUS
    if _CORPUS is not None:
        return _CORPUS

    out: dict[str, dict[str, Any]] = {}
    if not _DOCS_DIR.is_dir():
        log.warning("docs/help missing at %s — yorik_help will return empty", _DOCS_DIR)
        _CORPUS = out
        return out

    for path in sorted(_DOCS_DIR.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        m = _TOPIC_FROM_FILENAME.match(path.name)
        if not m:
            continue
        topic = m.group(1).lower()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)
            continue
        fm: dict[str, Any] = {}
        body = text
        fm_match = _FRONTMATTER_RE.match(text)
        if fm_match:
            try:
                fm = yaml.safe_load(fm_match.group(1)) or {}
            except yaml.YAMLError as exc:
                log.warning("bad YAML in %s: %s", path, exc)
                fm = {}
            body = fm_match.group(2)
        out[topic] = {
            "topic":     topic,
            "title":     fm.get("title") or topic.replace("-", " ").title(),
            "summary":   fm.get("summary") or "",
            "nav_app":   fm.get("nav_app") or "",
            "nav_query": fm.get("nav_query") or {},
            "body":      body.strip(),
            "filename":  path.name,
        }
    _CORPUS = out
    log.info("yorik_help loaded %d topic(s) from %s", len(out), _DOCS_DIR)
    return out


# Token-overlap keywords per topic — used when the LLM passes `query`
# without a topic. Keep tight — better to return "no match" and let the
# LLM pick from available_topics than to silently route to the wrong doc.
_TOPIC_KEYWORDS = {
    "first-run":       ["start", "first", "begin", "anfang", "anfangen", "neu", "install", "installation", "setup", "onboarding", "loslegen", "erst", "profil"],
    "llm-setup":       ["llm", "ki", "ai", "model", "modell", "ollama", "qwen", "openai", "api", "key", "endpoint", "schluessel", "schlüssel", "anthropic", "cloud", "gpt", "claude", "mistral"],
    "paperless":       ["paperless", "document", "documents", "dokument", "dokumente", "scan", "ocr", "pdf", "rechnung scan", "post"],
    "immich":          ["immich", "photo", "photos", "picture", "pictures", "foto", "fotos", "bild", "bilder", "camera", "kamera", "kamerarolle", "cameraroll", "face", "gesicht", "upload", "hochladen", "sync", "autobackup", "auto-backup"],
    "tailscale":       ["tailscale", "vpn", "remote", "phone", "handy", "unterwegs", "ausserhalb", "zugriff", "extern", "ssh"],
    "voice":           ["voice", "stimme", "sprach", "whisper", "dictation", "diktat", "mikro", "microphone", "fab", "sprechen", "spracheingabe"],
    "whatsapp":        ["whatsapp", "wa", "messenger", "qr", "bridge", "nachrichten"],
    "email":           ["email", "e-mail", "mail", "imap", "smtp", "gmail", "outlook", "icloud", "postfach"],
    "compose":         ["compose", "brief", "schreiben", "letter", "invoice", "rechnung", "offer", "angebot", "template", "vorlage", "kündig", "kundig"],
    "contacts":        ["contact", "contacts", "kontakt", "kontakte", "address book", "adressbuch", "vcard", "vcf"],
    "calendar":        ["calendar", "kalender", "event", "termin", "appointment", "meeting", "besprechung"],
    "tasks":           ["task", "tasks", "todo", "to-do", "aufgabe", "aufgaben", "erinnerung"],
    "briefing":        ["briefing", "tagesplan", "tagesübersicht", "tagesuebersicht", "morgen", "morning", "daily", "summary", "übersicht", "uebersicht"],
    "themes":          ["theme", "themes", "dark mode", "darkmode", "design", "farben", "color", "colour", "aussehen", "look"],
    "extensions":      ["extension", "extensions", "addon", "add-on", "erweiterung", "zugferd", "factur", "plugin"],
    "troubleshooting": ["error", "fehler", "broken", "kaputt", "geht nicht", "doesnt work", "doesn't work", "log", "debug", "problem", "issue", "stuck", "hängt"],
}


def _rank_topics(query: str, corpus: dict[str, dict[str, Any]]) -> list[tuple[str, int]]:
    """Score each topic by keyword overlap with the query. Returns
    (topic, score) sorted by score desc; only positive scores included."""
    q = (query or "").strip().lower()
    if not q:
        return []
    tokens = [t for t in re.split(r"[\s,;.!?/\\()\[\]\"']+", q) if t and len(t) > 1]
    scored: list[tuple[str, int]] = []
    for topic in corpus:
        kws = _TOPIC_KEYWORDS.get(topic, [])
        score = 0
        for tok in tokens:
            for kw in kws:
                # Match on equality or "token is a plural/conjugated form
                # of keyword" (tok starts with kw, ≤3 extra chars).
                # Asymmetric on purpose: "fotos" → "foto" should hit,
                # but "brief" → "briefing" should not. Without the length
                # cap, every short keyword swallows everything starting
                # with it.
                if tok == kw:
                    score += 1
                    break
                if tok.startswith(kw) and len(tok) - len(kw) <= 3:
                    score += 1
                    break
        if score > 0:
            scored.append((topic, score))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored


async def execute(
    ctx,  # noqa: ARG001
    topic: Optional[str] = None,
    query: Optional[str] = None,
) -> dict[str, Any]:
    corpus = _load_corpus()
    available = sorted(corpus.keys())

    if not corpus:
        return {
            "_llm_hint": (
                "No help docs found on disk. Tell the user the help corpus "
                "isn't installed, and offer general guidance from memory."
            ),
            "available_topics": [],
        }

    key = (topic or "").strip().lower()
    # Strip an .md suffix if the LLM pasted a filename.
    if key.endswith(".md"):
        key = key[:-3]
    # Strip a leading NN- prefix if the LLM pasted the filename verbatim.
    m = re.match(r"^\d+-(.+)$", key)
    if m:
        key = m.group(1)

    doc = corpus.get(key) if key else None

    if doc is None and (query or "").strip():
        ranked = _rank_topics(query or "", corpus)
        if ranked:
            best_topic, _score = ranked[0]
            doc = corpus[best_topic]

    if doc is None:
        # No topic resolved — return the index so the LLM can pick.
        summaries = [
            {"topic": t, "title": corpus[t]["title"], "summary": corpus[t]["summary"]}
            for t in available
        ]
        return {
            "available_topics": summaries,
            "_llm_hint": (
                f"No topic matched (asked={topic!r}, query={query!r}). "
                f"Pick the closest from available_topics and call yorik_help "
                f"again with that `topic`. If genuinely none fit, tell the "
                f"user the corpus doesn't cover this yet."
            ),
        }

    nav_app = doc.get("nav_app") or ""
    nav_query = doc.get("nav_query") or {}

    # Emit a click-through card instead of auto-navigating. Users read
    # the help answer in chat; ripping them to another screen mid-read
    # is jarring. The card renders as a button so they jump on demand.
    button_hint = ""
    if nav_app:
        path = _resolve_path(nav_app, nav_query)
        if path:
            from backend.ui_tools import _append
            _append({
                "type":   "help_open_app",
                "app":    nav_app,
                "path":   path,
                "topic":  doc["topic"],
            })
            button_hint = (
                f" A button to open the {nav_app!r} screen is shown under "
                f"your reply — DO NOT also call navigate_to (that would yank "
                f"the user away mid-read). DO NOT mention the button in your "
                f"reply; the user sees it on their own."
            )

    return {
        "topic":      doc["topic"],
        "title":      doc["title"],
        "body":       doc["body"],
        "nav_app":    nav_app,
        "nav_query":  nav_query,
        "_llm_hint": (
            f"shown_to_user:help text + open-app button. "
            f"Quote the 2-5 most relevant sentences from the body in the "
            f"user's language; do NOT paste the whole body."
            + button_hint
        ),
    }


def _resolve_path(app: str, query_params: dict[str, Any] | None) -> str:
    """Build the React-router path for an app + optional query params,
    reusing navigate_to's friendly-name → /r/* map. Returns "" if the
    app key is unknown (the card is skipped in that case)."""
    try:
        from backend.skills.navigate_to.skill import _APP_ROUTES
    except Exception:
        return ""
    key = (app or "").strip().lower()
    route = _APP_ROUTES.get(key)
    if not route:
        for variant in (key.rstrip("s"), key + "s"):
            route = _APP_ROUTES.get(variant)
            if route:
                break
    if not route:
        return ""
    if query_params:
        from urllib.parse import urlencode
        clean = {k: str(v) for k, v in query_params.items() if v is not None}
        if clean:
            return f"{route}?{urlencode(clean)}"
    return route
