"""Bundled local embedder — MiniLM through onnxruntime, in-process.

Default model: paraphrase-multilingual-MiniLM-L12-v2
    384-dim output, multilingual (50+ languages incl. German + English),
    Apache 2.0. We run the fp32 ONNX export that sentence-transformers
    publishes next to the PyTorch weights, so vectors are the same ones
    the old torch path produced — no re-ingest on upgrade. Mean pooling
    over the attention mask, sequences truncated at 128 tokens, exactly
    like the SentenceTransformer config.

Why ONNX instead of sentence-transformers: it was the last thing that
pulled torch + transformers (~4 GB on disk, ~1.5 GB resident) into a
box whose only GPU work is the LLM. onnxruntime + tokenizers do the
same job in ~50 MB of libraries.

Files land in HOMEOS_EMBED_MODEL_DIR (default data/embed/<model>) from
the pinned HuggingFace revision on first use.

Override the model via HOMEOS_EMBED_LOCAL_MODEL. If you change it, the
embedding dimension changes too, which means the vector tables get
dropped + recreated on next startup and all documents need re-ingest.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import List, Optional

import numpy as np

log = logging.getLogger("homeos.embedders.local")

MODEL_NAME = os.getenv(
    "HOMEOS_EMBED_LOCAL_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
MODEL_REVISION = os.getenv("HOMEOS_EMBED_LOCAL_REVISION", "") or None
MAX_SEQ_LENGTH = int(os.getenv("HOMEOS_EMBED_MAX_TOKENS", "128"))
ONNX_FILE = os.getenv("HOMEOS_EMBED_ONNX_FILE", "onnx/model.onnx")

# Pinned revisions for the models we ship defaults for. Anything else
# resolves `main` at download time.
_PINNED_REVISIONS = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2":
        "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
}

# Lookup of output dimension for known models, so we don't have to load
# the model at import time just to size the vector tables. Unknown
# models trigger a lazy load (one-shot, cached).
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

_FILES = [ONNX_FILE, "tokenizer.json", "config.json"]

_session = None
_tokenizer = None
_input_names: list[str] = []
_dim: Optional[int] = None
_lock = threading.Lock()


def model_dir() -> Path:
    root = Path(os.getenv("HOMEOS_EMBED_MODEL_DIR", "data/embed"))
    return root / MODEL_NAME.split("/")[-1]


def installed() -> bool:
    d = model_dir()
    return all((d / f).exists() for f in _FILES)


def download() -> None:
    """Fetch the ONNX export + tokenizer for MODEL_NAME. Blocking."""
    from huggingface_hub import hf_hub_download
    rev = MODEL_REVISION or _PINNED_REVISIONS.get(MODEL_NAME)
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    for f in _FILES:
        hf_hub_download(MODEL_NAME, f, revision=rev, local_dir=str(d))
    if not installed():
        raise RuntimeError(f"embedder download finished but files are missing in {d}")


def _load():
    """Lazy-load the ONNX session + tokenizer (~1 s). Downloads once."""
    global _session, _tokenizer, _input_names, _dim
    if _session is not None:
        return _session
    with _lock:
        if _session is not None:
            return _session
        if not installed():
            log.info("local embedder %s not on disk — downloading", MODEL_NAME)
            download()
        import onnxruntime as ort
        from tokenizers import Tokenizer
        d = model_dir()
        tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        tok.enable_truncation(MAX_SEQ_LENGTH)
        tok.no_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        sess = ort.InferenceSession(str(d / ONNX_FILE), sess_options=opts,
                                    providers=["CPUExecutionProvider"])
        _input_names = [i.name for i in sess.get_inputs()]
        _tokenizer = tok
        _session = sess
        log.info("local embedder ready: %s (onnxruntime, cpu)", MODEL_NAME)
        return sess


def dimension() -> int:
    """Output dimension of the configured local model.

    Fast path: known-model lookup (no model load). Fallback: lazy-load
    the model and embed one token.
    """
    global _dim
    if MODEL_NAME in _KNOWN_DIMS:
        return _KNOWN_DIMS[MODEL_NAME]
    if _dim is None:
        _dim = len(embed("x"))
    return _dim


def embed(text: str) -> List[float]:
    """Encode `text` into a dense vector. Synchronous, in-process."""
    return embed_batch([text])[0]


def embed_batch(texts: List[str]) -> List[List[float]]:
    sess = _load()
    assert _tokenizer is not None
    out: List[List[float]] = []
    # One sequence per run keeps memory flat and avoids padding logic;
    # ingest calls this per chunk anyway.
    for text in texts:
        enc = _tokenizer.encode(text or "")
        ids = np.asarray([enc.ids], dtype=np.int64)
        mask = np.asarray([enc.attention_mask], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in _input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = sess.run(None, feeds)[0]  # (1, seq, dim)
        m = mask[..., None].astype(np.float32)
        pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        out.append(pooled[0].astype(np.float32).tolist())
    return out


def warm_up() -> None:
    _load()
