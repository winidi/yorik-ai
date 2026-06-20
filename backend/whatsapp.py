"""WhatsApp integration for Yorik — talks to the Baileys bridge over
REST + WebSocket, persists messages in SQLite, exposes typed FastAPI
routes to the frontend.

Wave 1 scope:
- Subscribe to bridge /events WS as a background task at app startup.
- Ingest incoming messages into wa_chats + wa_messages.
- Proxy chat list, thread messages, send, and QR endpoints through
  FastAPI (so the browser only ever talks to Yorik on :8000, not to the
  bridge directly — keeps the cross-origin story simple).
- Draft generation lives in this module too (small enough for v1; will
  split into backend/whatsapp/draft.py if it grows).

owner_user_id is hard-stubbed to 1 (admin) throughout. When real auth
ships, this becomes "the logged-in user's id" and the existing schema
already has the column — pure migration, no structural change.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from backend.database import get_conn
from backend import auth_sessions as _auth

log = logging.getLogger("yorik.whatsapp")

BRIDGE_URL = os.getenv("YORIK_WA_BRIDGE_URL", "http://127.0.0.1:3015")
BRIDGE_WS  = os.getenv("YORIK_WA_BRIDGE_WS",  "ws://127.0.0.1:3015/events")

# Fallback owner used by legacy single-tenant paths where no logged-in
# user is available (e.g. /clear without auth context, the legacy WS
# subscriber, fixture/seed code). Multi-tenant code paths now extract
# owner_user_id from either Depends(_auth.current_user) (REST routes)
# or evt["userId"] (bridge events).
DEFAULT_OWNER = 1


def _bridge_url(suffix: str, user_id: str | str | None) -> str:
    """Build a user-scoped bridge URL. `suffix` is the per-route path
    (must start with /) — e.g. "/chats/abc@s.whatsapp.net/send".
    user_id=None falls back to the legacy admin user via the bridge's
    backward-compat shim, so callers that don't yet have a user context
    keep working unchanged."""
    if user_id is None:
        return f"{BRIDGE_URL}{suffix}"
    return f"{BRIDGE_URL}/users/{user_id}{suffix}"


# Module-level so the lifespan task can cancel cleanly on shutdown.
_ws_task: Optional[asyncio.Task] = None

# Per-user browser WebSocket clients. Each user's open tabs subscribe
# here; ingested bridge events are fanned out only to the matching
# user's tabs, so the wife's open chat list never blinks because the
# admin received a message. dict[int, set[WebSocket]] — int is the
# Yorik user_id.
_browser_ws: dict[int, set[WebSocket]] = {}

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


async def _broadcast_to_browsers(payload: dict[str, Any], user_id: str | None = None) -> None:
    """Best-effort fan-out scoped by user. If user_id is given, only
    that user's open tabs receive the broadcast. If None, falls back to
    fan-out across all users (used by routes that don't have user
    context yet — kept for transitional safety). Drops any sockets that
    error out (they've closed but their close handler hasn't fired)."""
    if not _browser_ws:
        return
    msg = json.dumps(payload)
    targets: list[tuple[int, WebSocket]] = []
    if user_id is None:
        for uid, conns in _browser_ws.items():
            for ws in conns:
                targets.append((uid, ws))
    else:
        for ws in _browser_ws.get(user_id, set()):
            targets.append((user_id, ws))
    dead: list[tuple[int, WebSocket]] = []
    for uid, ws in targets:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append((uid, ws))
    for uid, ws in dead:
        conns = _browser_ws.get(uid)
        if conns is not None:
            conns.discard(ws)
            if not conns:
                _browser_ws.pop(uid, None)


# ─────────────────────────── ingestion ─────────────────────────────────

def _get_self_pushname(conn, owner_user_id: str) -> Optional[str]:
    """Cached lookup of the user's WhatsApp pushname (e.g. 'Tom'),
    captured on every bridge `ready` event. Returns None if the user
    hasn't paired yet."""
    row = conn.execute(
        "SELECT pushname FROM wa_self_identity WHERE owner_user_id=?",
        (owner_user_id,),
    ).fetchone()
    return row["pushname"] if row and row["pushname"] else None


def _is_phantom_self_chat(jid: Optional[str], name: Optional[str],
                          owner_user_id: str, conn) -> bool:
    """Heuristic: a chat keyed to an `@lid` JID whose name matches the
    user's own pushName is almost always one of WhatsApp's per-contact
    privacy-LID ghost chats — sending a message from your phone surfaces
    a parallel chat for the user's LID-self with the outgoing message
    inside, on top of the real recipient's chat. Real LID chats (groups,
    privacy-enabled contacts) keep their counterparty's name, not yours.

    Conservative: requires BOTH @lid format AND name=self. Real contact
    named "Tom" who uses LID would slip through this filter, but the
    odds of a real contact carrying your exact pushName AND being one
    of the small fraction of LID chats are very low."""
    if not jid or not jid.endswith("@lid"):
        return False
    if not name:
        return False
    self_name = _get_self_pushname(conn, owner_user_id)
    if not self_name:
        return False
    return name.strip().casefold() == self_name.strip().casefold()


