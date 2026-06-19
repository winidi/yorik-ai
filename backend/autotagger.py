"""LLM-driven Paperless autotagger.

Walks every Paperless document and PATCHes it with 0–3 tags picked
from the curated taxonomy in `tag_taxonomy.yaml`. The LLM never
invents tags — it picks from a closed list, returning an empty array
when nothing fits. Tags are created in Paperless on first use (German
label as the canonical name) and merged with whatever tags the doc
already has (no destructive writes).

Safety guarantees:
  - Never calls DELETE on a Paperless document. Period.
  - Only PATCHes the `tags` field, set-merged with existing tags so
    user-added tags survive.
  - Skips docs that already carry any taxonomy tag (idempotent re-run),
    unless force_retag=True.

Cost: ~2–3 s per doc on a local 7B/9B LLM, so a 2500-doc corpus is
~1.5h of background time. Progress is reported via the workers
heartbeat ("autotagger" worker) so Settings → Embeddings shows live
"X / Y done" status.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

from . import workers
from .connectors.paperless import _settings as _paperless_settings

log = logging.getLogger("yorik.autotagger")

TAXONOMY_PATH = Path(__file__).with_name("tag_taxonomy.yaml")
PAPERLESS_TIMEOUT = 15
CONTENT_SAMPLE_CHARS = 1500  # chars of content fed to the LLM per doc

# ─── Taxonomy loading ─────────────────────────────────────────────────

_taxonomy_cache: Optional[Dict[str, Any]] = None

# Cooperative cancel flag. Set by the cancel endpoint; checked by the
# walk loop once per doc. Thread-safe enough for the single-batch
# pattern (one autotag job at a time; FastAPI guards re-entry by
# convention since the user can only click the button once).
_cancel_requested: bool = False


def request_cancel() -> None:
    """Tell any running autotag_all batch to stop after the current
    document. No-op if nothing is running."""
    global _cancel_requested
    _cancel_requested = True


def _reset_cancel() -> None:
    global _cancel_requested
    _cancel_requested = False


def is_cancel_requested() -> bool:
    return _cancel_requested


def load_taxonomy() -> Dict[str, Any]:
    """Parse tag_taxonomy.yaml. Cached process-wide so the file is read
    once per startup; tests can clear via load_taxonomy.cache_clear()."""
    global _taxonomy_cache
    if _taxonomy_cache is None:
        with open(TAXONOMY_PATH, "r", encoding="utf-8") as f:
            _taxonomy_cache = yaml.safe_load(f)
    return _taxonomy_cache


def all_tags(taxonomy: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten taxonomy → list of {id, de, en, category_id, category_de, color}."""
    out = []
    for cat in taxonomy.get("categories", []):
        cat_id = cat["id"]
        cat_label_de = cat["label"]["de"]
        cat_color = cat.get("color")  # may be None for older taxonomies
        for t in cat.get("tags", []):
            out.append({
                "id":            t["id"],
                "de":            t["label"]["de"],
                "en":            t["label"]["en"],
                "category_id":   cat_id,
                "category_de":   cat_label_de,
                "color":         cat_color,
            })
    return out


def taxonomy_id_set(taxonomy: Dict[str, Any]) -> set[str]:
    """Set of all valid tag IDs — for validating LLM output."""
    return {t["id"] for t in all_tags(taxonomy)}


def taxonomy_de_name_set(taxonomy: Dict[str, Any]) -> set[str]:
    """Set of DE labels — for detecting which Paperless tags came from us
    when deciding 'already tagged, skip on idempotent re-run'."""
    return {t["de"] for t in all_tags(taxonomy)}


# ─── LLM prompt construction ──────────────────────────────────────────

def _format_taxonomy_for_prompt(taxonomy: Dict[str, Any]) -> str:
    """Compact one-line-per-tag listing for the LLM. Grouped by
    category so semantically-related tags stay together — improves
    pick accuracy vs. a flat alphabetic dump."""
    lines: List[str] = []
    for cat in taxonomy.get("categories", []):
        lines.append(f"\n# {cat['label']['de']} / {cat['label']['en']}")
        for t in cat.get("tags", []):
            lines.append(f"  {t['id']}  ·  {t['label']['de']} / {t['label']['en']}")
    return "\n".join(lines)


