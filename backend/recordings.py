"""Recordings — a conversation captured at the table, turned into a
transcript with speaker turns, visible only to the people who were there.

The browser records (MediaRecorder, WebM/Opus) and uploads a chunk every
couple of minutes so a crash costs minutes, not the whole dinner. On
finish the chunks are joined, decoded once to 16 kHz mono, and run
through a CPU-only pipeline:

  1. speaker segmentation + clustering (sherpa-onnx: pyannote
     segmentation-3.0 + 3D-Speaker CAM++ embeddings)
  2. cluster → household member, by comparing a WeSpeaker embedding of
     each cluster's speech with the voice profiles people enrolled in
     Settings → Voice (same model, same threshold as voice_id)
  3. Parakeet transcription per segment (short pieces, which Parakeet
     likes; no timestamp alignment needed)
  4. turns saved to recording_segments, the participants get a bell
     entry + push

No torch, no GPU. A dinner of an hour takes minutes on a workstation
and tens of minutes on an old laptop; the work runs in one background
thread so the chat stays responsive.

Files live in data/recordings/<id>/ (chunks while recording, audio.webm
after finish). Audio is deleted after HOMEOS_RECORDING_RETENTION_DAYS;
transcript and report stay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .database import get_conn

log = logging.getLogger("yorik.recordings")

SAMPLE_RATE = 16_000
ROOT = Path(os.getenv("HOMEOS_RECORDINGS_DIR", "data/recordings"))
MODEL_DIR = Path(os.getenv("HOMEOS_DIARIZATION_MODEL_DIR", "data/diarization"))
SEG_SUBDIR = "sherpa-onnx-pyannote-segmentation-3-0"
SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
           "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMB_FILE = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
           "speaker-recongition-models/" + EMB_FILE)
AUTO_DOWNLOAD = os.getenv("HOMEOS_DIARIZATION_AUTO_DOWNLOAD", "1") not in ("0", "false", "False")

MAX_MINUTES = int(os.getenv("HOMEOS_RECORDING_MAX_MINUTES", "120"))
MAX_CHUNK_MB = int(os.getenv("HOMEOS_RECORDING_MAX_CHUNK_MB", "20"))
RETENTION_DAYS = int(os.getenv("HOMEOS_RECORDING_RETENTION_DAYS", "30"))
THREADS = max(1, min(int(os.getenv("HOMEOS_DIARIZATION_THREADS", "4")), os.cpu_count() or 1))
CLUSTER_THRESHOLD = float(os.getenv("HOMEOS_DIARIZATION_THRESHOLD", "0.7"))
MIN_SEGMENT_S = 0.3           # shorter pieces are breath and clatter
MAX_WINDOW_S = 45.0           # Parakeet window per decode
MERGE_GAP_S = 1.0             # same speaker, pause shorter than this → one turn
IDENTIFY_SECONDS = 20.0       # speech per cluster used for the profile match
KINDS = ("dinner", "meeting", "conversation")
AUTO_REPORT_KINDS = tuple(k.strip() for k in (os.getenv("HOMEOS_RECORDING_AUTO_REPORT") or "dinner,meeting").split(",") if k.strip())

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recordings")
_diarizer = None
_diarizer_lock = threading.Lock()
_download_lock = threading.Lock()


# ─── helpers ────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def rec_dir(rid: int) -> Path:
    return ROOT / str(int(rid))


def _speaker_word(owner_id: Optional[str] = None) -> str:
    """'Sprecher' / 'Speaker' … in the language of the person who recorded."""
    lang = ""
    if owner_id:
        with get_conn() as conn:
            r = conn.execute("SELECT language FROM user_profiles WHERE id = ?", (owner_id,)).fetchone()
        lang = (r["language"] or "") if r else ""
    lang = (lang or os.getenv("HOMEOS_DEFAULT_LANGUAGE") or "en").lower()
    return {"de": "Sprecher", "fr": "Locuteur", "es": "Hablante", "it": "Parlante",
            "nl": "Spreker", "pl": "Mówca"}.get(lang, "Speaker")


def _participants(row: Dict[str, Any]) -> List[str]:
    try:
        ids = json.loads(row.get("participants_json") or "[]")
    except (TypeError, ValueError):
        ids = []
    return [str(x) for x in ids if x]


def _row(rid: int, conn=None) -> Optional[Dict[str, Any]]:
    def q(c):
        r = c.execute("SELECT * FROM recordings WHERE id = ?", (int(rid),)).fetchone()
        return dict(r) if r else None
    if conn is not None:
        return q(conn)
    with get_conn() as c:
        return q(c)


def _set(rid: int, **cols: Any) -> None:
    if not cols:
        return
    sets = ", ".join(f"{k} = ?" for k in cols)
    with get_conn() as conn:
        conn.execute(f"UPDATE recordings SET {sets} WHERE id = ?", (*cols.values(), int(rid)))
        conn.commit()


def _names(ids: List[str]) -> Dict[str, str]:
    if not ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, name FROM user_profiles WHERE id IN ({','.join('?' * len(ids))})",
            tuple(ids)).fetchall()
    return {str(r["id"]): r["name"] or "" for r in rows}


def _share_with(rid: int, user_ids: List[str]) -> None:
    """Read access for the participants via row_shares (same mechanism
    the rest of Yorik uses; admins get no exception)."""
    with get_conn() as conn:
        for uid in user_ids:
            have = conn.execute(
                "SELECT 1 FROM row_shares WHERE table_name = 'recordings' AND row_id = ? AND user_id = ?",
                (int(rid), uid)).fetchone()
            if not have:
                conn.execute(
                    "INSERT INTO row_shares (table_name, row_id, user_id, level, shared_by_user_id) "
                    "VALUES ('recordings', ?, ?, 'read', (SELECT owner_user_id FROM recordings WHERE id = ?))",
                    (int(rid), uid, int(rid)))
        conn.commit()


def can_view(rid: int, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The recording row when the user may see it, else None. Owner or
    participant; role does not matter."""
    from . import spaces as _sp
    frag, params = _sp.row_filter(user["id"], user.get("role"), "recordings")
    with get_conn() as conn:
        r = conn.execute(f"SELECT * FROM recordings WHERE id = ? AND {frag}",
                         (int(rid), *params)).fetchone()
    return dict(r) if r else None


