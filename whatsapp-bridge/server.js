// Yorik WhatsApp bridge — Baileys-based, multi-session.
//
// Holds ONE WhatsApp session per Yorik user (family member). Each
// session is fully isolated:
//   - own auth state dir under /data/sessions/<userId>/baileys-auth
//   - own avatar cache under /data/sessions/<userId>/avatars
//   - own pino child logger with {user: userId}
//   - own in-memory msgCache (bounded 1000 per session)
//
// HTTP API — every route is scoped by the path param `:userId`:
//   POST /users/:userId/start                     -> idempotent, opens the socket
//   GET  /users/:userId/status                    -> {connected, me, hasQr}
//   GET  /users/:userId/qr                        -> {qrPng}
//   GET  /users/:userId/chats                     -> [{jid,name,...}]
//   GET  /users/:userId/chats/:jid/messages       -> [...]
//   POST /users/:userId/chats/:jid/send           -> {text} -> {msgId}
//   POST /users/:userId/chats/:jid/typing         -> {composing: bool}
//   POST /users/:userId/chats/:jid/fetch-history  -> {count} -> {ok}
//   GET  /users/:userId/profile-picture/:jid      -> image/jpeg
//   GET  /users/:userId/media/:msgId              -> binary stream
//   POST /users/:userId/logout                    -> closes the session
//   GET  /users                                   -> [{userId, connected, me}]
//
// Backward-compat shim: every legacy route (e.g. /status, /chats,
// /chats/:jid/send) is silently rewritten as if it targeted the
// LEGACY_ADMIN_USER_ID — defaults to "1". This lets the Python adapter
// keep working until it's migrated to user-aware URLs.
//
// WS /events streams {type, userId, payload, ts}. Subscribers see
// everyone's events; backend dispatches to the right SQL owner_user_id
// based on the userId field.
//
// On startup, the bridge scans /data/sessions/* and auto-starts every
// session that has an auth-state dir — so all family members reconnect
// after a bridge restart without manual /start calls.
//
// One-time migration: if /data/baileys-auth exists (single-tenant
// legacy layout) and /data/sessions does NOT, the bridge moves it to
// /data/sessions/<LEGACY_ADMIN_USER_ID>/baileys-auth on first boot.
// The admin's existing pairing is preserved — no re-QR needed.
//
// Multi-instance safety per @whiskeysockets/baileys research:
//   - Each socket gets a logger.child({user: userId}) — no shared
//     logger streams across sessions (avoids issue #585 typing
//     + mixed log output).
//   - No makeInMemoryStore — we persist in SQLite on the Python side
//     (avoids issue #747 race conditions on the in-memory store).
//   - Signal-state CPU spikes (issues #2340/#2520) can stall the
//     event loop. Fine for household scale (≤6 sessions); if the
//     household grows beyond that, split into worker threads.

import express from "express";
import { WebSocketServer } from "ws";
import { createServer } from "http";
import QRCode from "qrcode";
import pino from "pino";
import {
  default as makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
} from "@whiskeysockets/baileys";
import { existsSync, mkdirSync, readdirSync, renameSync, statSync } from "fs";
import { join } from "path";

const PORT = process.env.BRIDGE_PORT ? Number(process.env.BRIDGE_PORT) : 3001;
const DATA_DIR = process.env.BRIDGE_DATA_DIR || "/data";
const SESSIONS_DIR = join(DATA_DIR, "sessions");
// Legacy single-tenant install moves its existing auth state into the
// sessions tree under this id on first boot. Defaults to the admin id
// in a fresh install (typically 1).
const LEGACY_ADMIN_USER_ID = String(process.env.LEGACY_ADMIN_USER_ID || "1");

const baseLogger = pino({ level: process.env.LOG_LEVEL || "warn" });

// ────────────────────────── session manager ────────────────────────────

// Per-user session state. The Map is the entire multi-tenancy mechanism.
// All routes look up `sessions.get(userId)`; if absent and an auth dir
// exists on disk, the session is lazy-started on first access.
//
// UserSession shape:
//   {
//     userId: string,
//     sock: BaileysSocket | null,
//     connected: bool,
//     me: {id, name} | null,
//     lastQr: string | null,
//     msgCache: Map<msgId, rawMessage>,  // bounded 1000
//     logger: pino child,
//     reconnecting: bool,                 // suppresses parallel reconnect loops
//   }
const sessions = new Map();