def _build_prompt(content: str, taxonomy: Dict[str, Any]) -> List[Dict[str, str]]:
    body = (content or "").strip()
    if len(body) > CONTENT_SAMPLE_CHARS:
        body = body[:CONTENT_SAMPLE_CHARS] + "\n[…truncated]"
    tag_list = _format_taxonomy_for_prompt(taxonomy)
    # Prompt is English regardless of the user's language preference —
    # the model picks taxonomy IDs (language-neutral) and the taxonomy
    # listing already shows both DE and EN labels, so the model still
    # categorizes German-language documents accurately.
    return [{
        "role": "system",
        "content": (
            "You categorize documents by picking from a FIXED list of "
            "tag IDs. You never invent new tags. If nothing fits, you "
            "reply with an empty array []."
        ),
    }, {
        "role": "user",
        "content": (
            f"Available tags (grouped by category):\n{tag_list}\n\n"
            f"Document content:\n---\n{body}\n---\n\n"
            "Pick 0–3 tag IDs from the list above that best fit this "
            "document. Reply ONLY with a JSON array of tag IDs. No "
            "markdown, no prose, no comments.\n\n"
            "Examples:\n"
            '  ["rechnung_eingang", "strom"]\n'
            '  ["arbeitsvertrag"]\n'
            "  []"
        ),
    }]


_JSON_ARRAY_RE = re.compile(r"\[[^\]]*\]", re.DOTALL)


def _parse_llm_tag_response(text: str, valid_ids: set[str]) -> List[str]:
    """LLM might wrap the array in prose despite our prompt — extract the
    first [...] and parse it. Drop unknown IDs silently (better to under-
    tag than to invent)."""
    if not text:
        return []
    m = _JSON_ARRAY_RE.search(text)
    raw = m.group(0) if m else text.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("autotagger: couldn't parse LLM response as JSON: %r", text[:200])
        return []
    if not isinstance(parsed, list):
        return []
    picked = [str(x) for x in parsed if isinstance(x, (str,)) and str(x) in valid_ids]
    return picked[:3]  # taxonomy says 0–3 max


# ─── Paperless tag upsert + doc PATCH ─────────────────────────────────

