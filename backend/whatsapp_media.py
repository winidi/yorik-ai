"""WhatsApp incoming-media auto-routing.

Called for every incoming WA message that has an attachment. Routes by
content type:

  document (PDF / Office / text)  →  Paperless (auto-tagged "whatsapp"
                                     + contact name, OCR'd, indexed,
                                     searchable from Yorik's docs app)
  image / video                   →  Immich (added to library, CLIP-
                                     indexed, searchable by content)
  audio (voice notes)             →  Whisper transcription, stored in
                                     wa_messages.transcript so the
                                     draft generator can use it

Everything runs in a background task — the WS subscriber never blocks
on a slow OCR/upload/transcription cycle. Idempotent: skips work if
the per-target id column on wa_messages is already populated, so a
re-process call is cheap.

If a downstream service is misconfigured or down (no Paperless API
token, Immich unreachable, …), we log + skip rather than retry. The
raw message is already in wa_messages and the user can trigger a
manual re-process via POST /api/whatsapp/messages/<msg_id>/reprocess.

Security gate — `_is_known_sender`:
  Routing is gated on the sender being a "known" contact. Phishing-
  by-fake-invoice, parser-CVE delivery via crafted PDFs, and "stranger
  selfie" pollution of Immich are real risks if every incoming WA
  attachment auto-files. Gate: 1:1 DM only (groups + status@broadcast
  never auto-route), AND either the JID is in the household contacts
  with status='active' OR this Yorik user has sent at least one
  message to that JID (proof of two-way relationship). Anything that
  fails the gate still lands in wa_messages and the chat list — the
  user can pick "file this" manually; spam never touches Paperless /
  Immich / Whisper.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import httpx

from .database import get_conn

log = logging.getLogger("yorik.whatsapp.media")

BRIDGE_URL = os.getenv("YORIK_WA_BRIDGE_URL", "http://127.0.0.1:3015")

# Document-style mimetypes route to Paperless. Everything else with
# mediaKind=="document" still routes there (Paperless OCR's images too)
# but at least these are the ones we expect.
_PAPERLESS_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/csv",
    "application/rtf",
}


def _status_imports_enabled() -> bool:
    """Read the app_settings flag that gates WhatsApp Status auto-import.
    Default off — see process_media docstring for rationale.

    Tolerant of a missing app_settings table / unreachable DB so a
    bridge-event during early boot doesn't 500 the entire media pipeline.
    """
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'whatsapp_import_status'"
            ).fetchone()
        return bool(row and str(row["value"]).strip() == "1")
    except Exception:  # noqa: BLE001
        return False


def set_status_imports_enabled(enabled: bool) -> None:
    """Persist the flag. Called by the Settings → WhatsApp toggle."""
    from datetime import datetime, timezone
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES ('whatsapp_import_status', ?, ?)",
            ("1" if enabled else "0",
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


async def process_media(msg: dict[str, Any], owner_user_id: str = 1, *, force: bool = False) -> None:
    """Entry point — called from the WS subscriber for each ingested
    message that has a mediaKind. Spawned via asyncio.create_task so
    the subscriber doesn't block.

    owner_user_id is the Yorik user who owns the WhatsApp session this
    message came in on. Used by the known-sender gate so wife's
    incoming media is evaluated against her own outbound history, not
    admin's.

    force=True bypasses the known-sender gate. Used by reprocess_message
    where the user has explicitly clicked "file this" — their click IS
    the vouching signal."""
    kind = msg.get("mediaKind")
    msg_id = msg.get("id")
    chat_jid = msg.get("jid")
    if not kind or not msg_id or not chat_jid:
        return

    # WhatsApp Status broadcasts are basically Stories — 24h ephemeral
    # photos/videos from contacts. Almost no one wants them
    # auto-archived into Immich (it floods the library with stranger's
    # selfies). Default: skip media routing for status@broadcast unless
    # the user explicitly opts in via Settings → WhatsApp.
    if chat_jid == "status@broadcast" and not _status_imports_enabled() and not force:
        log.info("skipping status@broadcast media (toggle off) msg=%s", msg_id)
        return

    # Security gate: don't auto-route media from unknown senders.
    # See module docstring for the threat model. Status@broadcast above
    # is handled by its own toggle; everything else falls through here.
    # force=True (manual reprocess) skips the gate.
    if not force and chat_jid != "status@broadcast" and not _is_known_sender(chat_jid, owner_user_id):
        log.info(
            "skipping media auto-route: unknown sender chat=%s user=%s kind=%s msg=%s "
            "(not in household contacts, no prior outbound to this jid)",
            chat_jid, owner_user_id, kind, msg_id,
        )
        return

    # Skip if already processed for this target.
    with get_conn() as conn:
        row = conn.execute(
            "SELECT media_paperless_id, media_immich_id, transcript "
            "FROM wa_messages WHERE chat_jid=? AND msg_id=?",
            (chat_jid, msg_id),
        ).fetchone()
    if not row:
        return  # message not yet inserted — caller ordering bug, just skip

    try:
        if kind == "document" and not row["media_paperless_id"]:
            await _route_to_paperless(msg)
        elif kind == "image" and not row["media_immich_id"]:
            await _route_to_immich(msg, is_video=False)
        elif kind == "video" and not row["media_immich_id"]:
            await _route_to_immich(msg, is_video=True)
        elif kind == "audio" and not row["transcript"]:
            await _route_to_whisper(msg)
        # sticker / contact / location — intentionally not routed
    except MediaNotInCache:
        # Expected for historical messages — the bridge LRU only holds
        # the 1000 most recent. One log line at INFO so an admin can
        # still grep it if a reprocess seems suspicious; NO traceback
        # and NO ERROR-level entry. The 415-line burst that prompted
        # this branch was exactly this case: a WS reconnect kicked off
        # backfill for old media that the bridge had already evicted,
        # and every miss landed in /api/system/errors as a stack trace.
        log.info("skipping evicted media for %s/%s (kind=%s) — too old in bridge LRU",
                 chat_jid, msg_id, kind)
    except Exception as e:
        log.exception("media processing failed for %s/%s (kind=%s): %s",
                      chat_jid, msg_id, kind, e)


def _is_known_sender(chat_jid: str, owner_user_id: str) -> bool:
    """Trust gate for auto-routing media. A sender is "known" iff:

      1. Chat is a 1:1 DM (not a group, not status broadcast). Groups
         are never auto-routed — even attachments from trusted senders
         arriving in a 30-person group would be more noise than signal.
      2. AND either:
         a) The JID is in the household contacts table with
            status='active' (someone in the household actively added
            or replied to them — vouched), OR
         b) This owner has sent at least one message to this JID
            (proof of prior two-way relationship — the user knows them
            well enough to have reached out).

    A False return means we silently skip media routing. The message
    itself is still stored in wa_messages and visible in the chat list,
    so the user can manually file an attachment they actually want via
    the chat UI's "save to Paperless / Immich" action."""
    if not chat_jid or chat_jid == "status@broadcast":
        return False
    if chat_jid.endswith("@g.us"):
        return False

    # Household contact check. Not owner-scoped — the contacts table is
    # the household's shared address book; if any member has vouched
    # for this person, that trust extends to all members.
    try:
        from . import contacts as _contacts
        c = _contacts.find_by_channel("whatsapp", chat_jid)
        if c and (c.get("status") or "").lower() == "active":
            return True
    except Exception as e:
        log.debug("known-sender contact lookup failed for %s: %s", chat_jid, e)

    # Two-way fallback: has THIS owner sent the JID at least one message?
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM wa_messages "
            "WHERE chat_jid=? AND from_me=1 AND owner_user_id=? LIMIT 1",
            (chat_jid, owner_user_id),
        ).fetchone()
    return row is not None