def public(row: Dict[str, Any], *, with_token: bool = False) -> Dict[str, Any]:
    """with_token: only for the creating call — the device that records
    keeps the token; it never appears in lists or later reads."""
    ids = _participants(row)
    names = _names(ids + [str(row["owner_user_id"])])
    return {
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "status": row["status"],
        "owner_user_id": str(row["owner_user_id"]),
        "owner_name": names.get(str(row["owner_user_id"]), ""),
        "participants": [{"user_id": i, "name": names.get(i, "")} for i in ids],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_s": row["duration_s"],
        "chunks": row["chunks"],
        "progress": row.get("progress"),
        "error": row.get("error"),
        "processed_at": row.get("processed_at"),
        "stop_requested": bool(row.get("stop_requested_at")),
        "audio_available": row.get("audio_deleted_at") is None and (rec_dir(row["id"]) / "audio.webm").exists(),
        "has_report": bool(row.get("report_json")),
        "report_template": row.get("report_template"),
        **({"upload_token": row.get("upload_token")} if with_token else {}),
    }


# ─── lifecycle ──────────────────────────────────────────────────────

def create(owner_id: str, *, title: str = "", kind: str = "conversation",
           participants: Optional[List[str]] = None) -> Dict[str, Any]:
    from . import spaces as _sp
    kind = kind if kind in KINDS else "conversation"
    ids = [str(p) for p in (participants or []) if p and str(p) != str(owner_id)]
    known = set(_names(ids).keys()) if ids else set()
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"unknown participant(s): {', '.join(unknown)}")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO recordings (owner_user_id, space_id, title, kind, participants_json, upload_token, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_id, _sp.personal_space_id(owner_id), (title or "").strip()[:200], kind, json.dumps(ids),
             secrets.token_urlsafe(24), _now()))    # local time, like every other timestamp Yorik writes
        rid = int(cur.lastrowid)
        conn.commit()
    _share_with(rid, ids)
    rec_dir(rid).mkdir(parents=True, exist_ok=True)
    return _row(rid)