def _paperless_session(creds: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, str]]:
    s = creds or _paperless_settings()
    base = (s.get("base_url") or "http://localhost:8010").rstrip("/")
    headers = {
        "Authorization": f"Token {s['api_key']}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }
    return base, headers


def _ensure_paperless_tag(
    name: str, base: str, headers: Dict[str, str], cache: Dict[str, int],
    color: Optional[str] = None,
) -> Optional[int]:
    """Get-or-create a Paperless tag by DE name. New tags are created
    with the taxonomy's per-category color so Paperless's tag pills
    match Yorik's category palette. Existing tags are left as-is — we
    never overwrite a user-edited color.

    Returns the tag's Paperless id, or None if Paperless itself failed
    (in which case we skip this tag rather than aborting the whole doc)."""
    if name in cache:
        return cache[name]
    # Look up by name first — Paperless's name index is case-sensitive
    # and exact; safer than guessing the slug.
    try:
        r = requests.get(
            f"{base}/api/tags/", headers=headers,
            params={"name__iexact": name, "page_size": 1},
            timeout=PAPERLESS_TIMEOUT,
        )
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        if results:
            tid = int(results[0]["id"])
            cache[name] = tid
            return tid
    except requests.RequestException as exc:
        log.warning("autotagger: tag lookup %r failed: %s", name, exc)
        return None
    # Not found — create. Include color when we have one.
    payload: Dict[str, Any] = {"name": name, "matching_algorithm": 0}
    if color:
        payload["color"] = color
    try:
        r = requests.post(
            f"{base}/api/tags/", headers=headers,
            json=payload,
            timeout=PAPERLESS_TIMEOUT,
        )
        if r.status_code in (200, 201):
            tid = int(r.json()["id"])
            cache[name] = tid
            log.info("autotagger: created Paperless tag %r (id=%d, color=%s)",
                     name, tid, color)
            return tid
        log.warning("autotagger: tag create %r returned %s: %s",
                    name, r.status_code, r.text[:200])
    except requests.RequestException as exc:
        log.warning("autotagger: tag create %r failed: %s", name, exc)
    return None


def _patch_doc_tags(
    doc_id: int, new_tag_ids: List[int], existing_tag_ids: List[int],
    base: str, headers: Dict[str, str],
) -> bool:
    """PATCH the document's tags. UNION with existing tags so user-added
    tags (and our prior runs' tags) survive. Never DELETEs."""
    merged = sorted({*existing_tag_ids, *new_tag_ids})
    if merged == sorted(existing_tag_ids):
        return True  # no-op
    try:
        r = requests.patch(
            f"{base}/api/documents/{doc_id}/", headers=headers,
            json={"tags": merged},
            timeout=PAPERLESS_TIMEOUT,
        )
        if r.status_code in (200, 202):
            return True
        log.warning("autotagger: PATCH /documents/%d returned %s: %s",
                    doc_id, r.status_code, r.text[:200])
    except requests.RequestException as exc:
        log.warning("autotagger: PATCH /documents/%d failed: %s", doc_id, exc)
    return False


# ─── Orchestration ────────────────────────────────────────────────────

def _backfill_tag_colors(
    base: str, headers: Dict[str, str], name_to_color: Dict[str, Optional[str]],
) -> int:
    """For taxonomy tags that already exist in Paperless but were
    created without a color (older autotagger runs predate this
    feature), PATCH the per-category color in. Only touches tags where
    the current color is empty/None — never overwrites a user-edited
    color. Returns count of tags updated."""
    updated = 0
    try:
        url: Optional[str] = f"{base}/api/tags/"
        params: Dict[str, Any] = {"page_size": 500}
        while url:
            r = requests.get(url, headers=headers, params=params, timeout=PAPERLESS_TIMEOUT)
            if not r.ok:
                break
            body = r.json() or {}
            for t in body.get("results") or []:
                nm = (t.get("name") or "").strip()
                wanted = name_to_color.get(nm)
                if not wanted:
                    continue
                current = (t.get("color") or "").strip()
                if current:
                    continue  # respect user-edited colors
                try:
                    pr = requests.patch(
                        f"{base}/api/tags/{int(t['id'])}/",
                        headers=headers,
                        json={"color": wanted},
                        timeout=PAPERLESS_TIMEOUT,
                    )
                    if pr.status_code in (200, 202):
                        updated += 1
                except requests.RequestException as exc:
                    log.warning("autotagger: backfill color on %r failed: %s", nm, exc)
            url = body.get("next")
            params = {}
    except requests.RequestException as exc:
        log.warning("autotagger: color backfill list failed: %s", exc)
    if updated:
        log.info("autotagger: backfilled colors on %d existing taxonomy tag(s)", updated)
    return updated


def _build_paperless_tag_id_cache(
    base: str, headers: Dict[str, str], wanted_names: set[str],
) -> Dict[str, int]:
    """One-shot lookup: pull all Paperless tags up front so the per-doc
    loop doesn't do N+1 GETs. Returns name→id for tags Paperless already
    has; unknown ones get created on first PATCH via _ensure_paperless_tag."""
    cache: Dict[str, int] = {}
    try:
        url: Optional[str] = f"{base}/api/tags/"
        params: Dict[str, Any] = {"page_size": 500}
        while url:
            r = requests.get(url, headers=headers, params=params, timeout=PAPERLESS_TIMEOUT)
            r.raise_for_status()
            body = r.json() or {}
            for t in body.get("results") or []:
                nm = (t.get("name") or "").strip()
                if nm in wanted_names:
                    cache[nm] = int(t["id"])
            url = body.get("next")
            params = {}  # `next` URL already carries pagination
    except requests.RequestException as exc:
        log.warning("autotagger: pre-fetch tag cache failed: %s", exc)
    return cache


def autotag_all(
    *,
    force_retag: bool = False,
    creds: Optional[Dict[str, Any]] = None,
    llm_client: Any = None,
    lang: str = "de",
) -> Dict[str, Any]:
    """Walk every Paperless doc, infer tags from the taxonomy, PATCH
    (union-merge with existing tags). Idempotent by default — skips
    docs that already carry at least one taxonomy tag.

    Heartbeats progress into the workers framework as the 'autotagger'
    worker so Settings → Embeddings can render live X/Y.
    """
    # Batch worker — runs to completion then idles indefinitely until
    # re-triggered. Long interval keeps the chip green during idle.
    workers.register("autotagger", kind="batch", expected_interval_s=86400)
    workers.heartbeat("autotagger", "starting", "loading taxonomy")
    _reset_cancel()

    taxonomy = load_taxonomy()
    tags = all_tags(taxonomy)
    valid_ids = {t["id"] for t in tags}
    # New tags get created in the admin's preferred language. Fall back
    # to DE if a taxonomy entry is missing the requested locale.
    def _label_for(t: Dict[str, Any], lg: str) -> str:
        return (t.get(lg) or t.get("de") or t["id"])
    id_to_name = {t["id"]: _label_for(t, lang) for t in tags}
    name_to_color: Dict[str, Optional[str]] = {
        _label_for(t, lang): t.get("color") for t in tags
    }
    # For "skip already tagged" detection: include ALL language variants
    # so docs auto-tagged in a previous language still get recognised
    # as ours (the system-wide tag language can change between runs;
    # we don't want to double-tag).
    taxonomy_all_lang_names: set[str] = set()
    for t in tags:
        for lg in ("de", "en"):
            n = t.get(lg)
            if n:
                taxonomy_all_lang_names.add(n)

    base, headers = _paperless_session(creds)

    # Lazy default LLM client — reuse the agent's singleton settings.
    if llm_client is None:
        from .agent.llm import LlmClient
        from . import ask as _ask
        llm_client = LlmClient(model=_ask.LLM_MODEL, base_url=_ask.LLM_BASE_URL)

    # Backfill colors on any taxonomy tags Paperless already has from a
    # pre-color-feature autotag run. Idempotent — only PATCHes when the
    # current color is empty so user-edited colors stay.
    workers.heartbeat("autotagger", "ok", "backfilling tag colors")
    _backfill_tag_colors(base, headers, name_to_color)

    # Prefetch Paperless tag ids for ALL taxonomy names across every
    # language variant — covers installs that previously ran the
    # autotagger in a different language.
    tag_id_cache = _build_paperless_tag_id_cache(base, headers, taxonomy_all_lang_names)
    # Set of Paperless tag ids that originated from our taxonomy — used
    # to decide "already auto-tagged, skip".
    taxonomy_pl_ids = set(tag_id_cache.values())

    # Walk all docs (paginated). We need id, tags, content per doc.
    workers.heartbeat("autotagger", "ok", "listing Paperless docs")
    summary = {"total": 0, "tagged": 0, "skipped": 0, "failed": 0, "started_at": time.time()}
    url: Optional[str] = f"{base}/api/documents/"
    params: Dict[str, Any] = {"page_size": 50, "ordering": "id"}

    while url:
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            r.raise_for_status()
            body = r.json() or {}
        except requests.RequestException as exc:
            log.exception("autotagger: list failed mid-walk: %s", exc)
            workers.heartbeat("autotagger", "error", f"list failed: {exc}")
            return summary
        params = {}  # `next` carries the pagination

        for doc in body.get("results") or []:
            if is_cancel_requested():
                elapsed = int(time.time() - summary["started_at"])
                workers.heartbeat(
                    "autotagger", "ok",
                    f"STOPPED at {summary['total']} docs after {elapsed}s · tagged {summary['tagged']}, skipped {summary['skipped']}, failed {summary['failed']}",
                )
                summary["cancelled"] = True
                summary["elapsed_s"] = elapsed
                return summary
            summary["total"] += 1
            doc_id = int(doc["id"])
            existing_tag_ids = [int(x) for x in (doc.get("tags") or [])]
            # Idempotent skip: doc already carries one of OUR tags.
            already = bool(set(existing_tag_ids) & taxonomy_pl_ids)
            if already and not force_retag:
                summary["skipped"] += 1
                continue
            # Force-retag: wipe existing taxonomy tags from the doc
            # FIRST so old/wrong picks from a previous run don't pile
            # up with the new ones (the union-merge in _patch_doc_tags
            # would otherwise keep both). User-added / non-taxonomy
            # tags are preserved — only OUR tags get cleared.
            if force_retag and already:
                kept_tag_ids = [tid for tid in existing_tag_ids
                                if tid not in taxonomy_pl_ids]
                if len(kept_tag_ids) != len(existing_tag_ids):
                    try:
                        rr = requests.patch(
                            f"{base}/api/documents/{doc_id}/",
                            headers=headers,
                            json={"tags": sorted(kept_tag_ids)},
                            timeout=PAPERLESS_TIMEOUT,
                        )
                        if rr.status_code in (200, 202):
                            # Re-read existing_tag_ids so the subsequent
                            # union-merge in _patch_doc_tags starts from
                            # the cleaned state, not the old set.
                            existing_tag_ids = kept_tag_ids
                        else:
                            log.warning(
                                "autotagger: pre-clean PATCH /documents/%d "
                                "returned %s: %s",
                                doc_id, rr.status_code, rr.text[:200],
                            )
                    except requests.RequestException as exc:
                        log.warning(
                            "autotagger: pre-clean PATCH /documents/%d failed: %s",
                            doc_id, exc,
                        )
            content = (doc.get("content") or "").strip()
            if not content:
                summary["skipped"] += 1
                continue
            try:
                messages = _build_prompt(content, taxonomy)
                resp = llm_client.chat(messages, temperature=0.1, max_tokens=120)
                text = (resp.get("content") or "") if isinstance(resp, dict) else str(resp)
                picked_ids = _parse_llm_tag_response(text, valid_ids)
            except Exception as exc:  # noqa: BLE001
                log.warning("autotagger: LLM call failed for doc %d: %s", doc_id, exc)
                summary["failed"] += 1
                continue

            if not picked_ids:
                summary["skipped"] += 1
                continue

            # Resolve picked ids → Paperless tag ids, creating any missing
            # (with the taxonomy's per-category color so Paperless's tag
            # pills match Yorik's dock palette).
            new_pl_ids: List[int] = []
            for pid in picked_ids:
                name = id_to_name[pid]
                pl_id = tag_id_cache.get(name) or _ensure_paperless_tag(
                    name, base, headers, tag_id_cache, color=name_to_color.get(name),
                )
                if pl_id is None:
                    continue
                new_pl_ids.append(pl_id)
                taxonomy_pl_ids.add(pl_id)  # later docs see it as ours

            if not new_pl_ids:
                summary["failed"] += 1
                continue

            ok = _patch_doc_tags(doc_id, new_pl_ids, existing_tag_ids, base, headers)
            if ok:
                summary["tagged"] += 1
            else:
                summary["failed"] += 1

            # Heartbeat every doc — frontend polls Settings every 5s so
            # the user sees the count tick up. Detail string format is
            # what the panel renders verbatim.
            workers.heartbeat(
                "autotagger", "ok",
                f"{summary['tagged']} tagged · {summary['skipped']} skipped · {summary['failed']} failed (of {summary['total']})",
            )

        url = body.get("next")

    # Final heartbeat — done.
    elapsed = int(time.time() - summary["started_at"])
    workers.heartbeat(
        "autotagger", "ok",
        f"DONE in {elapsed}s · tagged {summary['tagged']}, skipped {summary['skipped']}, failed {summary['failed']} ({summary['total']} total)",
    )
    summary["elapsed_s"] = elapsed
    return summary