function sessionDir(userId) {
  return join(SESSIONS_DIR, String(userId));
}

function authDir(userId) {
  return join(sessionDir(userId), "baileys-auth");
}

function avatarDir(userId) {
  return join(sessionDir(userId), "avatars");
}

function ensureSessionDirs(userId) {
  const root = sessionDir(userId);
  if (!existsSync(root)) mkdirSync(root, { recursive: true });
  const auth = authDir(userId);
  if (!existsSync(auth)) mkdirSync(auth, { recursive: true });
  const av = avatarDir(userId);
  if (!existsSync(av)) mkdirSync(av, { recursive: true });
}

function getOrCreateSession(userId) {
  const key = String(userId);
  let s = sessions.get(key);
  if (s) return s;
  s = {
    userId: key,
    sock: null,
    connected: false,
    me: null,
    lastQr: null,
    msgCache: new Map(),
    logger: baseLogger.child({ user: key }),
    reconnecting: false,
  };
  sessions.set(key, s);
  return s;
}

function cacheMessage(session, m) {
  if (!m?.key?.id) return;
  session.msgCache.set(m.key.id, m);
  if (session.msgCache.size > 1000) {
    const oldest = session.msgCache.keys().next().value;
    session.msgCache.delete(oldest);
  }
}

// ─────────────────────── WS fan-out (tagged) ───────────────────────────

const wsClients = new Set();

function broadcast(userId, type, payload) {
  if (!wsClients.size) return;
  const msg = JSON.stringify({ type, userId: String(userId), payload, ts: Date.now() });
  const dead = [];
  for (const c of wsClients) {
    if (c.readyState === 1) {
      try { c.send(msg); } catch { dead.push(c); }
    } else {
      dead.push(c);
    }
  }
  for (const c of dead) wsClients.delete(c);
}

// ──────────────────────────── Baileys ──────────────────────────────────

async function startSession(userId) {
  const session = getOrCreateSession(userId);
  if (session.sock && session.connected) {
    session.logger.info("startSession: already connected, no-op");
    return session;
  }
  ensureSessionDirs(userId);
  const dir = authDir(userId);
  const { state: authState, saveCreds } = await useMultiFileAuthState(dir);
  const { version } = await fetchLatestBaileysVersion();
  session.logger.info(`connecting with Baileys ${version.join(".")}`);

  const sock = makeWASocket({
    version,
    auth: authState,
    // Per-session child logger — avoids shared-stream / typing collisions
    // when multiple sessions log simultaneously.
    logger: session.logger,
    browser: [`Yorik-${userId}`, "Chrome", "1.0"],
    printQRInTerminal: false,
    syncFullHistory: true,
    markOnlineOnConnect: false,
    defaultQueryTimeoutMs: 120_000,
  });
  session.sock = sock;

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      session.lastQr = qr;
      session.logger.info("new QR available — fetch GET /users/:userId/qr");
      broadcast(userId, "qr", { available: true });
    }
    if (connection === "open") {
      session.connected = true;
      session.lastQr = null;
      session.me = {
        id: sock.user?.id || null,
        name: sock.user?.name || sock.user?.verifiedName || null,
      };
      session.logger.info(`connected as ${session.me.name} (${session.me.id})`);
      broadcast(userId, "ready", { me: session.me });
    } else if (connection === "close") {
      session.connected = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      session.logger.info(`connection closed (code=${code}, loggedOut=${loggedOut})`);
      broadcast(userId, "disconnected", { code, loggedOut });
      if (!loggedOut && !session.reconnecting) {
        // Per-session reconnect — issue #2052 reports auto-reconnect is
        // unreliable under multi-session load, so we implement it here.
        session.reconnecting = true;
        setTimeout(async () => {
          session.reconnecting = false;
          try { await startSession(userId); }
          catch (e) { session.logger.error({err: e}, "reconnect failed"); }
        }, 2000);
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    for (const m of messages) {
      cacheMessage(session, m);
      if (type === "notify" || type === "append") {
        const s = serializeMessage(m);
        if (s) broadcast(userId, "message", s);
      }
    }
  });

  sock.ev.on("messaging-history.set", (payload) => {
    const { chats = [], messages = [], isLatest = false } = payload;
    let kept = 0;
    for (const c of chats) broadcast(userId, "chat", serializeChat(c));
    for (const m of messages) {
      cacheMessage(session, m);
      const s = serializeMessage(m);
      if (s) { broadcast(userId, "message", s); kept++; }
    }
    session.logger.info(
      `history sync: ${chats.length} chats, ${kept}/${messages.length} messages forwarded (isLatest=${isLatest})`
    );
  });

  sock.ev.on("chats.upsert", (chats) => {
    for (const c of chats) broadcast(userId, "chat", serializeChat(c));
  });
  sock.ev.on("chats.update", (chats) => {
    for (const c of chats) broadcast(userId, "chat", serializeChat(c));
  });
  sock.ev.on("contacts.upsert", (contacts) => {
    for (const c of contacts) {
      if (!c.id || !c.name) continue;
      broadcast(userId, "chat", { jid: c.id, name: c.name, isGroup: false });
    }
  });

  return session;
}