class MediaNotInCache(RuntimeError):
    """Raised when the bridge has evicted a message from its 1000-item
    LRU cache before we got around to fetching its media. Distinct
    from generic RuntimeError so process_media can downgrade the log
    level — historical-sync misses are expected, not bugs."""


async def _download_from_bridge(msg_id: str) -> bytes:
    """Pull the original binary via the bridge's cached protobuf. Will
    fail if the bridge has evicted the message from its 1000-item LRU
    (it's a soft cache, not durable storage)."""
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.get(f"{BRIDGE_URL}/media/{msg_id}")
        if r.status_code == 404 and "msg_not_cached" in (r.text or ""):
            # Bridge LRU evicted this one — caller treats it as a
            # benign skip, not an exception worth a stack trace.
            raise MediaNotInCache(msg_id)
        if r.status_code != 200:
            raise RuntimeError(f"bridge /media returned {r.status_code}: {r.text[:200]}")
        return r.content


def _chat_name(jid: str) -> str:
    """Best-known display name for a chat — falls back to the JID's
    user part (the phone number without the WA suffix)."""
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM wa_chats WHERE jid=?", (jid,)).fetchone()
    if row and row["name"]:
        return row["name"]
    return jid.split("@")[0]


# ───────────────────────── Paperless route ─────────────────────────────