def add_chunk(rid: int, seq: int, data: bytes) -> Dict[str, Any]:
    row = _row(rid)
    if not row:
        raise KeyError(rid)
    if row["status"] != "recording":
        raise ValueError(f"recording is {row['status']}, not accepting audio")
    if seq < 0 or seq > 100_000:
        raise ValueError("bad chunk sequence")
    d = rec_dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"chunk-{seq:06d}.webm").write_bytes(data)
    n = len(list(d.glob("chunk-*.webm")))
    _set(rid, chunks=n)
    return {"id": rid, "chunks": n}


def finish(rid: int, *, duration_s: Optional[float] = None) -> Dict[str, Any]:
    """Join the chunks, mark uploaded, queue the pipeline."""
    row = _row(rid)
    if not row:
        raise KeyError(rid)
    if row["status"] not in ("recording", "failed"):
        return row
    d = rec_dir(rid)
    chunks = sorted(d.glob("chunk-*.webm"))
    audio = d / "audio.webm"
    if chunks:
        with audio.open("wb") as out:
            for c in chunks:
                out.write(c.read_bytes())
        for c in chunks:
            c.unlink(missing_ok=True)
    if not audio.exists() or audio.stat().st_size == 0:
        raise ValueError("no audio was uploaded")
    _set(rid, status="uploaded", ended_at=_now(), duration_s=duration_s, error=None, progress="queued")
    schedule_processing(rid)
    return _row(rid)


def request_stop(rid: int) -> Dict[str, Any]:
    """Ask the recording device to stop: it polls the status, uploads its
    last chunk and calls finish. Used when the stop comes from chat or
    voice rather than from the recording UI."""
    _set(rid, stop_requested_at=_now())
    return _row(rid)


def delete(rid: int) -> None:
    shutil.rmtree(rec_dir(rid), ignore_errors=True)
    with get_conn() as conn:
        conn.execute("DELETE FROM row_shares WHERE table_name = 'recordings' AND row_id = ?", (int(rid),))
        conn.execute("DELETE FROM recordings WHERE id = ?", (int(rid),))
        conn.commit()


def list_visible(user: Dict[str, Any], limit: int = 50) -> List[Dict[str, Any]]:
    from . import spaces as _sp
    frag, params = _sp.row_filter(user["id"], user.get("role"), "recordings")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM recordings WHERE {frag} ORDER BY id DESC LIMIT ?",
            (*params, int(limit))).fetchall()
    return [public(dict(r)) for r in rows]


def latest_for(user_id: str, statuses: Optional[tuple] = None) -> Optional[Dict[str, Any]]:
    """The owner's most recent recording, optionally in given states."""
    where = "owner_user_id = ?"
    params: list = [user_id]
    if statuses:
        where += f" AND status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    with get_conn() as conn:
        r = conn.execute(f"SELECT * FROM recordings WHERE {where} ORDER BY id DESC LIMIT 1",
                         tuple(params)).fetchone()
    return dict(r) if r else None


def segments(rid: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT seq, start_s, end_s, speaker_label, user_id, text FROM recording_segments "
            "WHERE recording_id = ? ORDER BY seq", (int(rid),)).fetchall()
    return [{"seq": r["seq"], "start_s": r["start_s"], "end_s": r["end_s"], "speaker": r["speaker_label"],
             "user_id": str(r["user_id"]) if r["user_id"] else None, "text": r["text"]} for r in rows]


def transcript_text(rid: int, max_chars: Optional[int] = None) -> str:
    """'[mm:ss] Name: text' per turn — what the report skill reads."""
    lines = []
    for s in segments(rid):
        m, sec = divmod(int(s["start_s"]), 60)
        lines.append(f"[{m:02d}:{sec:02d}] {s['speaker']}: {s['text']}")
    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n…"
    return text


# ─── models ─────────────────────────────────────────────────────────

def seg_model_path() -> Path:
    return MODEL_DIR / SEG_SUBDIR / "model.onnx"