// Detect existing sessions on disk and start them. Also handles the
// one-time migration of the legacy single-tenant /data/baileys-auth
// layout into /data/sessions/<LEGACY_ADMIN_USER_ID>/baileys-auth.
function discoverAndAutoStart() {
  // ── legacy migration: only run if /data/sessions doesn't exist yet
  //    AND the old /data/baileys-auth dir does. Idempotent on rerun.
  const legacyAuth = join(DATA_DIR, "baileys-auth");
  const legacyAvatars = join(DATA_DIR, "avatars");
  const haveLegacy = existsSync(legacyAuth) && statSync(legacyAuth).isDirectory();
  const haveSessions = existsSync(SESSIONS_DIR);
  if (haveLegacy && !haveSessions) {
    console.log(`[bridge] migrating legacy auth dir → sessions/${LEGACY_ADMIN_USER_ID}/`);
    ensureSessionDirs(LEGACY_ADMIN_USER_ID);
    const targetAuth = authDir(LEGACY_ADMIN_USER_ID);
    // ensureSessionDirs creates targetAuth empty; remove the empty one,
    // then rename the legacy in.
    try {
      // Move legacy contents into the new auth dir. Rename of the
      // directory itself fails because targetAuth already exists, so we
      // move file-by-file.
      const entries = readdirSync(legacyAuth);
      for (const name of entries) {
        renameSync(join(legacyAuth, name), join(targetAuth, name));
      }
      console.log(`[bridge] moved ${entries.length} legacy auth files`);
    } catch (e) {
      console.error("[bridge] legacy auth migration failed:", e);
    }
    if (existsSync(legacyAvatars) && statSync(legacyAvatars).isDirectory()) {
      try {
        const targetAvatars = avatarDir(LEGACY_ADMIN_USER_ID);
        const entries = readdirSync(legacyAvatars);
        for (const name of entries) {
          renameSync(join(legacyAvatars, name), join(targetAvatars, name));
        }
        console.log(`[bridge] moved ${entries.length} legacy avatar files`);
      } catch (e) {
        console.error("[bridge] legacy avatar migration failed:", e);
      }
    }
  }
  // ── auto-start every session with a non-empty auth dir on disk.
  if (!existsSync(SESSIONS_DIR)) {
    console.log(`[bridge] no sessions dir yet — waiting for explicit /users/:id/start`);
    return;
  }
  const userIds = readdirSync(SESSIONS_DIR).filter((name) => {
    const auth = authDir(name);
    if (!existsSync(auth)) return false;
    // Only count sessions that have credentials on disk — a freshly
    // created empty dir wouldn't have anything to reconnect to.
    return readdirSync(auth).length > 0;
  });
  console.log(`[bridge] discovered ${userIds.length} session(s) on disk: [${userIds.join(", ")}]`);
  for (const userId of userIds) {
    startSession(userId).catch((e) => {
      console.error(`[bridge] auto-start failed for user ${userId}:`, e);
    });
  }
}