async def _route_to_paperless(msg: dict[str, Any]) -> None:
    # Imported lazily — both modules pull from connectors which has
    # heavier imports we don't want to pay if the user has no Paperless.
    from .connectors.paperless import _settings as _paperless_settings
    from .compose.save import _ensure_tag_ids

    s = _paperless_settings()
    if not s.get("api_key"):
        log.info("Paperless not configured — skipping doc route for %s", msg.get("id"))
        return

    content = await _download_from_bridge(msg["id"])
    filename = msg.get("filename") or f"whatsapp-{msg['id']}.bin"
    mimetype = msg.get("mimetype") or "application/octet-stream"
    contact = _chat_name(msg["jid"])
    title = f"WA · {contact} · {filename}"

    # Use _ensure_tag_ids (sync, uses `requests`) in a thread so we don't
    # block the asyncio loop.
    tag_ids = await asyncio.to_thread(_ensure_tag_ids, s, ["whatsapp", contact])

    headers = {"Authorization": f"Token {s['api_key']}"}
    files = {"document": (filename, content, mimetype)}
    data: dict[str, Any] = {"title": title}
    for tid in tag_ids:
        data.setdefault("tags", []).append(tid)

    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            f"{s['base_url']}/api/documents/post_document/",
            headers=headers, files=files, data=data,
        )
        if not r.ok:
            log.warning("Paperless upload failed: %s %s", r.status_code, r.text[:200])
            return
        task_id = r.text.strip().strip('"')

    # task_id is a UUID — the post-consume webhook (already wired) will
    # backfill the real doc id once OCR finishes. For our purposes, the
    # task id is enough to prove the upload succeeded.
    with get_conn() as conn:
        conn.execute(
            "UPDATE wa_messages SET media_paperless_id=? WHERE chat_jid=? AND msg_id=?",
            (task_id, msg["jid"], msg["id"]),
        )
        conn.commit()
    log.info("WA doc → Paperless (task=%s) from %s", task_id, contact)


# ───────────────────────── Immich route ────────────────────────────────

async def _route_to_immich(msg: dict[str, Any], is_video: bool) -> None:
    from . import credential_store

    creds = credential_store.get("immich") or {}
    api_key = creds.get("api_key")
    if not api_key:
        log.info("Immich not configured — skipping %s route for %s",
                 "video" if is_video else "image", msg.get("id"))
        return
    base_url = (creds.get("base_url") or "http://localhost:2283").rstrip("/")

    content = await _download_from_bridge(msg["id"])
    default_ext = "mp4" if is_video else "jpg"
    filename = msg.get("filename") or f"whatsapp-{msg['id']}.{default_ext}"
    mimetype = msg.get("mimetype") or ("video/mp4" if is_video else "image/jpeg")
    ts = msg.get("timestamp") or int(datetime.now(timezone.utc).timestamp())
    iso = datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"

    headers = {"x-api-key": api_key, "Accept": "application/json"}
    files = {"assetData": (filename, content, mimetype)}
    # deviceAssetId + deviceId are Immich's dedup key. Using the msg_id
    # means a re-process of the same WA message won't create a duplicate
    # in Immich (it'll return the existing asset id).
    data = {
        "deviceAssetId": f"whatsapp-{msg['id']}",
        "deviceId": "yorik-whatsapp",
        "fileCreatedAt": iso,
        "fileModifiedAt": iso,
    }

    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(f"{base_url}/api/assets", headers=headers, files=files, data=data)
        if not r.ok:
            log.warning("Immich upload failed: %s %s", r.status_code, r.text[:200])
            return
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        immich_id = body.get("id")

    if not immich_id:
        log.warning("Immich upload returned no id: %s", body)
        return

    with get_conn() as conn:
        conn.execute(
            "UPDATE wa_messages SET media_immich_id=? WHERE chat_jid=? AND msg_id=?",
            (immich_id, msg["jid"], msg["id"]),
        )
        conn.commit()
    log.info("WA %s → Immich (id=%s) from %s",
             "video" if is_video else "image", immich_id, _chat_name(msg["jid"]))