def emb_model_path() -> Path:
    return MODEL_DIR / EMB_FILE


def models_installed() -> bool:
    return seg_model_path().exists() and emb_model_path().exists()


def download_models() -> None:
    """Blocking. ~6 MB segmentation + ~28 MB embedding model."""
    import requests
    with _download_lock:
        if models_installed():
            return
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        if not seg_model_path().exists():
            with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=False, dir=str(MODEL_DIR)) as tmp:
                tmp_path = Path(tmp.name)
                with requests.get(SEG_URL, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        tmp.write(chunk)
            try:
                with tarfile.open(tmp_path, "r:bz2") as tar:
                    for m in tar.getmembers():
                        if m.isfile() and m.name.startswith(SEG_SUBDIR + "/") and ".." not in Path(m.name).parts:
                            tar.extract(m, path=str(MODEL_DIR))
            finally:
                tmp_path.unlink(missing_ok=True)
        if not emb_model_path().exists():
            tmp = emb_model_path().with_suffix(".part")
            with requests.get(EMB_URL, stream=True, timeout=60) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            tmp.rename(emb_model_path())
        if not models_installed():
            raise RuntimeError(f"diarization models missing after download in {MODEL_DIR}")
        log.info("recordings: diarization models installed in %s", MODEL_DIR)


def _get_diarizer():
    global _diarizer
    if _diarizer is not None:
        return _diarizer
    with _diarizer_lock:
        if _diarizer is not None:
            return _diarizer
        if not models_installed():
            if not AUTO_DOWNLOAD:
                raise FileNotFoundError(f"diarization models are not installed (expected in {MODEL_DIR})")
            download_models()
        import sherpa_onnx as so
        cfg = so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg_model_path())),
                num_threads=THREADS),
            embedding=so.SpeakerEmbeddingExtractorConfig(model=str(emb_model_path()), num_threads=THREADS),
            clustering=so.FastClusteringConfig(num_clusters=-1, threshold=CLUSTER_THRESHOLD),
            min_duration_on=MIN_SEGMENT_S, min_duration_off=0.5)
        _diarizer = so.OfflineSpeakerDiarization(cfg)
        log.info("recordings: diarizer ready (%d threads, threshold %.2f)", THREADS, CLUSTER_THRESHOLD)
        return _diarizer


# ─── pipeline steps (module-level so tests can swap them) ──────────

def decode_audio(path: str) -> np.ndarray:
    from . import stt_parakeet
    return stt_parakeet._decode_audio(path)


def diarize(audio: np.ndarray) -> List[Dict[str, Any]]:
    """[{start, end, cluster}] sorted by start."""
    sd = _get_diarizer()
    res = sd.process(audio).sort_by_start_time()
    return [{"start": float(s.start), "end": float(s.end), "cluster": int(s.speaker)} for s in res]


def identify_clusters(audio: np.ndarray, segs: List[Dict[str, Any]],
                      candidate_ids: List[str]) -> Dict[int, Dict[str, Any]]:
    """cluster → {"user_id", "name", "similarity"} for clusters whose
    voice matches an enrolled household member. Uses voice_id's model so
    the enrolled embeddings are comparable."""
    from . import voice_id as V
    profiles = [p for p in V._load_enrolled_profiles() if str(p["id"]) in set(candidate_ids)]
    if not profiles or not segs:
        return {}
    try:
        ext = V._get_extractor()
    except Exception as exc:  # noqa: BLE001
        log.warning("recordings: speaker encoder unavailable (%s); speakers stay anonymous", exc)
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    clusters = sorted({s["cluster"] for s in segs})
    for c in clusters:
        pieces = sorted((s for s in segs if s["cluster"] == c), key=lambda s: s["end"] - s["start"], reverse=True)
        buf, total = [], 0.0
        for s in pieces:
            a, b = int(s["start"] * SAMPLE_RATE), int(s["end"] * SAMPLE_RATE)
            buf.append(audio[a:b])
            total += s["end"] - s["start"]
            if total >= IDENTIFY_SECONDS:
                break
        if total < 1.0:
            continue
        with V._lock:
            st = ext.create_stream()
            st.accept_waveform(SAMPLE_RATE, np.concatenate(buf))
            st.input_finished()
            vec = [float(x) for x in ext.compute(st)]
        best, best_sim = None, 0.0
        for p in profiles:
            sim = V._cosine(vec, p["embedding"])
            if sim > best_sim:
                best, best_sim = p, sim
        if best and best_sim >= V.MATCH_THRESHOLD:
            out[c] = {"user_id": str(best["id"]), "name": best["name"], "similarity": round(best_sim, 3)}
    return out


