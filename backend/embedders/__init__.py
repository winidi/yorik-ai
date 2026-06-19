"""Bundled embedders.

Yorik ships an in-process sentence-transformers embedder so semantic
search works out of the box on every fresh install — no separate
llama-server / Ollama / embeddings endpoint required. Users who already
run a faster GPU embedder can still point Yorik at it via
HOMEOS_EMBED_BASE_URL; the local module is the fallback / default.
"""