// ───────────────────────── serializers (unchanged) ─────────────────────

function serializeMessage(m) {
  let msg = m.message || {};
  if (msg.ephemeralMessage?.message)        msg = msg.ephemeralMessage.message;
  if (msg.viewOnceMessage?.message)         msg = msg.viewOnceMessage.message;
  if (msg.viewOnceMessageV2?.message)       msg = msg.viewOnceMessageV2.message;
  if (msg.viewOnceMessageV2Extension?.message) msg = msg.viewOnceMessageV2Extension.message;
  if (msg.deviceSentMessage?.message)       msg = msg.deviceSentMessage.message;
  if (msg.documentWithCaptionMessage?.message) msg = msg.documentWithCaptionMessage.message;

  if (msg.protocolMessage ||
      msg.senderKeyDistributionMessage ||
      msg.reactionMessage ||
      msg.pollUpdateMessage ||
      msg.pollCreationMessage ||
      msg.messageContextInfo && Object.keys(msg).length === 1) {
    return null;
  }

  const conv =
    msg.conversation ||
    msg.extendedTextMessage?.text ||
    msg.imageMessage?.caption ||
    msg.videoMessage?.caption ||
    msg.documentMessage?.caption ||
    null;

  let mediaKind = null;
  let mimetype = null;
  let filename = null;
  if (msg.imageMessage)         { mediaKind = "image";    mimetype = msg.imageMessage.mimetype; }
  else if (msg.videoMessage)    { mediaKind = "video";    mimetype = msg.videoMessage.mimetype; }
  else if (msg.audioMessage)    { mediaKind = "audio";    mimetype = msg.audioMessage.mimetype; }
  else if (msg.documentMessage) {
    mediaKind = "document";
    mimetype = msg.documentMessage.mimetype;
    filename = msg.documentMessage.fileName || null;
  } else if (msg.stickerMessage) { mediaKind = "sticker"; mimetype = msg.stickerMessage.mimetype; }
  else if (msg.locationMessage)  { mediaKind = "location"; }
  else if (msg.contactMessage || msg.contactsArrayMessage) { mediaKind = "contact"; }

  if (!conv && !mediaKind) return null;

  return {
    id: m.key?.id,
    jid: m.key?.remoteJid,
    fromMe: !!m.key?.fromMe,
    participant: m.key?.participant || null,
    pushName: m.pushName || null,
    timestamp: Number(m.messageTimestamp) || Math.floor(Date.now() / 1000),
    text: conv,
    mediaKind,
    mimetype,
    filename,
    raw: null,
  };
}

function serializeChat(c) {
  return {
    jid: c.id,
    name: c.name || c.subject || null,
    unread: c.unreadCount || 0,
    isGroup: c.id?.endsWith("@g.us") || false,
    lastMessageTs: Number(c.conversationTimestamp) || null,
  };
}

// ─────────────────────────── HTTP API ──────────────────────────────────

const app = express();
app.use(express.json({ limit: "10mb" }));

// Backward-compat shim: rewrite legacy routes (no /users/:id prefix) so
// they target the legacy admin user. Lets the existing Python adapter
// keep working unchanged while the gradual cutover happens. Once Python
// is fully migrated to user-aware URLs, this middleware can be removed.
app.use((req, res, next) => {
  if (req.path.startsWith("/users/")) return next();
  // Allow-list of legacy paths we proxy.
  const legacyPrefixes = [
    "/status", "/qr", "/chats", "/media", "/profile-picture",
    "/logout",
  ];
  for (const p of legacyPrefixes) {
    if (req.path === p || req.path.startsWith(p + "/")) {
      req.url = `/users/${LEGACY_ADMIN_USER_ID}${req.url}`;
      return next();
    }
  }
  next();
});

// List active sessions (used by Python's /api/whatsapp/users for admin
// UI, and by the spike to see what's running).
app.get("/users", (req, res) => {
  const out = [];
  for (const [userId, s] of sessions) {
    out.push({
      userId,
      connected: s.connected,
      me: s.me,
      hasQr: !!s.lastQr,
      cachedMessages: s.msgCache.size,
    });
  }
  res.json(out);
});