def transcribe_segment(audio: np.ndarray) -> str:
    from . import stt_parakeet as P
    rec = P.get_recognizer()
    texts = []
    n = len(audio)
    win = int(MAX_WINDOW_S * SAMPLE_RATE)
    for off in range(0, n, win):
        piece = audio[off:off + win]
        if len(piece) < int(MIN_SEGMENT_S * SAMPLE_RATE):
            continue
        with P._decode_lock:
            s = rec.create_stream()
            s.accept_waveform(SAMPLE_RATE, piece)
            rec.decode_stream(s)
            t = (s.result.text or "").strip()
        if t:
            texts.append(t)
    return " ".join(texts)


def _label_clusters(segs: List[Dict[str, Any]], ident: Dict[int, Dict[str, Any]],
                    owner_id: Optional[str] = None) -> Dict[int, Dict[str, Any]]:
    """cluster → {label, user_id}; identified clusters carry the name,
    the rest are numbered in order of first appearance."""
    labels: Dict[int, Dict[str, Any]] = {}
    word = _speaker_word(owner_id)
    n = 0
    for s in segs:
        c = s["cluster"]
        if c in labels:
            continue
        if c in ident:
            labels[c] = {"label": ident[c]["name"], "user_id": ident[c]["user_id"]}
        else:
            n += 1
            labels[c] = {"label": f"{word} {n}", "user_id": None}
    return labels