def _upsert_chat(jid: str, name: Optional[str], is_group: bool, ts: Optional[int],
                 last_text: Optional[str], owner_user_id: str = DEFAULT_OWNER) -> None:
    """Idempotent chat upsert. Bumps last_message_ts only if the new ts is
    newer; name always backfills (COALESCE-on-NULL semantics so a name
    update never overwrites an existing name with NULL)."""
    with get_conn() as conn:
        if _is_phantom_self_chat(jid, name, owner_user_id, conn):
            log.info("wa: skipping phantom self-LID chat jid=%s name=%s", jid, name)
            return
        row = conn.execute("SELECT last_message_ts FROM wa_chats WHERE jid=?", (jid,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO wa_chats (jid, name, is_group, last_message_ts, last_message_text, owner_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (jid, name, 1 if is_group else 0, ts, last_text, owner_user_id),
            )
        else:
            prev = row["last_message_ts"] or 0
            if (ts or 0) >= prev:
                # Newer activity — update ts + preview text + (re-)backfill name.
                conn.execute(
                    "UPDATE wa_chats SET name=COALESCE(?, name), last_message_ts=?, "
                    "last_message_text=? WHERE jid=?",
                    (name, ts, last_text, jid),
                )
            elif name:
                # Older event (or chat-only update) but it has a name — still
                # backfill it so contact updates land even on stale events.
                conn.execute(
                    "UPDATE wa_chats SET name=COALESCE(?, name) WHERE jid=?",
                    (name, jid),
                )
        conn.commit()


def _insert_message(m: dict[str, Any], owner_user_id: str = DEFAULT_OWNER) -> None:
    """Insert one message (idempotent on (chat_jid, msg_id)). Skips empty
    meta-messages — anything with no text AND no media is a protocol
    artefact (encryption handshake, reaction, poll update, etc.) that
    the bridge serializer should've already filtered, but we defend in
    depth so a future serializer bug can't pollute the chat list."""
    jid = m.get("jid")
    msg_id = m.get("id")
    if not jid or not msg_id:
        return
    if not m.get("text") and not m.get("mediaKind"):
        return  # nothing to show — silent skip
    with get_conn() as conn:
        # Don't persist messages keyed to one of the user's phantom
        # self-LIDs. Same message will already be coming in (or has come
        # in) on the recipient's real @s.whatsapp.net chat with fromMe=1.
        if _is_phantom_self_chat(jid, m.get("pushName"), owner_user_id, conn):
            log.info("wa: skipping phantom self-LID message jid=%s msg=%s", jid, msg_id)
            return
        conn.execute(
            "INSERT OR IGNORE INTO wa_messages "
            "(msg_id, chat_jid, from_me, participant, push_name, timestamp, "
            " text, media_kind, mimetype, filename, owner_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id, jid,
                1 if m.get("fromMe") else 0,
                m.get("participant"),
                m.get("pushName"),
                int(m.get("timestamp") or 0),
                m.get("text"),
                m.get("mediaKind"),
                m.get("mimetype"),
                m.get("filename"),
                owner_user_id,
            ),
        )
        conn.commit()
    # Bump the chat's recency.
    _upsert_chat(
        jid=jid,
        name=m.get("pushName"),
        is_group=jid.endswith("@g.us"),
        ts=int(m.get("timestamp") or 0),
        last_text=m.get("text") or _media_placeholder(m),
        owner_user_id=owner_user_id,
    )

    # Contacts autocapture — incoming 1:1 WhatsApp messages from unknown
    # senders park a pending contact. Skipped for fromMe (sent by user)
    # and for groups (the jid is the group's, not the sender's). The
    # autocapture itself filters out @lid / @newsletter / @broadcast
    # pseudo-jids; we pass the FULL jid so the channel value stays
    # unambiguous (storing just digits caused the "Tom → brother"
    # cross-routing bug).
    if not m.get("fromMe") and not jid.endswith("@g.us"):
        try:
            from . import contact_autocapture
            contact_autocapture.on_inbound_whatsapp(
                from_jid=jid,
                from_name=m.get("pushName") or "",
            )
        except Exception as exc:
            log.debug("WA contact_autocapture failed: %s", exc)


def _media_placeholder(m: dict[str, Any]) -> Optional[str]:
    """Generate a stand-in last-message preview for media-only messages."""
    kind = m.get("mediaKind")
    if not kind:
        return None
    return {
        "image":    "📷 Photo",
        "video":    "🎥 Video",
        "audio":    "🎙️ Voice message",
        "document": f"📄 {m.get('filename') or 'Document'}",
        "sticker":  "🩷 Sticker",
    }.get(kind, f"[{kind}]")


# ─────────────────────────── WS subscriber ─────────────────────────────

async def _ws_subscriber() -> None:
    """Long-lived: connect to bridge /events, ingest messages, reconnect on drop."""
    # websockets is a separate dep; using httpx-ws would be nicer but it's
    # not pinned in requirements yet. Use the std-library websockets pkg.
    from . import workers
    # Event-driven WS subscriber — heartbeats only on connect / inbound
    # message / status flip. A quiet day can legitimately go hours
    # without a tick; mark stale after ~5 hours of true silence.
    workers.register("whatsapp_subscriber", kind="subscriber",
                     expected_interval_s=3600)
    try:
        import websockets
    except ImportError:
        log.error("websockets package not installed — WA ingestion disabled. "
                  "Add `websockets>=12` to backend/requirements.txt.")
        workers.report_error("whatsapp_subscriber", "websockets package missing")
        return

    backoff = 1.0
    # Log spam dampening: a fresh install has the bridge container
    # running but unpaired — connection refused storms the API log
    # every 1–30 seconds forever. WARN once at the start of each new
    # outage, then drop to DEBUG until reconnection. Heartbeat still
    # captures the state for the workers dashboard either way.
    last_was_connected = True
    while True:
        try:
            log.info("connecting to bridge WS at %s", BRIDGE_WS)
            async with websockets.connect(BRIDGE_WS, ping_interval=30) as ws:
                backoff = 1.0
                last_was_connected = True
                workers.heartbeat("whatsapp_subscriber", "ok",
                                  "connected to Baileys bridge")
                async for raw in ws:
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await _handle_event(evt)
                    workers.heartbeat("whatsapp_subscriber", "ok",
                                      f"processed event: {evt.get('type', '?')}")
        except asyncio.CancelledError:
            log.info("WS subscriber cancelled, exiting cleanly")
            workers.report_error("whatsapp_subscriber", "cancelled")
            raise
        except Exception as e:
            if last_was_connected:
                log.warning("bridge WS failed (%s); retrying in %.1fs", e, backoff)
                last_was_connected = False
            else:
                log.debug("bridge WS still down (%s); retrying in %.1fs", e, backoff)
            workers.heartbeat("whatsapp_subscriber", "warn",
                              f"bridge unreachable, retry in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 30.0)


async def _handle_event(evt: dict[str, Any]) -> None:
    t = evt.get("type")
    p = evt.get("payload") or {}
    # Bridge tags every event with the userId of the WhatsApp session
    # it came from — a UUID string matching the bridge's per-user
    # session storage and our user_profiles.id. Older code coerced to
    # int with DEFAULT_OWNER fallback, which meant every UUID event
    # silently re-attributed to user 1 and the per-user WS fan-out
    # (_browser_ws keyed by uuid) never matched. Drop events without
    # a userId rather than silently mis-attributing.
    owner_user_id = evt.get("userId")
    if not owner_user_id:
        log.warning("bridge event missing userId, skipping type=%s", t)
        return
    # Fan out to browser WS clients FIRST so the UI updates as fast as
    # possible. Scoped by owner_user_id — only that user's tabs see it.
    await _broadcast_to_browsers({"type": t, "payload": p}, user_id=owner_user_id)
    if t == "message":
        _insert_message(p, owner_user_id=owner_user_id)
        # Auto-route media: PDFs → Paperless, images/video → Immich,
        # voice notes → Whisper transcript. Spawned, not awaited, so
        # a slow OCR/upload/transcribe pass never backs up the WS
        # subscriber. owner_user_id flows through so the known-sender
        # gate evaluates against the right user's history.
        if p.get("mediaKind"):
            from . import whatsapp_media
            asyncio.create_task(whatsapp_media.process_media(p, owner_user_id=owner_user_id))
        # Semantic indexing: embed text messages into wa_vec so the
        # draft generator can do meaning-based cross-chat retrieval.
        # Voice notes get re-indexed later once their transcript lands
        # (whatsapp_media calls back into the indexer after Whisper).
        if p.get("text"):
            from . import whatsapp_semantic as _sem
            asyncio.create_task(asyncio.to_thread(
                _sem.index_message,
                msg_id=p.get("id"), chat_jid=p.get("jid"),
                text=p.get("text"), ts=int(p.get("timestamp") or 0),
                push_name=p.get("pushName"), from_me=bool(p.get("fromMe")),
            ))
        # Auto-draft on incoming, discard on outgoing-from-other-device.
        # Auto-drafting on every incoming message was removed — drafts
        # are now user-triggered via the state-button panel (see
        # /chats/{jid}/draft-options). Still discard any leftover
        # pending drafts when the user replies from another device, so
        # the panel stays clean while transitional drafts age out.
        from . import whatsapp_autodraft as _ad
        if p.get("fromMe"):
            killed = _ad.discard_on_manual_reply(p, owner_user_id=owner_user_id)
            if killed:
                await _broadcast_to_browsers({
                    "type": "drafts_updated",
                    "payload": {"chat_jid": p.get("jid"), "discarded": killed,
                                "reason": "manual_reply"},
                }, user_id=owner_user_id)
    elif t == "chat":
        # Bridge sends this on history sync, contacts.upsert, and chat
        # name updates. Just refresh the chat row's name/group flag.
        jid = p.get("jid")
        name = p.get("name")
        if jid:
            _upsert_chat(
                jid=jid,
                name=name,
                is_group=bool(p.get("isGroup")),
                ts=p.get("lastMessageTs"),
                last_text=None,
                owner_user_id=owner_user_id,
            )
    elif t == "ready":
        me = p.get("me") or {}
        log.info("bridge reports WA ready for user=%s: %s", owner_user_id, me)
        # Capture user's WhatsApp identity so we can later filter the
        # phantom @lid self-chats WhatsApp's privacy system creates.
        me_jid = me.get("id")
        pushname = me.get("name")
        if me_jid or pushname:
            with get_conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO wa_self_identity "
                    "(owner_user_id, me_jid, pushname, updated_at) VALUES (?, ?, ?, ?)",
                    (owner_user_id, me_jid, pushname, int(time.time())),
                )
                conn.commit()
    elif t == "qr":
        log.info("bridge reports new QR available for user=%s", owner_user_id)
    elif t == "disconnected":
        log.warning("bridge reports WA disconnected for user=%s: %s", owner_user_id, p)


def start_background(loop: asyncio.AbstractEventLoop) -> None:
    """Called from FastAPI startup. Schedules the WS subscriber task."""
    global _ws_task
    if _ws_task and not _ws_task.done():
        return
    _ws_task = loop.create_task(_ws_subscriber(), name="yorik-wa-subscriber")


async def stop_background() -> None:
    if _ws_task and not _ws_task.done():
        _ws_task.cancel()
        try:
            await _ws_task
        except asyncio.CancelledError:
            pass


# ─────────────────────────── REST routes ───────────────────────────────

class SendBody(BaseModel):
    text: str
    # If the text came from (or was derived from) an auto-draft variant,
    # the UI passes the variant's wa_drafts.id so we can mark it as
    # "used" and discard the sibling variants. Optional — manual sends
    # (typed from scratch) leave this null.
    draft_id: Optional[int] = None


class DraftBody(BaseModel):
    chat_jid: str
    extra_instructions: Optional[str] = None  # user can nudge tone/intent


@router.get("/status")
async def status(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Pass-through to bridge /status for the logged-in user's session.
    Returns connected/me/hasQr. If the user has never paired (no session
    on disk), returns connected=false with exists=false so the UI can
    offer a 'Pair your phone' button."""
    uid = user["id"]
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            # Eagerly start the session if it isn't running yet — auto-
            # creates the empty auth dir on the bridge so the next QR
            # request has a session to attach to.
            try:
                await c.post(_bridge_url("/start", uid))
            except Exception:
                pass
            r = await c.get(_bridge_url("/status", uid))
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"connected": False, "bridge_unreachable": True, "detail": str(e)}


# ─── Bridge container management ─────────────────────────────────────
#
# When the WhatsApp bridge container isn't running, the UI's pairing
# modal previously printed `docker compose up -d whatsapp-bridge` and
# made the user run it themselves. These three endpoints + the matching
# button in the modal let admins start / restart the container without
# leaving the app. Profile-gated (`bundled-whatsapp`) — non-bundled
# installs (BYO bridge on another host) get a clean error message.

@router.get("/bridge/info")
def bridge_info() -> dict[str, Any]:
    """Container + docker state for the WhatsApp bridge, so the UI can
    show the right action ("Start" vs "Restart" vs "Docker not installed").

    States returned via `container_state`:
      running     — up; bridge should be reachable on its port
      exited      — created but stopped; "Start" brings it back
      restarting  — docker compose is rolling it; "Wait"
      absent      — never created; first `up` will build the image
      unknown     — probe failed or docker not available
    """
    import subprocess
    info: Dict[str, Any] = {
        "docker_available":  False,
        "compose_available": False,
        "container_state":   "unknown",
        "container_exists":  False,
        "container_name":    "yorik-whatsapp-bridge",
    }
    try:
        r = subprocess.run(["docker", "--version"], capture_output=True, timeout=3)
        info["docker_available"] = (r.returncode == 0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return info
    if not info["docker_available"]:
        return info

    # Compose CLI probe — distinguishes "docker installed but no compose"
    # (rare) so the UI can hint to install compose.
    from .storage import _compose_command
    info["compose_available"] = _compose_command() is not None

    # Container inspect — fastest reliable way to read state.
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", info["container_name"]],
            capture_output=True, timeout=3, text=True,
        )
        if r.returncode == 0:
            info["container_state"] = (r.stdout or "").strip() or "unknown"
            info["container_exists"] = True
        else:
            info["container_state"] = "absent"
            info["container_exists"] = False
    except subprocess.TimeoutExpired:
        info["container_state"] = "unknown"
    return info


def _run_compose_bridge(args: List[str], timeout: int = 300) -> None:
    """Background task: run a compose command against the whatsapp-bridge
    service. Logs the result; nothing surfaced to the request thread
    since this runs after FastAPI returned."""
    import subprocess
    from .storage import _compose_command, _project_root
    cmd = _compose_command()
    if not cmd:
        log.warning("whatsapp bridge: compose CLI not found; aborting %s", args)
        return
    full = cmd + args
    log.info("whatsapp bridge: %s", " ".join(full))
    try:
        r = subprocess.run(
            full, cwd=_project_root(),
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            log.warning("whatsapp bridge: command failed (%d): %s",
                        r.returncode, (r.stderr or r.stdout)[:600])
        else:
            log.info("whatsapp bridge: command ok")
    except subprocess.TimeoutExpired:
        log.warning("whatsapp bridge: command timed out (>%ds)", timeout)


@router.post("/bridge/start")
def bridge_start(
    background: BackgroundTasks,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Bring the WhatsApp bridge container up via docker compose. Admin
    only. Returns immediately; the actual `compose up` (and first-run
    image build, which can take a minute or two) runs in the background.
    UI should poll /api/whatsapp/status until bridge_unreachable=false."""
    if (user.get("role") or "") != "admin":
        raise HTTPException(403, "admin only")
    from .storage import _compose_command
    if _compose_command() is None:
        raise HTTPException(503, "docker compose CLI not found on PATH (install Docker + the Compose plugin)")
    # --profile bundled-whatsapp because the service is profile-gated.
    background.add_task(
        _run_compose_bridge,
        ["--profile", "bundled-whatsapp", "up", "-d", "whatsapp-bridge"],
        300,
    )
    return {"queued": True}


@router.post("/bridge/restart")
def bridge_restart(
    background: BackgroundTasks,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Restart the WhatsApp bridge container — used when it's running
    but unreachable (rare: bridge crashed inside the container, websocket
    deadlock, etc.). Cheaper + faster than `up` because the image and
    volumes are already there."""
    if (user.get("role") or "") != "admin":
        raise HTTPException(403, "admin only")
    from .storage import _compose_command
    if _compose_command() is None:
        raise HTTPException(503, "docker compose CLI not found on PATH")
    background.add_task(
        _run_compose_bridge,
        ["restart", "whatsapp-bridge"],
        60,
    )
    return {"queued": True}


@router.post("/cleanup-phantom-chats")
def cleanup_phantom_chats(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Delete existing phantom self-LID chats and their messages —
    cleanup for installs that ran before _is_phantom_self_chat landed.
    Matches the same heuristic (@lid + name=pushName) so it can't take
    out real chats. Reports counts.

    Idempotent: re-running after everything is clean is a no-op."""
    uid = user["id"]
    with get_conn() as conn:
        self_name = _get_self_pushname(conn, uid)
        if not self_name:
            return {"deleted_chats": 0, "deleted_messages": 0,
                    "note": "no pushname captured yet (pair the bridge first)"}
        # Find the phantoms first so we can return what we touched.
        rows = conn.execute(
            "SELECT jid FROM wa_chats WHERE owner_user_id=? "
            "AND jid LIKE '%@lid' AND LOWER(TRIM(name)) = LOWER(TRIM(?))",
            (uid, self_name),
        ).fetchall()
        phantom_jids = [r["jid"] for r in rows]
        if not phantom_jids:
            return {"deleted_chats": 0, "deleted_messages": 0,
                    "matched_pushname": self_name}
        placeholders = ",".join("?" * len(phantom_jids))
        msg_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM wa_messages WHERE chat_jid IN ({placeholders})",
            phantom_jids,
        ).fetchone()["n"]
        conn.execute(
            f"DELETE FROM wa_messages WHERE chat_jid IN ({placeholders})",
            phantom_jids,
        )
        conn.execute(
            f"DELETE FROM wa_chats WHERE jid IN ({placeholders})",
            phantom_jids,
        )
        conn.commit()

    # Cascade: also nuke any contact_channels rows pointing at the
    # phantom JIDs. Otherwise a contact (e.g. autocaptured "Sara" with
    # channel = 64373087281262@lid) inherits a tombstoned link forever:
    # the chat is gone but the channel still claims the JID, so message
    # lookups by contact return 0 results permanently. The enricher saw
    # this in the wild — contact_enrichment_proposals dev report flagged
    # contacts pointing at one of Tom's phantom LIDs.
    channels_deleted = 0
    try:
        from .contacts import conn_ctx as _ccx
        with _ccx() as cc:
            placeholders_c = ",".join("?" * len(phantom_jids))
            cur = cc.execute(
                f"DELETE FROM contact_channels "
                f"WHERE kind='whatsapp' AND value IN ({placeholders_c})",
                phantom_jids,
            )
            channels_deleted = cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        log.exception("wa: phantom-chat channel cascade failed: %s", exc)

    log.info("wa: cleaned up %d phantom chat(s) + %d message(s) + %d contact channel(s) for user=%s",
             len(phantom_jids), msg_count, channels_deleted, uid)
    return {
        "deleted_chats":           len(phantom_jids),
        "deleted_messages":        int(msg_count),
        "deleted_contact_channels": channels_deleted,
        "matched_pushname":        self_name,
        "jids":                    phantom_jids,
    }


@router.get("/media/{msg_id}")
async def get_media(
    msg_id: str,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Response:
    """Proxy decrypted media bytes from the bridge to the UI. Used by
    <img>/<video> tags in the WhatsApp app's message renderer for any
    message with media_kind set.

    The bridge (whatsapp-bridge/server.js:557 GET /users/:userId/media/:msgId)
    runs Baileys' downloadMediaMessage to decrypt the bytes; we forward
    content-type as-is so the browser renders the right element.
    msg_id is immutable for the message's lifetime, so browser caching
    is safe (1 day).
    """
    uid = user["id"]
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(_bridge_url(f"/media/{msg_id}", uid))
            if r.status_code == 404:
                raise HTTPException(404, "media not found — may have expired on WhatsApp's servers")
            if r.status_code != 200:
                raise HTTPException(r.status_code, f"bridge error: {r.text[:200]}")
            return Response(
                content=r.content,
                media_type=r.headers.get("content-type", "application/octet-stream"),
                headers={"Cache-Control": "private, max-age=86400"},
            )
    except httpx.RequestError as e:
        raise HTTPException(502, f"bridge unreachable: {e}")


@router.get("/qr")
async def qr(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> Response:
    """Return the pairing QR for the logged-in user (204 if already paired)."""
    uid = user["id"]
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            # Make sure the user's session is started so a QR can be generated.
            try:
                await c.post(_bridge_url("/start", uid))
            except Exception:
                pass
            r = await c.get(_bridge_url("/qr", uid))
            if r.status_code == 204:
                return Response(status_code=204)
            if r.status_code != 200:
                raise HTTPException(r.status_code, r.text)
            return Response(content=r.content, media_type="application/json")
    except httpx.RequestError as e:
        raise HTTPException(502, f"WhatsApp bridge unreachable — start yorik-whatsapp-bridge ({e})")


@router.get("/settings")
def get_wa_settings() -> dict[str, Any]:
    """Read user-controllable WhatsApp toggles. Used by the Settings →
    WhatsApp panel. Currently one flag (status auto-import); easy to
    extend with more without schema churn (each flag is a separate
    app_settings key)."""
    from . import whatsapp_media
    return {
        "import_status_broadcasts": whatsapp_media._status_imports_enabled(),
    }


class WaSettings(BaseModel):
    """Partial update for WhatsApp toggles. Open shape so we can add
    fields without versioning the route."""
    import_status_broadcasts: Optional[bool] = None


@router.patch("/settings")
def patch_wa_settings(body: WaSettings) -> dict[str, Any]:
    """Flip a WhatsApp toggle. Returns the post-update state for the
    caller to confirm. Admin gate is implicit via the chat-app auth
    already applied to the router."""
    from . import whatsapp_media
    if body.import_status_broadcasts is not None:
        whatsapp_media.set_status_imports_enabled(body.import_status_broadcasts)
    return get_wa_settings()


@router.post("/import")
async def import_chat_export(
    file: UploadFile = File(...),
    chat_jid: Optional[str] = Form(None),
) -> dict[str, Any]:
    """Import a WhatsApp "Export Chat" file.

    Accepts:
      - .txt — the exported chat log on its own (no media variant)
      - .zip — txt + media files (we ingest the txt; media is stubbed)

    The chat_jid defaults to a synthetic "import_<hash>@yorik.local"
    derived from the file name + first contact found, so re-importing
    the same export is idempotent. The user can also explicitly pin
    the import to an existing chat by passing chat_jid.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    # Pull the .txt out (either it's the upload itself, or it's inside a zip).
    txt: Optional[str] = None
    if file.filename and file.filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for name in z.namelist():
                    if name.lower().endswith(".txt"):
                        txt = z.read(name).decode("utf-8", errors="replace")
                        break
        except zipfile.BadZipFile:
            raise HTTPException(400, "not a valid zip file")
        if not txt:
            raise HTTPException(400, "no .txt found inside zip")
    else:
        txt = raw.decode("utf-8", errors="replace")

    messages, contact_name = _parse_chat_export(txt)
    if not messages:
        raise HTTPException(400, "no messages parsed — is this a WhatsApp Export Chat file?")

    if not chat_jid:
        # Synth a stable JID so re-import doesn't duplicate. Use first
        # non-Me sender + file content hash as the seed.
        seed = (contact_name or "unknown") + hashlib.sha1(raw).hexdigest()[:8]
        chat_jid = f"import_{re.sub(r'[^a-z0-9]', '_', seed.lower())[:40]}@yorik.local"

    _upsert_chat(
        jid=chat_jid,
        name=contact_name or chat_jid,
        is_group=False,
        ts=messages[-1]["timestamp"] if messages else None,
        last_text=messages[-1].get("text") if messages else None,
    )

    inserted = 0
    for m in messages:
        m["jid"] = chat_jid
        _insert_message(m)
        inserted += 1

    return {
        "chat_jid": chat_jid,
        "contact_name": contact_name,
        "messages_parsed": len(messages),
        "messages_inserted": inserted,
    }


# WhatsApp Export Chat format examples:
#   [3/15/24, 10:23:45 PM] John Doe: Hey, how are you?
#   3/15/24, 22:23 - John Doe: Hey, how are you?     (24h, Android)
#   ‎[3/15/24, 10:23:45 PM] John Doe: ‎<Media omitted>
# Multi-line messages continue without a date prefix until the next
# date-prefixed line. System messages: "Messages and calls are end-to-end
# encrypted..." — we skip these.
_EXPORT_LINE_RE = re.compile(
    r"^[‎‏\[]?\s*"                                        # optional LRM/RLM + opening bracket
    r"(?P<date>\d{1,2}[./]\d{1,2}[./]\d{2,4})"                      # date
    r"[, ]+"                                                         # comma/space sep
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)"              # time (12 or 24h)
    r"\]?\s*[-–—]?\s*"                                              # closing bracket + dash
    r"(?P<sender>[^:]+?):\s+"                                       # sender name (no colon)
    r"(?P<text>.*)$",
    re.IGNORECASE,
)
_EXPORT_SYSTEM_PREFIXES = (
    "messages and calls are end-to-end encrypted",
    "missed voice call",
    "missed video call",
    "you deleted this message",
    "this message was deleted",
    "<media omitted>",
    "image omitted",
    "video omitted",
    "audio omitted",
    "document omitted",
    "sticker omitted",
    "gif omitted",
)


def _parse_chat_export(text: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Parse a WhatsApp Export Chat .txt into a list of message dicts
    matching the bridge's serializeMessage output shape. Returns
    (messages, primary_contact_name)."""
    messages: list[dict[str, Any]] = []
    senders: dict[str, int] = {}
    current: Optional[dict[str, Any]] = None
    for raw_line in text.splitlines():
        # Strip BOM / LRM / RLM control chars — WA exports are riddled with these.
        line = raw_line.lstrip("﻿‎‏").rstrip()
        if not line:
            if current:
                current["text"] = (current.get("text") or "") + "\n"
            continue
        m = _EXPORT_LINE_RE.match(line)
        if not m:
            # Continuation of previous message
            if current is not None:
                current["text"] = (current.get("text") or "") + "\n" + line
            continue
        # New message line — flush current first
        if current is not None:
            _finalize_export_msg(current, messages)
        sender = m.group("sender").strip()
        text_field = m.group("text").strip().lstrip("‎").strip()
        ts = _parse_export_ts(m.group("date"), m.group("time"))
        # System messages have no real sender — heuristic: line text starts
        # with one of the known system prefixes AND sender contains "messages"
        # OR sender is the same as the WA "system" pseudo-account.
        low = text_field.lower()
        is_system = any(low.startswith(p) for p in _EXPORT_SYSTEM_PREFIXES) and not _looks_like_real_sender(sender)
        if is_system:
            current = None
            continue
        # Detect media stubs ("<Media omitted>", "image omitted", etc.) and
        # mark them with media_kind so the UI shows a placeholder rather
        # than the literal stub text.
        media_kind = _media_kind_from_stub(text_field)
        current = {
            "id": _stable_msg_id(sender, ts, text_field),
            "fromMe": _is_me_sender(sender),
            "pushName": None if _is_me_sender(sender) else sender,
            "timestamp": ts,
            "text": None if media_kind else text_field,
            "mediaKind": media_kind,
            "mimetype": None,
            "filename": None,
        }
        senders[sender] = senders.get(sender, 0) + 1
    if current is not None:
        _finalize_export_msg(current, messages)
    # Pick the most common non-Me sender as the contact name.
    contact = None
    if senders:
        non_me = [(s, n) for s, n in senders.items() if not _is_me_sender(s)]
        if non_me:
            contact = max(non_me, key=lambda x: x[1])[0]
    return messages, contact


def _finalize_export_msg(msg: dict[str, Any], out: list[dict[str, Any]]) -> None:
    if msg.get("text"):
        msg["text"] = msg["text"].strip()
        if not msg["text"]:
            msg["text"] = None
    if msg.get("text") or msg.get("mediaKind"):
        out.append(msg)


_ME_NAMES = {"me", "you", "ich"}  # phone locale: en, de


def _is_me_sender(sender: str) -> bool:
    return sender.strip().lower() in _ME_NAMES


def _looks_like_real_sender(sender: str) -> bool:
    # Real senders are typically short names without colons or sentences.
    return len(sender) < 60 and "." not in sender and sender.count(" ") < 4


_MEDIA_STUB_RE = re.compile(
    r"^(?:<(image|video|audio|sticker|document|gif|media) omitted>|"
    r"(image|video|audio|sticker|document|gif|media) omitted)$",
    re.IGNORECASE,
)


def _media_kind_from_stub(text: str) -> Optional[str]:
    m = _MEDIA_STUB_RE.match(text.strip())
    if not m:
        return None
    kind = (m.group(1) or m.group(2) or "media").lower()
    return {"gif": "video", "media": "document"}.get(kind, kind)


def _parse_export_ts(date: str, time: str) -> int:
    """WhatsApp uses the phone's locale, which varies wildly. Try a few
    formats and fall back to 'now' if none match — better than crashing
    the import on one bad line."""
    candidates = [
        f"%m/%d/%y %I:%M:%S %p", f"%m/%d/%Y %I:%M:%S %p",
        f"%d/%m/%y %I:%M:%S %p", f"%d/%m/%Y %I:%M:%S %p",
        f"%m/%d/%y %I:%M %p",    f"%m/%d/%Y %I:%M %p",
        f"%d/%m/%y %I:%M %p",    f"%d/%m/%Y %I:%M %p",
        f"%m/%d/%y %H:%M:%S",    f"%m/%d/%Y %H:%M:%S",
        f"%d/%m/%y %H:%M:%S",    f"%d/%m/%Y %H:%M:%S",
        f"%m/%d/%y %H:%M",       f"%m/%d/%Y %H:%M",
        f"%d/%m/%y %H:%M",       f"%d/%m/%Y %H:%M",
        f"%d.%m.%y %H:%M:%S",    f"%d.%m.%Y %H:%M:%S",
        f"%d.%m.%y %H:%M",       f"%d.%m.%Y %H:%M",
    ]
    combined = f"{date} {time}".strip()
    for fmt in candidates:
        try:
            return int(datetime.strptime(combined, fmt).timestamp())
        except ValueError:
            continue
    return int(datetime.now().timestamp())


def _stable_msg_id(sender: str, ts: int, text: str) -> str:
    """Imported messages don't have a Baileys key.id. Hash the
    sender+ts+text so re-importing the same export hits the
    (chat_jid, msg_id) unique constraint and skips duplicates."""
    h = hashlib.sha1(f"{sender}|{ts}|{text}".encode("utf-8")).hexdigest()
    return f"IMP{h[:30].upper()}"


@router.get("/semantic-status")
async def semantic_status() -> dict[str, Any]:
    """How many messages are indexed for semantic search, is the
    embedder reachable, etc. Drives the "Backfill" button state."""
    from . import whatsapp_semantic as _sem
    return _sem.index_stats()


@router.post("/backfill-embeddings")
async def backfill_embeddings(limit: Optional[int] = None) -> dict[str, Any]:
    """Embed every wa_message that isn't yet in the semantic index.
    Idempotent — safe to re-run. Limit param caps the batch size for
    a first probe (defaults to "do everything")."""
    from . import whatsapp_semantic as _sem
    # Run in thread — embed() is sync and can take ~50-200 ms × thousands.
    return await asyncio.to_thread(_sem.backfill, limit)


@router.post("/messages/{msg_id}/reprocess")
async def reprocess_message(msg_id: str, chat_jid: str = Query(...)) -> dict[str, Any]:
    """Re-trigger media processing for one message. Useful after
    configuring Paperless/Immich, or if a service was down at first
    ingest. chat_jid is a query param (not path) because msg_id alone
    isn't globally unique."""
    from . import whatsapp_media
    return await whatsapp_media.reprocess_message(chat_jid, msg_id)


@router.get("/avatar/{jid:path}")
async def avatar(jid: str) -> Response:
    """Proxy the bridge's cached profile picture. Returns 404 with a long
    cache-control on miss/error so the browser stops re-requesting on
    every navigation.

    Two classes of JID never have user profile pictures and we short-circuit
    them before touching the bridge:
      - status@broadcast       (WhatsApp's status broadcast pseudo-chat)
      - *@newsletter, *@lid    (channel / linked-device pseudo-JIDs;
                                bridge crashes on these — see logs)
    Returning 404 immediately keeps the frontend fallback (initials) and
    avoids hammering the bridge with N requests per chat-list render.
    """
    # Cache misses for an hour. Without this the browser memory cache is
    # the only thing standing between us and N requests per page nav —
    # not enough when the user bounces between Tasks / Calendar / WhatsApp.
    miss_headers = {"cache-control": "public, max-age=3600"}

    if (jid == "status@broadcast"
            or jid.endswith("@newsletter")
            or jid.endswith("@lid")):
        return Response(status_code=404, headers=miss_headers)

    try:
        # Profile-pic fetch uses the legacy (un-prefixed) bridge URL —
        # the bridge's compat shim routes it to the legacy admin
        # session's avatar cache. Acceptable: profile pics are the same
        # for any session looking at the same JID, and the shared cache
        # avoids each user re-downloading.
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{BRIDGE_URL}/profile-picture/{jid}")
    except Exception:
        # Bridge unreachable or timeout — degrade gracefully to "no picture"
        # rather than 500. Frontend already shows initials on miss.
        return Response(status_code=404, headers=miss_headers)

    if r.status_code != 200:
        return Response(status_code=404, headers=miss_headers)

    return Response(
        content=r.content,
        media_type="image/jpeg",
        headers={"cache-control": "public, max-age=3600"},
    )


@router.post("/clear")
async def clear_all(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Wipe THIS user's ingested chats/messages/drafts. Useful after
    the initial pair imports junk (protocol handshake messages, empty
    chats) — clear + re-sync gives a clean state. Other users' WA data
    is untouched."""
    uid = user["id"]
    with get_conn() as conn:
        n_msgs   = conn.execute("DELETE FROM wa_messages WHERE owner_user_id=?", (uid,)).rowcount
        n_chats  = conn.execute("DELETE FROM wa_chats    WHERE owner_user_id=?", (uid,)).rowcount
        n_drafts = conn.execute("DELETE FROM wa_drafts   WHERE owner_user_id=?", (uid,)).rowcount
        conn.commit()
    return {"chats_cleared": n_chats, "messages_cleared": n_msgs, "drafts_cleared": n_drafts}


@router.post("/chats/{jid:path}/fetch-history")
async def fetch_more_history(
    jid: str,
    count: int = 50,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Pass-through to the user's bridge session: ask the phone for older
    messages in this chat. Result lands asynchronously via
    messaging-history.set events."""
    uid = user["id"]
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(_bridge_url(f"/chats/{jid}/fetch-history", uid), json={"count": count})
            if r.status_code != 200:
                raise HTTPException(r.status_code, r.text)
            return r.json()
    except httpx.RequestError as e:
        raise HTTPException(502, f"WhatsApp bridge unreachable — start yorik-whatsapp-bridge ({e})")


@router.post("/sync")
async def sync_from_bridge(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Force-pull whatever this user's bridge session has in its
    in-memory cache and ingest into SQLite. Useful right after pairing
    if the WS subscriber missed the historical sync burst."""
    uid = user["id"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            chats_r = await c.get(_bridge_url("/chats", uid))
            chats_r.raise_for_status()
            chats = chats_r.json()
    except Exception as e:
        raise HTTPException(502, f"bridge unreachable: {e}")
    msg_count = 0
    for ch in chats:
        jid = ch.get("jid")
        if not jid:
            continue
        _upsert_chat(
            jid=jid,
            name=ch.get("name"),
            is_group=bool(ch.get("isGroup")),
            ts=ch.get("lastMessageTs"),
            last_text=None,
            owner_user_id=uid,
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                mr = await c.get(_bridge_url(f"/chats/{jid}/messages?limit=200", uid))
                if mr.status_code != 200:
                    continue
                for m in mr.json():
                    _insert_message(m, owner_user_id=uid)
                    msg_count += 1
        except Exception:
            continue
    return {"chats": len(chats), "messages_ingested": msg_count}


@router.get("/chats")
async def list_chats(
    limit: int = Query(50, ge=1, le=500),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> list[dict[str, Any]]:
    """Authoritative chat list from local SQLite (not the bridge), scoped
    to the logged-in user's WhatsApp session. Ordered by most-recent
    message."""
    uid = user["id"]
    with get_conn() as conn:
        self_name = _get_self_pushname(conn, uid)
        # Soft phantom filter at query time — belt-and-braces in case a
        # phantom slipped through ingest (e.g. bridge re-sync before
        # the pushname was captured on `ready`). The cleanup endpoint
        # physically removes them; this just hides them.
        if self_name:
            rows = conn.execute(
                "SELECT jid, name, is_group, last_message_ts, last_message_text, unread_count "
                "FROM wa_chats WHERE owner_user_id=? AND archived=0 "
                "AND NOT (jid LIKE '%@lid' AND LOWER(TRIM(name)) = LOWER(TRIM(?))) "
                "ORDER BY last_message_ts DESC NULLS LAST LIMIT ?",
                (uid, self_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT jid, name, is_group, last_message_ts, last_message_text, unread_count "
                "FROM wa_chats WHERE owner_user_id=? AND archived=0 "
                "ORDER BY last_message_ts DESC NULLS LAST LIMIT ?",
                (uid, limit),
            ).fetchall()
    return [dict(r) for r in rows]


@router.get("/chats/{jid:path}/messages")
async def list_messages(
    jid: str,
    limit: int = Query(50, ge=1, le=500),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> list[dict[str, Any]]:
    """Messages in one chat, oldest→newest, newest `limit` of them — scoped
    to the logged-in user."""
    uid = user["id"]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT msg_id, chat_jid, from_me, push_name, timestamp, text, "
            "       media_kind, mimetype, filename, transcript, "
            "       media_paperless_id, media_immich_id "
            "FROM wa_messages WHERE chat_jid=? AND owner_user_id=? "
            "ORDER BY timestamp DESC LIMIT ?",
            (jid, uid, limit),
        ).fetchall()
    # Oldest first for rendering.
    return [dict(r) for r in reversed(rows)]


class DraftOptionsBody(BaseModel):
    state: str = "friendly"  # friendly | formal | quick | warm | firm
    custom: Optional[str] = None


@router.post("/chats/{jid:path}/draft-options")
async def chat_draft_options(
    jid: str,
    body: DraftOptionsBody,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Generate 3 draft replies in the user-picked tone (state). All 3
    drafts share the tone; they differ in angle. Custom one-liner from
    the user is mixed in as content guidance (does not override tone).

    Ephemeral — drafts are NOT persisted to wa_drafts (no chat-row
    badges, no DB cruft). Each click of a state button = one LLM call
    returning 3 options. User picks one → composer fills → user sends
    (or edits then sends).
    """
    state = (body.state or "friendly").strip().lower()
    from . import whatsapp_autodraft as _ad
    if state not in _ad.STATE_SPECS:
        raise HTTPException(400, f"unknown state: {state}")
    from .skills import get_registry, SkillContext, SkillError
    reg = get_registry()
    if not reg.get("whatsapp_draft"):
        raise HTTPException(503, "whatsapp_draft skill not loaded yet — try again in a moment")
    try:
        result = await reg.invoke(
            "whatsapp_draft",
            ctx=SkillContext(reg, role=user.get("role", "admin"), user_id=user["id"]),
            chat_jid=jid,
            extra_instructions=body.custom,
            variants=3,
            state=state,
            persist=False,
        )
    except SkillError as e:
        raise HTTPException(400, str(e))
    return {
        "state":  state,
        "drafts": [d.get("text", "") for d in (result.get("drafts") or [])],
    }


@router.get("/draft-states")
def list_draft_states(
    user: dict[str, Any] = Depends(_auth.current_user),  # noqa: ARG001
) -> list[dict[str, str]]:
    """Catalogue of available draft states + bilingual labels. The UI
    fetches this once on mount and renders one button per state, so
    adding/removing a state is a backend-only change."""
    from . import whatsapp_autodraft as _ad
    return [
        {"key": key, **spec}
        for key, spec in _ad.STATE_SPECS.items()
    ]


@router.post("/chats/{jid:path}/send")
async def send_message(
    jid: str,
    body: SendBody,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Forward send to the bridge for THIS user's WhatsApp session. The
    bridge adds humanlike typing delay."""
    if not body.text.strip():
        raise HTTPException(400, "empty text")
    uid = user["id"]
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(_bridge_url(f"/chats/{jid}/send", uid), json={"text": body.text})
            if r.status_code != 200:
                raise HTTPException(r.status_code, r.text)
            out = r.json()
    except httpx.RequestError as e:
        raise HTTPException(502, f"WhatsApp bridge unreachable — start yorik-whatsapp-bridge ({e})")
    # Optimistically insert as a fromMe message so the UI updates immediately;
    # the bridge will also push it via WS, but the (chat_jid, msg_id, owner_user_id)
    # PK protects against duplicate inserts.
    _insert_message({
        "id":        out.get("msgId"),
        "jid":       jid,
        "fromMe":    True,
        "pushName":  None,
        "timestamp": out.get("ts"),
        "text":      body.text,
    }, owner_user_id=uid)

    # Auto-promote: replying to someone is the strongest "this is a real
    # person" signal. If the recipient was still pending (shouldn't be,
    # post-migration 015 + active-by-default autocapture, but defensive),
    # flip them to active so they surface in autocomplete and Pending
    # count drops. Mirror of the email-side promote-on-reply hook.
    if not jid.endswith("@g.us"):
        try:
            from . import contacts as _contacts_mod
            from . import contact_autocapture
            existing = _contacts_mod.find_by_channel("whatsapp", jid)
            if existing and existing.get("status") == "pending":
                _contacts_mod.promote_pending(int(existing["id"]))
                log.info("WA outbound: auto-promoted contact %s to active", existing["id"])
            elif not existing:
                # Brand-new recipient — autocapture them on the outbound
                # side too. Uses the same active-by-default policy.
                contact_autocapture.on_inbound_whatsapp(from_jid=jid, from_name="")
        except Exception as exc:
            log.debug("WA outbound promote hook failed: %s", exc)
    # Mark the source draft (and its siblings) as resolved. The chosen
    # one becomes status='used'; sibling variants in the same group
    # become status='discarded' (reason: user picked another option).
    if body.draft_id is not None:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT variant_group_id FROM wa_drafts WHERE id=? AND owner_user_id=?",
                (body.draft_id, uid),
            ).fetchone()
            if row:
                gid = row["variant_group_id"]
                conn.execute(
                    "UPDATE wa_drafts SET status='used', sent_msg_id=?, sent_text=? WHERE id=?",
                    (out.get("msgId"), body.text, body.draft_id),
                )
                if gid:
                    conn.execute(
                        "UPDATE wa_drafts SET status='discarded', discarded_at=datetime('now'), "
                        "discard_reason='sibling_used' WHERE variant_group_id=? AND id!=? AND status='pending'",
                        (gid, body.draft_id),
                    )
                conn.commit()
    return out


# ─────────────────────────── draft generation ──────────────────────────

@router.post("/draft")
async def generate_draft(body: DraftBody, request: Request) -> dict[str, Any]:
    """Compose a draft reply for `chat_jid`. Thin HTTP adapter — the
    actual generation lives in the `whatsapp_draft` skill so the same
    code path is used by the WS auto-draft trigger, the agent's
    use_skill tool, and any composite skill.

    Carries the calling user through SkillContext so per-user external
    creds (wave 3) are picked up downstream.
    """
    from .skills import get_registry, SkillContext, SkillError
    from . import auth_sessions as _auth
    user = _auth.current_user_optional(request, request.cookies.get(_auth.COOKIE_NAME))
    role = (user or {}).get("role", "admin")
    user_id = (user or {}).get("id", 1)
    reg = get_registry()
    if reg.get("whatsapp_draft"):
        try:
            result = await reg.invoke(
                "whatsapp_draft",
                ctx=SkillContext(reg, role=role, user_id=user_id),
                chat_jid=body.chat_jid,
                extra_instructions=body.extra_instructions,
                variants=1,
            )
            drafts = result.get("drafts") or []
            return {
                "draft":   drafts[0]["text"] if drafts else "",
                "sources": result.get("sources", []),
            }
        except SkillError as e:
            raise HTTPException(400, str(e))

    # Skill registry not yet loaded (e.g. mid-startup) — fall back to
    # the inline implementation below so the endpoint never breaks.
    chat_jid = body.chat_jid
    with get_conn() as conn:
        chat_row = conn.execute(
            "SELECT jid, name, is_group FROM wa_chats WHERE jid=? AND owner_user_id=?",
            (chat_jid, user_id),
        ).fetchone()
        if not chat_row:
            raise HTTPException(404, "chat not found")
        recent = conn.execute(
            "SELECT from_me, push_name, timestamp, text, transcript "
            "FROM wa_messages WHERE chat_jid=? AND owner_user_id=? "
            "ORDER BY timestamp DESC LIMIT 20",
            (chat_jid, user_id),
        ).fetchall()
    recent = list(reversed(recent))  # oldest → newest
    if not recent:
        raise HTTPException(400, "no messages in this chat yet")

    # Cross-chat hints: fuse FTS5 (precise word matches) + semantic
    # search (meaning matches) over the last inbound message. Dedup by
    # msg_id, cap total. Plus Paperless docs that semantically match —
    # so if the contact asks about something we have a contract for,
    # the draft sees that contract.
    last_inbound = next((r["text"] for r in reversed(recent) if not r["from_me"] and r["text"]), recent[-1]["text"] or "")
    fts_hits      = _cross_chat_hints(chat_jid, last_inbound, owner_user_id=user_id)
    semantic_hits = _semantic_hints(chat_jid, last_inbound)
    paperless_hits = _paperless_hints(last_inbound)
    cross_hits = _merge_hints(fts_hits, semantic_hits, paperless_hits, cap=6)

    sources = [{"kind": "thread", "ref": chat_jid, "snippet": f"{len(recent)} recent messages"}]
    sources.extend(cross_hits)

    prompt = _build_draft_prompt(
        contact_name=chat_row["name"] or chat_jid.split("@")[0],
        is_group=bool(chat_row["is_group"]),
        recent=recent,
        cross_hits=cross_hits,
        calendar=_calendar_context(),
        extra=body.extra_instructions,
    )

    draft_text = await _call_llm(prompt)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO wa_drafts (chat_jid, draft_text, sources_json, owner_user_id) "
            "VALUES (?, ?, ?, ?)",
            (chat_jid, draft_text, json.dumps(sources), user_id),
        )
        conn.commit()

    return {"draft": draft_text, "sources": sources}


def _cross_chat_hints(current_jid: str, query_text: str, owner_user_id: str = DEFAULT_OWNER, k: int = 3) -> list[dict[str, Any]]:
    """FTS5 search over wa_messages, excluding the current chat, scoped
    to a single user. Returns up to `k` hits formatted as draft-prompt
    sources.

    owner_user_id defaults to DEFAULT_OWNER (admin) so legacy callers
    (whatsapp_autodraft, email_draft skill, whatsapp_draft skill) keep
    working unchanged for the admin's data. Multi-user code paths
    (REST routes) should pass the logged-in user's id explicitly.
    Follow-up: thread owner_user_id through whatsapp_autodraft + the
    draft skills so they support non-admin users."""
    q = (query_text or "").strip()
    if len(q) < 4:
        return []
    # Pull top 2-3 longest words as MATCH terms — quick noise filter.
    terms = sorted({w for w in q.split() if len(w) > 4}, key=len, reverse=True)[:3]
    if not terms:
        return []
    match = " OR ".join(terms)
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT m.chat_jid, m.timestamp, m.text, c.name "
                "FROM wa_messages_fts f "
                "JOIN wa_messages m ON m.rowid = f.rowid "
                "LEFT JOIN wa_chats c ON c.jid = m.chat_jid "
                "WHERE f.text MATCH ? AND m.chat_jid != ? AND m.owner_user_id = ? "
                "ORDER BY m.timestamp DESC LIMIT ?",
                (match, current_jid, owner_user_id, k),
            ).fetchall()
        except Exception as e:
            log.debug("FTS query failed (%s); skipping cross-chat hints", e)
            return []
    out = []
    for r in rows:
        out.append({
            "kind": "other_chat",
            "ref":  r["chat_jid"],
            "snippet": f"From chat with {r['name'] or r['chat_jid']}: {(r['text'] or '')[:140]}",
        })
    return out


def _semantic_hints(current_jid: str, query_text: str, k: int = 3) -> list[dict[str, Any]]:
    """Vector-search wa_vec for top-K semantic matches outside the current
    chat. Falls back to empty list if the embedder is down."""
    if not query_text or len(query_text.strip()) < 4:
        return []
    try:
        from . import whatsapp_semantic as _sem
        hits = _sem.search(query_text, k=k, exclude_chat_jid=current_jid)
    except Exception as e:
        log.debug("semantic search failed: %s", e)
        return []
    return [
        {
            "kind": "other_chat_sem",
            "ref": h["chat_jid"],
            "msg_id": h["msg_id"],
            "snippet": f"From chat with {h['chat_name']}: {(h['text'] or '')[:140]}",
        }
        for h in hits
    ]


def _paperless_hints(query_text: str, k: int = 3) -> list[dict[str, Any]]:
    """Semantic-search Paperless docs (vector index already maintained
    by the existing paperless_ingest pipeline). Surfaces relevant
    documents the user has filed so the draft can reference them."""
    if not query_text or len(query_text.strip()) < 4:
        return []
    try:
        from . import paperless_ingest as _pi
        hits = _pi.search(query_text, k=k)
    except Exception as e:
        log.debug("paperless semantic search failed: %s", e)
        return []
    out = []
    for h in hits[:k]:
        title = h.get("doc_title") or h.get("title") or "(untitled)"
        snippet = (h.get("text") or h.get("snippet") or "")[:160]
        out.append({
            "kind": "paperless_doc",
            "ref": str(h.get("paperless_doc_id") or h.get("doc_id") or ""),
            "snippet": f"From document '{title}': {snippet}",
        })
    return out


def _calendar_context(days_ahead: int = 7) -> list[dict[str, Any]]:
    """Pull upcoming events from family.db as draft context. Always
    included in the prompt — the LLM picks it up when scheduling
    questions arrive ('can we meet Thursday?', 'are you around next
    week?') and ignores otherwise. Cheap, no extra LLM call."""
    try:
        now = datetime.now()
        end = now + timedelta(days=days_ahead)
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT title, starts_at, ends_at, all_day, person "
                "FROM events "
                "WHERE starts_at >= ? AND starts_at <= ? "
                "ORDER BY starts_at ASC LIMIT 25",
                (now.isoformat(), end.isoformat()),
            ).fetchall()
        if not rows:
            return []
        events = []
        for r in rows:
            starts = r["starts_at"] or ""
            date = starts[:10]
            if r["all_day"]:
                time = "all day"
            elif len(starts) >= 16:
                time = starts[11:16]
                if r["ends_at"] and len(r["ends_at"]) >= 16:
                    time += f"-{r['ends_at'][11:16]}"
            else:
                time = ""
            who = f" ({r['person']})" if r["person"] and r["person"] != "all" else ""
            events.append({"date": date, "time": time, "title": r["title"], "who": who})
        return events
    except Exception as e:
        log.debug("calendar context fetch failed: %s", e)
        return []


def _merge_hints(*hint_lists, cap: int = 6) -> list[dict[str, Any]]:
    """Interleave hint lists round-robin so a single source can't
    monopolise the prompt budget; dedup by (kind, ref, msg_id) tuple."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    iters = [iter(lst) for lst in hint_lists]
    while iters and len(out) < cap:
        next_iters = []
        for it in iters:
            try:
                h = next(it)
                key = (h.get("kind", ""), h.get("ref", ""), h.get("msg_id", ""))
                if key in seen:
                    next_iters.append(it)
                    continue
                seen.add(key)
                out.append(h)
                if len(out) >= cap:
                    break
                next_iters.append(it)
            except StopIteration:
                continue
        iters = next_iters
    return out


def _build_draft_prompt(
    contact_name: str,
    is_group: bool,
    recent: list[Any],
    cross_hits: list[dict[str, Any]],
    calendar: Optional[list[dict[str, Any]]] = None,
    extra: Optional[str] = None,
) -> str:
    lines = [
        "You are helping the user draft a reply on WhatsApp.",
        f"You are replying to {contact_name}{' (group chat)' if is_group else ''}.",
        "",
        "Rules:",
        "- Match the language of the last incoming message.",
        "- Match the conversation's tone (formal vs. casual). When in doubt, match how the user replied recently.",
        "- Be concise. WhatsApp is a quick medium — keep it short unless the question demands detail.",
        "- Do NOT add greetings ('Hi X!') if the conversation is already mid-thread.",
        "- If the user mentions a fact or commitment, only state it if it appears in the messages or sources below.",
        "- When the message asks about your availability ('can we meet', 'are you free', a specific day), check the calendar section and answer concretely — propose specific free slots or say what's blocking.",
        "- Return ONLY the message text. No prefix, no quotes, no explanation.",
        "",
        "── Conversation so far ──",
    ]
    for r in recent:
        who = "Me" if r["from_me"] else (r["push_name"] or contact_name)
        text = r["text"] or r["transcript"] or "[media message]"
        lines.append(f"{who}: {text}")
    if calendar:
        lines.append("")
        lines.append(f"── Your upcoming calendar (today is {datetime.now().strftime('%A %Y-%m-%d')}) ──")
        for e in calendar:
            lines.append(f"  {e['date']} {e['time']}: {e['title']}{e['who']}")
        lines.append("(Use these ONLY if the conversation is about scheduling/availability.)")
    if cross_hits:
        lines.append("")
        lines.append("── Possibly relevant context from other chats ──")
        for h in cross_hits:
            lines.append(f"- {h['snippet']}")
    if extra:
        lines.append("")
        lines.append(f"── Extra instructions from user ──\n{extra}")
    lines.append("")
    lines.append("Draft reply:")
    return "\n".join(lines)


async def _call_llm(prompt: str) -> str:
    """One-shot completion through the same llama-swap endpoint Vanna uses.
    Direct OpenAI call (no tool loop) — drafts don't need tools."""
    base = os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    model = os.getenv("HOMEOS_MODEL", "qwen3.6-27b-mtp")
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            f"{base}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "max_tokens": 400,
                # Same Qwen3 thinking-mode kill as everywhere else in Yorik.
                "chat_template_kwargs": {"enable_thinking": False},
                "reasoning_effort": "none",
            },
            headers={"Authorization": "Bearer not-used"},
        )
        if r.status_code != 200:
            raise HTTPException(502, f"LLM error: {r.status_code} {r.text}")
        data = r.json()
    txt = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    # Strip accidental quote-wrapping from some models.
    if (txt.startswith('"') and txt.endswith('"')) or (txt.startswith("'") and txt.endswith("'")):
        txt = txt[1:-1].strip()
    return txt


@router.websocket("/ws")
async def browser_ws(ws: WebSocket) -> None:
    """Browser-facing WebSocket, scoped to the logged-in user. Push
    channel for live updates so the frontend doesn't have to poll. The
    user is identified via the standard session cookie sent on the WS
    upgrade request; if no valid session, the socket is rejected so we
    don't leak events to anonymous clients.

    Each user's open tabs join the per-user fan-out set so the wife
    doesn't see admin's incoming messages and vice-versa.
    """
    # WebSockets don't pass through the Depends() middleware, so we
    # read auth manually from the upgrade request's cookies.
    from . import auth_sessions as _sessions
    cookie = ws.cookies.get(_sessions.COOKIE_NAME)
    ip = ws.client.host if ws.client else None
    user = _sessions.get_user_for_session(cookie, ip=ip) if cookie else None
    if not user:
        await ws.close(code=4401)  # custom code: "no session"
        return
    uid = user["id"]
    await ws.accept()
    _browser_ws.setdefault(uid, set()).add(ws)
    # Send initial state for THIS user's session so the client knows
    # what's up immediately without a separate /status fetch.
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(_bridge_url("/status", uid))
            initial = r.json() if r.status_code == 200 else {"connected": False}
    except Exception:
        initial = {"connected": False, "bridge_unreachable": True}
    try:
        await ws.send_text(json.dumps({"type": "hello", "payload": initial}))
        # Keep the socket alive — client messages are ignored (this is
        # a push channel) but reading drains pings/closes cleanly.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("browser WS errored: %s", e)
    finally:
        conns = _browser_ws.get(uid)
        if conns is not None:
            conns.discard(ws)
            if not conns:
                _browser_ws.pop(uid, None)


@router.get("/briefing")
async def briefing(
    hours: int = Query(24, ge=1, le=168),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Natural-language summary of THIS user's recent WhatsApp activity.
    Aggregates their incoming messages, pending drafts, and auto-filed
    media from the last `hours` window and asks the LLM for a
    prioritised briefing.

    Returns {summary, stats, chats_needing_reply, generated_at}.
    Cheap to call repeatedly — no caching, generation takes ~3-8s on
    qwen3 depending on inbox size."""
    uid = user["id"]
    since_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())

    with get_conn() as conn:
        # Chats with new inbound messages.
        chat_rows = conn.execute("""
            SELECT c.jid, c.name, COUNT(*) AS msg_count, MAX(m.timestamp) AS last_ts
            FROM wa_messages m
            JOIN wa_chats c ON c.jid = m.chat_jid
            WHERE m.from_me = 0 AND m.timestamp >= ? AND m.owner_user_id = ?
            GROUP BY c.jid
            ORDER BY last_ts DESC
            LIMIT 12
        """, (since_ts, uid)).fetchall()

        # Per-chat recent messages (the last 4 each) for the LLM to summarise.
        chat_blocks = []
        for ch in chat_rows:
            msgs = conn.execute("""
                SELECT from_me, push_name, timestamp, text, transcript, media_kind
                FROM wa_messages WHERE chat_jid=? AND owner_user_id=?
                ORDER BY timestamp DESC LIMIT 4
            """, (ch["jid"], uid)).fetchall()
            chat_blocks.append({
                "name": ch["name"] or ch["jid"].split("@")[0],
                "jid":  ch["jid"],
                "msg_count": ch["msg_count"],
                "recent": list(reversed([dict(m) for m in msgs])),
            })

        # Stats.
        pending_chats = conn.execute("""
            SELECT COUNT(DISTINCT chat_jid) FROM wa_drafts
            WHERE status='pending' AND owner_user_id=?
        """, (uid,)).fetchone()[0]

        media_filed = conn.execute("""
            SELECT COUNT(*) FROM wa_messages
            WHERE timestamp >= ? AND owner_user_id=?
              AND (media_paperless_id IS NOT NULL OR media_immich_id IS NOT NULL
                   OR transcript IS NOT NULL)
        """, (since_ts, uid)).fetchone()[0]

    stats = {
        "hours":                  hours,
        "chats_with_new_msgs":    len(chat_rows),
        "chats_with_pending_drafts": pending_chats,
        "media_auto_filed":       media_filed,
    }
    chats_needing_reply = [
        {"jid": b["jid"], "name": b["name"], "msg_count": b["msg_count"]}
        for b in chat_blocks
    ]

    if not chat_blocks:
        return {
            "summary": f"Nothing new in the last {hours}h. Inbox is clear.",
            "stats": stats,
            "chats_needing_reply": [],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    prompt = _build_briefing_prompt(chat_blocks, stats, hours)
    summary = await _call_llm(prompt)

    return {
        "summary": summary,
        "stats": stats,
        "chats_needing_reply": chats_needing_reply,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _build_briefing_prompt(chat_blocks: list[dict], stats: dict, hours: int) -> str:
    lines = [
        f"You are summarising the user's WhatsApp inbox from the last {hours} hours.",
        "",
        "Write a tight, scannable briefing in this exact structure:",
        "",
        "**Action items** (the 3-5 most important things to reply to or act on, in priority order)",
        "- Each item: one line, contact name in bold, then what's needed in plain language.",
        "",
        "**By conversation** (bullet per chat with a one-sentence what-happened)",
        "",
        "Tone: efficient, friendly, no fluff. Write the briefing in the same language as the "
        "majority of the messages — match what the user reads in WhatsApp.",
        "",
        "Don't list every message — synthesise. If someone sent three things, summarise the request.",
        "",
        "── Inbox ──",
    ]
    for b in chat_blocks:
        lines.append("")
        lines.append(f"## {b['name']} ({b['msg_count']} new)")
        for m in b["recent"]:
            who = "Me" if m["from_me"] else (m["push_name"] or b["name"])
            content = m["text"] or m["transcript"] or (
                f"[{m['media_kind']}]" if m["media_kind"] else "[empty]"
            )
            lines.append(f"  {who}: {content}")
    lines.append("")
    lines.append(f"Context — pending drafts in {stats['chats_with_pending_drafts']} chats, "
                 f"{stats['media_auto_filed']} attachments auto-filed.")
    lines.append("")
    lines.append("Briefing:")
    return "\n".join(lines)


@router.get("/drafts/{chat_jid:path}/pending")
async def pending_drafts(
    chat_jid: str,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """The current set of pending auto-drafted variants for this user's
    chat (always one set max — older sets are auto-discarded on
    regenerate). Returns {group_id, variants: [{id, label, text,
    sources}]} or {group_id: null, variants: []} when there are no
    pending drafts."""
    uid = user["id"]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, draft_text, variant_label, variant_group_id, sources_json "
            "FROM wa_drafts WHERE chat_jid=? AND owner_user_id=? AND status='pending' "
            "ORDER BY id ASC",
            (chat_jid, uid),
        ).fetchall()
    if not rows:
        return {"group_id": None, "variants": []}
    variants = []
    sources: list[Any] = []
    for r in rows:
        try:
            srcs = json.loads(r["sources_json"] or "[]")
        except json.JSONDecodeError:
            srcs = []
        if not sources and srcs:
            sources = srcs  # all variants share the same sources blob
        variants.append({
            "id":    r["id"],
            "label": r["variant_label"] or "draft",
            "text":  r["draft_text"],
        })
    return {"group_id": rows[0]["variant_group_id"], "variants": variants, "sources": sources}


@router.get("/drafts/pending-counts")
async def pending_draft_counts(
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, int]:
    """Per-chat count of THIS user's pending drafts — drives the sparkle
    badge on the chat-list rows. Single query so it's cheap to call on
    every chat-list render."""
    uid = user["id"]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT chat_jid, COUNT(*) AS n FROM wa_drafts "
            "WHERE owner_user_id=? AND status='pending' GROUP BY chat_jid",
            (uid,),
        ).fetchall()
    return {r["chat_jid"]: r["n"] for r in rows}


@router.post("/drafts/{chat_jid:path}/discard")
async def discard_pending_drafts(chat_jid: str) -> dict[str, Any]:
    """User explicitly dismisses the pending draft set."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE wa_drafts SET status='discarded', discarded_at=datetime('now'), "
            "discard_reason='user_dismissed' WHERE chat_jid=? AND status='pending'",
            (chat_jid,),
        )
        conn.commit()
    await _broadcast_to_browsers({
        "type": "drafts_updated",
        "payload": {"chat_jid": chat_jid, "discarded": cur.rowcount or 0,
                    "reason": "user_dismissed"},
    })
    return {"discarded": cur.rowcount or 0}


@router.post("/drafts/{chat_jid:path}/regenerate")
async def regenerate_drafts(
    chat_jid: str,
    user: dict[str, Any] = Depends(_auth.current_user),
) -> dict[str, Any]:
    """Force a new draft set NOW (skip the debounce) for this user."""
    from . import whatsapp_autodraft as _ad
    uid = user["id"]
    # Find the most recent inbound message to use as trigger.
    with get_conn() as conn:
        row = conn.execute(
            "SELECT msg_id FROM wa_messages WHERE chat_jid=? AND owner_user_id=? AND from_me=0 "
            "ORDER BY timestamp DESC LIMIT 1",
            (chat_jid, uid),
        ).fetchone()
    if not row:
        raise HTTPException(400, "no inbound message to draft from")
    await _ad._generate_and_store(chat_jid, row["msg_id"], owner_user_id=uid)
    return {"ok": True}


@router.get("/drafts/{chat_jid:path}")
async def recent_drafts(
    chat_jid: str,
    limit: int = Query(5, ge=1, le=50),
    user: dict[str, Any] = Depends(_auth.current_user),
) -> list[dict[str, Any]]:
    uid = user["id"]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, draft_text, sources_json, sent_msg_id, sent_text, "
            "       status, variant_label, variant_group_id, created_at "
            "FROM wa_drafts WHERE chat_jid=? AND owner_user_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (chat_jid, uid, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["sources"] = json.loads(d.pop("sources_json") or "[]")
        except json.JSONDecodeError:
            d["sources"] = []
        out.append(d)
    return out