// Idempotent: open the socket for this user. If already connected,
// returns the current state without reconnecting.
app.post("/users/:userId/start", async (req, res) => {
  const userId = req.params.userId;
  try {
    const session = await startSession(userId);
    res.json({
      ok: true,
      connected: session.connected,
      me: session.me,
      hasQr: !!session.lastQr,
    });
  } catch (e) {
    console.error(`[bridge] /users/${userId}/start failed:`, e);
    res.status(500).json({ error: "start_failed", detail: String(e) });
  }
});

app.get("/users/:userId/status", (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s) {
    return res.json({ connected: false, me: null, hasQr: false, cachedMessages: 0, exists: false });
  }
  res.json({
    connected: s.connected,
    me: s.me,
    hasQr: !!s.lastQr,
    cachedMessages: s.msgCache.size,
    exists: true,
  });
});

app.get("/users/:userId/qr", async (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s) return res.status(404).json({ error: "no_session" });
  if (s.connected) return res.status(204).end();
  if (!s.lastQr) return res.status(409).json({ error: "no_qr_yet" });
  try {
    const dataUrl = await QRCode.toDataURL(s.lastQr, { width: 320, margin: 1 });
    res.json({ qrPng: dataUrl, raw: s.lastQr });
  } catch (e) {
    res.status(500).json({ error: "qr_render_failed", detail: String(e) });
  }
});

app.get("/users/:userId/chats", (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s || !s.connected) return res.status(503).json({ error: "not_connected" });
  const byJid = new Map();
  for (const m of s.msgCache.values()) {
    const jid = m.key?.remoteJid;
    if (!jid) continue;
    const ts = Number(m.messageTimestamp) || 0;
    const prev = byJid.get(jid);
    if (!prev || ts > prev.lastMessageTs) {
      byJid.set(jid, {
        jid,
        name: m.pushName || null,
        isGroup: jid.endsWith("@g.us"),
        lastMessageTs: ts,
      });
    }
  }
  res.json(Array.from(byJid.values()));
});

app.get("/users/:userId/chats/:jid/messages", (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s || !s.connected) return res.status(503).json({ error: "not_connected" });
  const { jid } = req.params;
  const limit = Math.min(Number(req.query.limit) || 50, 200);
  const out = [];
  for (const m of s.msgCache.values()) {
    if (m.key?.remoteJid !== jid) continue;
    const ser = serializeMessage(m);
    if (ser) out.push(ser);
  }
  out.sort((a, b) => a.timestamp - b.timestamp);
  res.json(out.slice(-limit));
});

const AVATAR_TTL_MS = 24 * 60 * 60 * 1000;

app.get("/users/:userId/profile-picture/:jid", async (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s || !s.connected) return res.status(503).json({ error: "not_connected" });
  const { jid } = req.params;
  const safe = jid.replace(/[^A-Za-z0-9@._-]/g, "_");
  const dir = avatarDir(req.params.userId);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  const path = join(dir, `${safe}.jpg`);
  try {
    const { readFileSync, statSync: fsStat, writeFileSync } = await import("fs");
    if (existsSync(path)) {
      const age = Date.now() - fsStat(path).mtimeMs;
      if (age < AVATAR_TTL_MS) {
        res.setHeader("content-type", "image/jpeg");
        res.setHeader("cache-control", "public, max-age=3600");
        return res.send(readFileSync(path));
      }
    }
    const url = await s.sock.profilePictureUrl(jid, "image").catch(() => null);
    if (!url) {
      writeFileSync(`${path}.none`, "");
      return res.status(404).json({ error: "no_picture" });
    }
    const r = await fetch(url);
    if (!r.ok) return res.status(404).json({ error: "fetch_failed" });
    const buf = Buffer.from(await r.arrayBuffer());
    writeFileSync(path, buf);
    res.setHeader("content-type", "image/jpeg");
    res.setHeader("cache-control", "public, max-age=3600");
    res.send(buf);
  } catch (e) {
    console.error(`[bridge] profile picture error for ${req.params.userId}/${jid}:`, e);
    res.status(500).json({ error: "fetch_failed", detail: String(e) });
  }
});