def _merge_turns(segs: List[Dict[str, Any]], labels: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    turns: List[Dict[str, Any]] = []
    for s in segs:
        lab = labels[s["cluster"]]
        if turns and turns[-1]["label"] == lab["label"] and s["start"] - turns[-1]["end"] <= MERGE_GAP_S:
            turns[-1]["end"] = s["end"]
            turns[-1]["pieces"].append((s["start"], s["end"]))
        else:
            turns.append({"start": s["start"], "end": s["end"], "label": lab["label"],
                          "user_id": lab["user_id"], "pieces": [(s["start"], s["end"])]})
    return turns


def process(rid: int) -> Dict[str, Any]:
    """The whole pipeline, blocking. Called from the worker thread."""
    from . import notifications as _notif
    from . import workers
    row = _row(rid)
    if not row:
        raise KeyError(rid)
    d = rec_dir(rid)
    audio_path = d / "audio.webm"
    t0 = time.monotonic()
    _set(rid, status="processing", progress="decoding", error=None)
    workers.heartbeat("recordings", "ok", f"#{rid} decoding")
    try:
        audio = decode_audio(str(audio_path))
        dur = float(len(audio)) / SAMPLE_RATE
        if dur < 1.0:
            raise ValueError("recording is empty")
        if dur > MAX_MINUTES * 60:
            raise ValueError(f"recording longer than {MAX_MINUTES} minutes")
        _set(rid, duration_s=round(dur, 1), progress="speakers")
        workers.heartbeat("recordings", "ok", f"#{rid} speakers ({dur/60:.0f} min)")
        segs = [s for s in diarize(audio) if s["end"] - s["start"] >= MIN_SEGMENT_S]
        candidates = _participants(row) + [str(row["owner_user_id"])]
        ident = identify_clusters(audio, segs, candidates)
        labels = _label_clusters(segs, ident, str(row["owner_user_id"]))
        turns = _merge_turns(segs, labels)
        _set(rid, progress=f"transcribing 0/{len(turns)}")
        rows = []
        for i, t in enumerate(turns, 1):
            parts = []
            for a, b in t["pieces"]:
                txt = transcribe_segment(audio[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)])
                if txt:
                    parts.append(txt)
            text = " ".join(parts).strip()
            if text:
                rows.append((i, t["start"], t["end"], t["label"], t["user_id"], text))
            if i % 10 == 0:
                _set(rid, progress=f"transcribing {i}/{len(turns)}")
                workers.heartbeat("recordings", "ok", f"#{rid} transcribing {i}/{len(turns)}")
        with get_conn() as conn:
            conn.execute("DELETE FROM recording_segments WHERE recording_id = ?", (int(rid),))
            for seq, (n, a, b, lab, uid, text) in enumerate(rows, 1):
                conn.execute(
                    "INSERT INTO recording_segments (recording_id, seq, start_s, end_s, speaker_label, user_id, text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (int(rid), seq, round(a, 2), round(b, 2), lab, uid, text))
            conn.commit()
        _set(rid, status="done", progress=None, processed_at=_now())
        took = time.monotonic() - t0
        speakers = sorted({lab for _, _, _, lab, _, _ in rows})
        log.info("recordings: #%s done — %.0f s audio, %d turns, %d speakers (%s), %.0f s processing",
                 rid, dur, len(rows), len(speakers), ", ".join(speakers), took)
        workers.heartbeat("recordings", "ok", f"#{rid} done in {took:.0f}s")
        report = None
        if rows and row["kind"] in AUTO_REPORT_KINDS:
            _set(rid, progress="report")
            workers.heartbeat("recordings", "ok", f"#{rid} report")
            try:
                from . import recording_reports as _rep
                report = _rep.build_report(rid, row["kind"], notify=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("recordings: #%s report failed (%s); transcript is there", rid, exc)
            _set(rid, progress=None)
        if report is None:
            _notify_done(rid, row, dur, len(rows), speakers)
        return {"id": rid, "status": "done", "turns": len(rows), "speakers": speakers, "seconds": round(took, 1),
                "report": bool(report)}
    except Exception as exc:  # noqa: BLE001
        log.exception("recordings: #%s failed", rid)
        _set(rid, status="failed", progress=None, error=f"{type(exc).__name__}: {exc}"[:500])
        workers.report_error("recordings", f"#{rid}: {exc}")
        raise


def _notify_done(rid: int, row: Dict[str, Any], dur: float, turns: int, speakers: List[str]) -> None:
    from . import notifications as _notif
    title = row["title"] or {"dinner": "Dinner", "meeting": "Meeting"}.get(row["kind"], "Recording")
    body = f"{dur/60:.0f} min, {turns} turns, {len(speakers)} speakers: {', '.join(speakers)}"
    for uid in [str(row["owner_user_id"])] + _participants(row):
        try:
            _notif.create(user_id=uid, kind="recording_done", title=f"{title}: transcript ready", body=body,
                          payload={"recording_id": rid}, navigate_to=f"/r/recordings/{rid}")
        except Exception as exc:  # noqa: BLE001
            log.warning("recordings: notify %s failed: %s", uid, exc)


def _process_safe(rid: int) -> None:
    try:
        process(rid)
    except Exception:  # noqa: BLE001
        pass  # logged + persisted in process()


def schedule_processing(rid: int) -> None:
    """Queue the pipeline on the single worker thread. Without a running
    loop (scripts, tests) it runs inline."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        _process_safe(rid)
        return
    loop.run_in_executor(_executor, _process_safe, rid)


# ─── retention ──────────────────────────────────────────────────────

def purge_audio(now: Optional[datetime] = None) -> int:
    """Delete audio files of finished recordings older than the retention
    window; the transcript stays. Also fails recordings that were never
    finished within a day. Returns the number of recordings touched."""
    now = now or datetime.now()
    cutoff = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    stale = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    touched = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM recordings WHERE status = 'done' AND audio_deleted_at IS NULL AND processed_at < ?",
            (cutoff,)).fetchall()
        for r in rows:
            d = rec_dir(r["id"])
            for f in d.glob("audio.*"):
                f.unlink(missing_ok=True)
            conn.execute("UPDATE recordings SET audio_deleted_at = ? WHERE id = ?", (_now(), r["id"]))
            touched += 1
        abandoned = conn.execute(
            "SELECT id FROM recordings WHERE status = 'recording' AND started_at < ?", (stale,)).fetchall()
        for r in abandoned:
            conn.execute("UPDATE recordings SET status = 'failed', error = 'never finished' WHERE id = ?", (r["id"],))
            touched += 1
        conn.commit()
    return touched


_scheduler_task: Optional[asyncio.Task] = None


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    from . import workers
    global _scheduler_task
    workers.register("recordings", kind="pipeline", expected_interval_s=3600)

    async def _loop():
        while True:
            try:
                n = await asyncio.get_running_loop().run_in_executor(None, purge_audio)
                workers.heartbeat("recordings", "ok", f"retention sweep, {n} touched")
            except Exception as exc:  # noqa: BLE001
                log.warning("recordings: retention sweep failed: %s", exc)
            await asyncio.sleep(3600)

    _scheduler_task = loop.create_task(_loop(), name="recordings-retention")


# ─── routes ─────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


def _current_user():
    from .auth_sessions import current_user
    return current_user


class CreateIn(BaseModel):
    title: str = ""
    kind: str = "conversation"
    participants: List[str] = []


class FinishIn(BaseModel):
    duration_s: Optional[float] = None


def _owned(rid: int, user: Dict[str, Any]) -> Dict[str, Any]:
    row = can_view(rid, user)
    if not row:
        raise HTTPException(status_code=404, detail="no such recording")
    if str(row["owner_user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="only the person who started the recording can do that")
    return row


def _device_or_owner(rid: int, request: Request, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Chunk and finish: the recording device authenticates with the
    upload token it got at creation, or the owner's session does."""
    token = (request.headers.get("x-recording-token") or "").strip()
    if token:
        row = _row(rid)
        if row and row.get("upload_token") and secrets.compare_digest(str(row["upload_token"]), token):
            return row
        raise HTTPException(status_code=403, detail="bad recording token")
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return _owned(rid, user)


def _current_user_optional():
    from .auth_sessions import current_user_optional
    return current_user_optional


@router.get("/models")
def models_status(user: Dict[str, Any] = Depends(_current_user())):
    return {"installed": models_installed(), "dir": str(MODEL_DIR), "auto_download": AUTO_DOWNLOAD}


@router.post("/models/download")
def models_download(user: Dict[str, Any] = Depends(_current_user())):
    if user.get("role") not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    threading.Thread(target=download_models, daemon=True, name="diarization-download").start()
    return {"started": True}


@router.get("")
def list_route(user: Dict[str, Any] = Depends(_current_user())):
    return {"recordings": list_visible(user)}


@router.post("")
def create_route(body: CreateIn, user: Dict[str, Any] = Depends(_current_user())):
    if user.get("role") == "restricted":
        raise HTTPException(status_code=403, detail="restricted accounts cannot start recordings")
    try:
        row = create(str(user["id"]), title=body.title, kind=body.kind, participants=body.participants)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return public(row, with_token=True)


@router.post("/{rid}/chunk")
async def chunk_route(rid: int, request: Request, seq: int = Form(...), audio: UploadFile = File(...),
                      user: Optional[Dict[str, Any]] = Depends(_current_user_optional())):
    _device_or_owner(rid, request, user)
    data = await audio.read()
    if len(data) > MAX_CHUNK_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"chunk larger than {MAX_CHUNK_MB} MB")
    if not data:
        raise HTTPException(status_code=400, detail="empty chunk")
    try:
        return add_chunk(rid, seq, data)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{rid}/finish")
def finish_route(rid: int, request: Request, body: Optional[FinishIn] = None,
                 user: Optional[Dict[str, Any]] = Depends(_current_user_optional())):
    _device_or_owner(rid, request, user)
    try:
        row = finish(rid, duration_s=(body.duration_s if body else None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return public(row)


@router.get("/{rid}")
def get_route(rid: int, user: Dict[str, Any] = Depends(_current_user())):
    row = can_view(rid, user)
    if not row:
        raise HTTPException(status_code=404, detail="no such recording")
    return public(row)


@router.get("/{rid}/transcript")
def transcript_route(rid: int, user: Dict[str, Any] = Depends(_current_user())):
    row = can_view(rid, user)
    if not row:
        raise HTTPException(status_code=404, detail="no such recording")
    return {"id": rid, "status": row["status"], "segments": segments(rid), "text": transcript_text(rid)}


@router.get("/{rid}/audio")
def audio_route(rid: int, user: Dict[str, Any] = Depends(_current_user())):
    row = can_view(rid, user)
    if not row:
        raise HTTPException(status_code=404, detail="no such recording")
    f = rec_dir(rid) / "audio.webm"
    if row.get("audio_deleted_at") or not f.exists():
        raise HTTPException(status_code=410, detail="audio was deleted")
    return FileResponse(str(f), media_type="audio/webm")


class ReportIn(BaseModel):
    refresh: bool = False
    template: Optional[str] = None


@router.get("/{rid}/report")
def report_get_route(rid: int, user: Dict[str, Any] = Depends(_current_user())):
    from . import recording_reports as REP
    row = can_view(rid, user)
    if not row:
        raise HTTPException(status_code=404, detail="no such recording")
    rep = REP.get_report(rid)
    if not rep:
        raise HTTPException(status_code=404, detail="no report yet")
    return rep


@router.post("/{rid}/report")
async def report_build_route(rid: int, body: Optional[ReportIn] = None, user: Dict[str, Any] = Depends(_current_user())):
    """Write (or rewrite) the report; a long LLM pass, so it runs off the loop."""
    from . import recording_reports as REP
    row = can_view(rid, user)
    if not row:
        raise HTTPException(status_code=404, detail="no such recording")
    if row["status"] != "done":
        raise HTTPException(status_code=409, detail=f"transcript is not ready ({row['status']})")
    body = body or ReportIn()
    existing = REP.get_report(rid)
    if existing and not body.refresh:
        return existing
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: REP.build_report(rid, body.template, notify=existing is None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"report failed: {exc}")


class AdoptIn(BaseModel):
    person: Optional[str] = None
    due_date: Optional[str] = None
    with_user_ids: List[str] = []       # people doing it together with the adopter


@router.post("/{rid}/tasks/{index}/adopt")
def adopt_task_route(rid: int, index: int, body: Optional[AdoptIn] = None, user: Dict[str, Any] = Depends(_current_user())):
    """Turn one proposed task of the report into a real task. Anyone at
    the table may adopt; the task is created by them, assigned to the
    named household member too, and the report remembers the task id."""
    from . import recording_reports as REP
    row = can_view(rid, user)
    if not row:
        raise HTTPException(status_code=404, detail="no such recording")
    rep = REP.get_report(rid)
    if not rep or index < 0 or index >= len(rep.get("tasks") or []):
        raise HTTPException(status_code=404, detail="no such task in the report")
    task = rep["tasks"][index]
    if task.get("task_id"):
        return rep
    body = body or AdoptIn()
    person = (body.person if body.person is not None else task.get("person") or "").strip()
    due = (body.due_date if body.due_date is not None else task.get("due_date") or "").strip()[:10] or None
    allowed = set(_participants(row) + [str(row["owner_user_id"])])
    together = [u for u in body.with_user_ids if u in allowed and u != str(user["id"])]
    task_id = REP.create_task_from_report(
        creator_id=str(user["id"]), title=task["title"], person=person, due_date=due,
        notes=(task.get("why") or "").strip() or None, recording_id=rid, recording_title=row["title"],
        with_user_ids=together)
    names = _names([str(user["id"])] + together)
    task["task_id"] = task_id
    task["adopted_by"] = names.get(str(user["id"]), "")
    task["with"] = [names.get(u, "") for u in together]
    task["adopted_at"] = _now()
    _set(rid, report_json=json.dumps(rep, ensure_ascii=False))
    return rep


@router.delete("/{rid}")
def delete_route(rid: int, user: Dict[str, Any] = Depends(_current_user())):
    _owned(rid, user)
    delete(rid)
    return {"ok": True}
