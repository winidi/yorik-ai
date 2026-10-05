"""Second look at the search hits: a cross-encoder reads the question
and each candidate together and orders them.

Word and meaning search each see one side only; a reranker sees both
and places "Bestellung #201684554 wurde zugestellt · Snuzone" first for
"wo hatte ich letztes mal snooze bestellt" (measured 2026-10-05). The
model is bge-reranker-v2-m3 (Apache 2.0, German and English) in a
llama.cpp container on the CPU — the GPU is full with the chat model
(scripts/install-search-reranker.sh). About 20 pairs take 0.6 s on
this box, 40 take 2.2 s, so the chat sends at most PAIRS.

Off when YORIK_SEARCH_RERANK_URL is unset or the service is slow: the
hits then stay in the order the search gave them.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("yorik.search_rerank")

RERANK_URL = os.getenv("YORIK_SEARCH_RERANK_URL", "").rstrip("/")
RERANK_MODEL = os.getenv("YORIK_SEARCH_RERANK_MODEL", "bge-reranker-v2-m3")
TIMEOUT_S = float(os.getenv("YORIK_SEARCH_RERANK_TIMEOUT_S", "2.5"))
PAIRS = int(os.getenv("YORIK_SEARCH_RERANK_PAIRS", "32"))      # candidates per question
PER_SOURCE = 8                                                 # candidates per source, at most
TEXT_CHARS = 320                                               # of each candidate
SKIP = {"immich", "pipelines"}                                 # pictures carry no text to read
# The sources with many near-identical rows get the deeper look: the
# Snuzone order sat on place 6 of the mails behind "Bestellt: …" shop
# notices (2026-10-05); a round-robin over eight sources never reached it.
DEEP = ("email", "whatsapp", "paperless", "bank")


def enabled() -> bool:
    return bool(RERANK_URL)


def candidate_text(h: Dict[str, Any]) -> str:
    parts = [str(h.get(k) or "").strip() for k in ("title", "subtitle", "who", "snippet")]
    return " · ".join(p for p in parts if p)[:TEXT_CHARS]


def _scores(query: str, docs: List[str], timeout: float) -> Optional[List[float]]:
    import requests
    r = requests.post(f"{RERANK_URL}/rerank", json={"model": RERANK_MODEL, "query": query, "documents": docs,
                                                    "top_n": len(docs)}, timeout=timeout)
    r.raise_for_status()
    out: List[Optional[float]] = [None] * len(docs)
    for item in r.json().get("results") or []:
        i = item.get("index")
        if isinstance(i, int) and 0 <= i < len(docs):
            out[i] = float(item.get("relevance_score") or 0.0)
    return [x if x is not None else float("-inf") for x in out]


def rerank(question: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """The search result with each source's first candidates in the
    reranker's order and the sources by their best candidate. Unchanged
    when the reranker is off, slow or fails."""
    if not enabled() or not question:
        return raw
    results: Dict[str, List[Dict[str, Any]]] = raw.get("results") or {}
    picks: List[tuple[str, int]] = []
    seen: set = set()
    # the deep sources first, in full; then round-robin over the rest
    for src in DEEP:
        for i, h in enumerate((results.get(src) or [])[:PER_SOURCE]):
            if candidate_text(h):
                picks.append((src, i)); seen.add((src, i))
    for i in range(PER_SOURCE):
        for src, hits in results.items():
            if src in SKIP or src in DEEP or i >= len(hits) or not candidate_text(hits[i]):
                continue
            picks.append((src, i))
    picks = picks[:PAIRS]
    if len(picks) < 2:
        return raw
    docs = [candidate_text(results[s][i]) for s, i in picks]
    from backend import speed
    limit = speed.budget(TIMEOUT_S, "rerank")
    t0 = time.perf_counter()
    try:
        scores = _scores(question, docs, limit)
    except Exception as exc:  # noqa: BLE001 — the search is fine without
        log.info("rerank skipped: %s", exc)
        speed.record("rerank", min(time.perf_counter() - t0, limit))
        return raw
    speed.record("rerank", time.perf_counter() - t0)
    if not scores:
        return raw
    by_source: Dict[str, Dict[int, float]] = {}
    for (src, i), sc in zip(picks, scores):
        by_source.setdefault(src, {})[i] = sc
    out: Dict[str, List[Dict[str, Any]]] = {}
    best: Dict[str, float] = {}
    for src, hits in results.items():
        scored = by_source.get(src)
        if not scored:
            out[src] = hits
            continue
        head = sorted(scored, key=lambda i: -scored[i])
        rest = [i for i in range(len(hits)) if i not in scored]
        out[src] = [{**hits[i], "_rerank": round(scored[i], 3)} for i in head] + [hits[i] for i in rest]
        best[src] = scored[head[0]]
    order = sorted(out, key=lambda s: -best.get(s, float("-inf")))
    return {**raw, "results": {s: out[s] for s in order}, "reranked": len(picks)}
