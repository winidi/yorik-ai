"""Bundled local embedder — sentence-transformers in-process.

Default model: paraphrase-multilingual-MiniLM-L12-v2
    ~120 MB on disk, 384-dim output, multilingual (50+ languages incl.
    German + English), no `trust_remote_code` required, Apache 2.0.
    First call downloads weights into ~/.cache/huggingface/; subsequent
    calls run entirely in-process (no HTTP, no Ollama, no llama-server).

Override via HOMEOS_EMBED_LOCAL_MODEL to use a different model. If you
change the model, you also change the embedding dimension, which means
the existing vec0 tables get dropped + recreated on next startup and
all documents need to be re-ingested.

Why bundled (vs. requiring an external embedder):
- Most users don't run a /v1/embeddings endpoint and shouldn't have to.
- A "semantic search works on day 1" UX beats a config-file maze.
- Power users keep their fast GPU path via HOMEOS_EMBED_BASE_URL.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

log = logging.getLogger("homeos.embedders.local")

MODEL_NAME = os.getenv(
    "HOMEOS_EMBED_LOCAL_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)

# Lookup of output dimension for known models, so we don't have to load
# the model at import time just to size the vec0 tables. Unknown models
# trigger a lazy load (one-shot, cached).
_KNOWN_DIMS = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "paraphrase-multilingual-MiniLM-L12-v2": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "all-MiniLM-L6-v2": 384,
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": 768,
    "paraphrase-multilingual-mpnet-base-v2": 768,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "all-mpnet-base-v2": 768,
}

_model = None
_dim: Optional[int] = None


def _get_model():
    """Lazy-load the SentenceTransformer. Costs ~2s on first call + a
    one-time ~120 MB download from HuggingFace if not cached."""
    global _model, _dim
    if _model is None:
        from sentence_transformers import SentenceTransformer
        log.info("loading local embedder %s (first call may download weights)", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME)
        _dim = int(_model.get_sentence_embedding_dimension())
        log.info("local embedder ready: dim=%d", _dim)
    return _model


def dimension() -> int:
    """Output dimension of the configured local model.

    Fast path: known-model lookup (no model load). Fallback: lazy-load
    the model. This is called once at startup to size the vec0 tables.
    """
    if MODEL_NAME in _KNOWN_DIMS:
        return _KNOWN_DIMS[MODEL_NAME]
    _get_model()
    assert _dim is not None
    return _dim


def embed(text: str) -> List[float]:
    """Encode `text` into a dense vector. Synchronous, in-process."""
    m = _get_model()
    vec = m.encode(text, normalize_embeddings=False, convert_to_numpy=True)
    return vec.tolist()


def is_available() -> bool:
    """True iff sentence-transformers is installed and the model loads.
    Used by start.sh / health checks to decide whether to surface the
    'install sentence-transformers' nudge."""
    try:
        _get_model()
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("local embedder unavailable: %s", exc)
        return False