# ───────────────────────── Whisper route ───────────────────────────────

async def _route_to_whisper(msg: dict[str, Any]) -> None:
    from . import voice  # heavy: loads whisper model on first call

    content = await _download_from_bridge(msg["id"])

    # WhatsApp voice notes are usually OGG/Opus; Whisper (via ffmpeg)
    # handles anything but a correct file extension keeps the temp
    # cleanup observable.
    mime = (msg.get("mimetype") or "").lower()
    suffix = ".ogg"
    if "mpeg" in mime or "mp3" in mime: suffix = ".mp3"
    elif "wav" in mime:                  suffix = ".wav"
    elif "mp4" in mime or "m4a" in mime: suffix = ".m4a"

    fd, path = tempfile.mkstemp(suffix=suffix, prefix="wa-voice-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        # Whisper is CPU-bound + slow — run in thread so the event loop
        # stays responsive for other messages arriving meanwhile.
        transcript = await asyncio.to_thread(voice.transcribe, path)
        if not transcript or not transcript.strip():
            return
        with get_conn() as conn:
            conn.execute(
                "UPDATE wa_messages SET transcript=? WHERE chat_jid=? AND msg_id=?",
                (transcript.strip(), msg["jid"], msg["id"]),
            )
            conn.commit()
        log.info("WA voice → transcribed %d chars from %s", len(transcript), _chat_name(msg["jid"]))
        # Index the transcript so semantic search can find spoken
        # content too ("anyone left me a voice note about the gas bill").
        try:
            from . import whatsapp_semantic as _sem
            await asyncio.to_thread(
                _sem.index_message,
                msg_id=msg["id"], chat_jid=msg["jid"],
                text=transcript.strip(), ts=int(msg.get("timestamp") or 0),
                push_name=msg.get("pushName"), from_me=False,
            )
        except Exception as e:
            log.debug("voice transcript semantic index failed: %s", e)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ───────────────────────── Re-process API ──────────────────────────────

async def reprocess_message(chat_jid: str, msg_id: str) -> dict[str, Any]:
    """Manually re-trigger media processing for one message. Used by
    POST /api/whatsapp/messages/<msg_id>/reprocess — handy when the
    user later configures Paperless/Immich, or one of them was down
    when the message originally arrived."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT msg_id, chat_jid, media_kind, mimetype, filename, "
            "       media_paperless_id, media_immich_id, transcript "
            "FROM wa_messages WHERE chat_jid=? AND msg_id=?",
            (chat_jid, msg_id),
        ).fetchone()
    if not row:
        return {"ok": False, "error": "message not found"}
    if not row["media_kind"]:
        return {"ok": False, "error": "no media on this message"}

    # Reset the target id so process_media doesn't skip the work.
    with get_conn() as conn:
        if row["media_kind"] == "document":
            conn.execute(
                "UPDATE wa_messages SET media_paperless_id=NULL WHERE chat_jid=? AND msg_id=?",
                (chat_jid, msg_id),
            )
        elif row["media_kind"] in ("image", "video"):
            conn.execute(
                "UPDATE wa_messages SET media_immich_id=NULL WHERE chat_jid=? AND msg_id=?",
                (chat_jid, msg_id),
            )
        elif row["media_kind"] == "audio":
            conn.execute(
                "UPDATE wa_messages SET transcript=NULL WHERE chat_jid=? AND msg_id=?",
                (chat_jid, msg_id),
            )
        conn.commit()

    fake_msg = {
        "id": msg_id,
        "jid": chat_jid,
        "mediaKind": row["media_kind"],
        "mimetype": row["mimetype"],
        "filename": row["filename"],
    }
    # Manual reprocess = user is vouching for this attachment themselves,
    # so bypass the known-sender gate.
    await process_media(fake_msg, force=True)

    # Read state back to report what landed.
    with get_conn() as conn:
        out = conn.execute(
            "SELECT media_paperless_id, media_immich_id, transcript "
            "FROM wa_messages WHERE chat_jid=? AND msg_id=?",
            (chat_jid, msg_id),
        ).fetchone()
    return {"ok": True, **dict(out)}