app.get("/users/:userId/media/:msgId", async (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s) return res.status(404).json({ error: "no_session" });
  const m = s.msgCache.get(req.params.msgId);
  if (!m) return res.status(404).json({ error: "msg_not_cached" });
  try {
    const buf = await downloadMediaMessage(m, "buffer", {}, { logger: s.logger });
    const msg = m.message || {};
    const mime =
      msg.imageMessage?.mimetype ||
      msg.videoMessage?.mimetype ||
      msg.audioMessage?.mimetype ||
      msg.documentMessage?.mimetype ||
      "application/octet-stream";
    const fn = msg.documentMessage?.fileName;
    res.setHeader("content-type", mime);
    if (fn) res.setHeader("content-disposition", `attachment; filename="${fn}"`);
    res.send(buf);
  } catch (e) {
    console.error(`[bridge] media download failed for ${req.params.userId}:`, e);
    res.status(500).json({ error: "download_failed", detail: String(e) });
  }
});

app.post("/users/:userId/chats/:jid/send", async (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s || !s.connected) return res.status(503).json({ error: "not_connected" });
  const { jid } = req.params;
  const text = String(req.body?.text || "").trim();
  if (!text) return res.status(400).json({ error: "empty_text" });
  try {
    await s.sock.sendPresenceUpdate("composing", jid);
    const delay = 200 + Math.floor(Math.random() * 1300);
    await new Promise((r) => setTimeout(r, delay));
    await s.sock.sendPresenceUpdate("paused", jid);
    const sent = await s.sock.sendMessage(jid, { text });
    cacheMessage(s, sent);
    res.json({ msgId: sent.key?.id, ts: Math.floor(Date.now() / 1000) });
  } catch (e) {
    console.error(`[bridge] send failed for ${req.params.userId}:`, e);
    res.status(500).json({ error: "send_failed", detail: String(e) });
  }
});

app.post("/users/:userId/chats/:jid/typing", async (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s || !s.connected) return res.status(503).json({ error: "not_connected" });
  const composing = !!req.body?.composing;
  try {
    await s.sock.sendPresenceUpdate(composing ? "composing" : "paused", req.params.jid);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: "typing_failed", detail: String(e) });
  }
});

app.post("/users/:userId/chats/:jid/fetch-history", async (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s || !s.connected) return res.status(503).json({ error: "not_connected" });
  const { jid } = req.params;
  const count = Math.min(Number(req.body?.count) || 50, 200);
  let oldest = null;
  for (const m of s.msgCache.values()) {
    if (m.key?.remoteJid !== jid) continue;
    const ts = Number(m.messageTimestamp) || 0;
    if (!oldest || ts < oldest.ts) oldest = { ts, key: m.key };
  }
  if (!oldest) return res.status(409).json({ error: "no_pivot_message" });
  try {
    await s.sock.fetchMessageHistory(count, oldest.key, oldest.ts);
    res.json({ ok: true, requested: count });
  } catch (e) {
    res.status(500).json({ error: "fetch_history_failed", detail: String(e) });
  }
});

app.post("/users/:userId/logout", async (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s || !s.sock) return res.status(409).json({ error: "not_running" });
  try {
    await s.sock.logout();
    sessions.delete(req.params.userId);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: "logout_failed", detail: String(e) });
  }
});

// ─────────────────────────── WS events ─────────────────────────────────

const server = createServer(app);
const wss = new WebSocketServer({ server, path: "/events" });
wss.on("connection", (ws) => {
  wsClients.add(ws);
  // Hello payload describes every active session so the subscriber can
  // sync its idea of "who's connected right now" without waiting for
  // the next connection.update.
  const sessionsSummary = [];
  for (const [userId, s] of sessions) {
    sessionsSummary.push({ userId, connected: s.connected, me: s.me });
  }
  ws.send(JSON.stringify({ type: "hello", payload: { sessions: sessionsSummary } }));
  ws.on("close", () => wsClients.delete(ws));
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`[bridge] http+ws listening on :${PORT}`);
  console.log(`[bridge] data root: ${DATA_DIR}`);
  console.log(`[bridge] legacy admin user id: ${LEGACY_ADMIN_USER_ID}`);
  discoverAndAutoStart();
});
