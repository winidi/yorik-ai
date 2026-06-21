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
  // Per-session name map, persisted to disk so bridge restarts
  // (deployments, server reboots, image rebuilds) don't lose what
  // Baileys has taught us. File lives next to the auth dir as
  // /data/sessions/<uid>/name-map.json — small JSON dict {jid: name}.
  //
  // Persistence is debounced: writing on every _learn() would thrash
  // disk during init-sync bursts (thousands of names in seconds).
  // We schedule one write per ~500ms instead.
  session.nameByJid = new Map();
  const nameMapPath = join(SESSIONS_DIR, userId, "name-map.json");
  try {
    const { readFileSync } = await import("fs");
    if (existsSync(nameMapPath)) {
      const loaded = JSON.parse(readFileSync(nameMapPath, "utf-8"));
      for (const [jid, name] of Object.entries(loaded || {})) {
        if (typeof name === "string" && name) session.nameByJid.set(jid, name);
      }
      session.logger.info(`name-map: loaded ${session.nameByJid.size} entries from disk`);
    }
  } catch (e) {
    session.logger.warn({ err: String(e) }, "name-map: load failed (continuing with empty map)");
  }

  let _saveTimer = null;
  const _scheduleSave = () => {
    if (_saveTimer) return;
    _saveTimer = setTimeout(async () => {
      _saveTimer = null;
      try {
        const { writeFileSync } = await import("fs");
        const obj = Object.fromEntries(session.nameByJid);
        writeFileSync(nameMapPath, JSON.stringify(obj));
      } catch (e) {
        session.logger.warn({ err: String(e) }, "name-map: save failed");
      }
    }, 500);
  };
  session._saveNameMap = _scheduleSave;

  const _learn = (jid, name) => {
    if (!jid || typeof name !== "string") return;
    const n = name.trim();
    if (!n) return;
    if (n === jid) return;
    const prev = session.nameByJid.get(jid);
    if (prev === n) return;  // no-op, skip the disk write
    session.nameByJid.set(jid, n);
    _scheduleSave();
  };

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
      // Every inbound message carries a pushName that names the sender
      // (or the group participant who sent it). Capture both so @lid
      // pseudo-JIDs we only ever see as group participants still
      // resolve to a human name.
      const remote = m.key?.remoteJid;
      const participant = m.key?.participant || null;
      const pushName = m.pushName;
      if (pushName) {
        _learn(participant || remote, pushName);
      }
      if (type === "notify" || type === "append") {
        const s = serializeMessage(m);
        if (s) broadcast(userId, "message", s);
      }
    }
  });

  sock.ev.on("messaging-history.set", (payload) => {
    const { chats = [], contacts = [], messages = [], isLatest = false } = payload;
    let kept = 0;
    for (const c of chats) {
      _learn(c.id, c.name || c.subject);
      broadcast(userId, "chat", serializeChat(c));
    }
    // The history payload also carries a contacts array — Baileys'
    // initial-sync snapshot of the user's address book. Most reliable
    // place to populate the name map after a restart.
    for (const ct of contacts) _learn(ct.id, ct.name || ct.notify || ct.verifiedName);
    for (const m of messages) {
      cacheMessage(session, m);
      const remote = m.key?.remoteJid;
      const participant = m.key?.participant || null;
      if (m.pushName) _learn(participant || remote, m.pushName);
      const s = serializeMessage(m);
      if (s) { broadcast(userId, "message", s); kept++; }
    }
    session.logger.info(
      `history sync: ${chats.length} chats, ${contacts.length} contacts, ${kept}/${messages.length} messages forwarded (isLatest=${isLatest}, nameMap=${session.nameByJid.size})`
    );
  });

  sock.ev.on("chats.upsert", (chats) => {
    for (const c of chats) {
      _learn(c.id, c.name || c.subject);
      broadcast(userId, "chat", serializeChat(c));
    }
  });
  sock.ev.on("chats.update", (chats) => {
    for (const c of chats) {
      _learn(c.id, c.name || c.subject);
      broadcast(userId, "chat", serializeChat(c));
    }
  });
  sock.ev.on("contacts.upsert", (contacts) => {
    for (const c of contacts) {
      const name = c.name || c.notify || c.verifiedName;
      _learn(c.id, name);
      if (!c.id || !name) continue;
      broadcast(userId, "chat", { jid: c.id, name, isGroup: false });
    }
  });
  sock.ev.on("contacts.update", (updates) => {
    for (const c of updates) {
      _learn(c.id, c.name || c.notify || c.verifiedName);
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

// Names from Baileys' in-memory contact map. Baileys keeps a live
// store of every contact it has ever seen — populated from the
// chats sync, push events on every inbound, contacts.upsert, etc.
// Each entry can carry up to three name fields: `name` (the contact's
// WhatsApp profile name), `notify` (the pushName the contact has
// configured), and `verifiedName` (only for business accounts).
// First non-empty wins.
//
// Two response shapes:
//   GET /users/:userId/contact-names           → {jid: name, ...}
//                                                 (all known contacts)
//   GET /users/:userId/contact-names/:jid      → {jid, name}
//
// Used by the backend's WhatsApp-names backfill as the strongest
// source available (after wa_chats.name and message.push_name —
// this one is most likely to actually have something for LID
// pseudo-JIDs that the other sources can't see).
function _pickContactName(c) {
  if (!c) return null;
  const candidates = [c.name, c.notify, c.verifiedName];
  for (const n of candidates) {
    if (n && typeof n === "string" && n.trim()) return n.trim();
  }
  return null;
}

app.get("/users/:userId/contact-names", (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s) return res.status(503).json({ error: "no_session" });
  // Prefer our accumulator (populated from every event with a name);
  // fall back to Baileys' sock.contacts if it happens to be populated.
  const out = {};
  if (s.nameByJid) {
    for (const [jid, name] of s.nameByJid.entries()) out[jid] = name;
  }
  const store = s.sock?.contacts || {};
  for (const jid of Object.keys(store)) {
    if (out[jid]) continue;
    const n = _pickContactName(store[jid]);
    if (n) out[jid] = n;
  }
  res.json({ count: Object.keys(out).length, contacts: out });
});

// Active per-JID name lookup. The passive listener stack
// (contacts.upsert, messaging-history.set, message pushNames) only
// gets us names when Meta pushes them — and Meta is selective.
// For JIDs where we have ZERO data (chat-stub rows from an old
// init-sync that arrived without names), we need to actively poke
// Meta's per-JID channel. presenceSubscribe is the cleanest:
// "tell me about this contact's presence" → Meta typically responds
// with a contacts.update carrying the name.
//
// Body: { jids: string[], waitMs?: number }
// Returns: { found: {jid: name, ...}, queried: number, took_ms: number }
//
// Rate-limited: max ACTIVE_LOOKUP_MAX_CONCURRENT presenceSubscribe
// calls in flight, ACTIVE_LOOKUP_GAP_MS between starts. Same pattern
// as the avatar concurrency cap — keeps us well under Meta's
// per-IP burst threshold.
const ACTIVE_LOOKUP_MAX_CONCURRENT = Number(process.env.YORIK_WA_LOOKUP_MAX_CONCURRENT || 4);
const ACTIVE_LOOKUP_GAP_MS         = Number(process.env.YORIK_WA_LOOKUP_GAP_MS         || 80);
const ACTIVE_LOOKUP_DEFAULT_WAIT   = Number(process.env.YORIK_WA_LOOKUP_DEFAULT_WAIT_MS || 4000);

async function _subscribePresenceWithThrottle(sock, jids, perUserLogger) {
  let inFlight = 0;
  let started  = 0;
  let errored  = 0;
  const sleep  = (ms) => new Promise((r) => setTimeout(r, ms));
  for (const jid of jids) {
    while (inFlight >= ACTIVE_LOOKUP_MAX_CONCURRENT) await sleep(20);
    inFlight++;
    started++;
    sock.presenceSubscribe(jid)
      .catch((e) => {
        errored++;
        // Most failures are "not on WhatsApp" or transient — debug, not warn,
        // because this fires for every unknown JID on lookup and would spam.
        perUserLogger.debug?.({ jid, err: String(e) }, "presenceSubscribe failed");
      })
      .finally(() => { inFlight--; });
    await sleep(ACTIVE_LOOKUP_GAP_MS);
  }
  // Wait for last in-flight to settle so the caller's waitMs starts
  // counting AFTER all subscribes are actually sent.
  while (inFlight > 0) await sleep(20);
  return { started, errored };
}

app.post("/users/:userId/lookup-contact-names", async (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s || !s.connected) return res.status(503).json({ error: "not_connected" });
  const jids = Array.isArray(req.body?.jids) ? req.body.jids.filter(j => typeof j === "string" && j) : [];
  if (jids.length === 0) return res.json({ found: {}, queried: 0, took_ms: 0 });
  // Hard cap to keep request latency bounded — Meta is unlikely to
  // answer for huge batches anyway, and the cap protects us from a
  // misconfigured frontend dumping 10k JIDs in one call.
  const MAX_BATCH = 200;
  const batch = jids.slice(0, MAX_BATCH);
  const waitMs = Math.min(Math.max(Number(req.body?.waitMs) || ACTIVE_LOOKUP_DEFAULT_WAIT, 0), 15000);

  const started = Date.now();
  // Snapshot names we already had before subscribing — lets us tell
  // the caller how many are NEW vs already-known, useful for UI.
  const preexisting = new Set();
  for (const jid of batch) {
    if (s.nameByJid?.has(jid)) preexisting.add(jid);
  }

  await _subscribePresenceWithThrottle(s.sock, batch, s.logger);

  // Let Baileys event callbacks run. We can't await individual
  // contacts.update events because they're delivered globally, not
  // per-call. Waiting a few seconds is the cheap pragmatic answer.
  await new Promise((r) => setTimeout(r, waitMs));

  const found = {};
  let newCount = 0;
  for (const jid of batch) {
    const name = s.nameByJid?.get(jid);
    if (name) {
      found[jid] = name;
      if (!preexisting.has(jid)) newCount++;
    }
  }
  s.logger.info(
    `lookup-contact-names: queried=${batch.length} preexisting=${preexisting.size} new=${newCount} took_ms=${Date.now() - started}`,
  );
  res.json({
    found,
    queried: batch.length,
    new_names: newCount,
    took_ms: Date.now() - started,
  });
});


app.get("/users/:userId/contact-names/:jid", (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s) return res.status(503).json({ error: "no_session" });
  const { jid } = req.params;
  const fromMap = s.nameByJid?.get(jid);
  if (fromMap) return res.json({ jid, name: fromMap });
  const n = _pickContactName(s.sock?.contacts?.[jid]);
  if (!n) return res.status(404).json({ error: "no_name" });
  res.json({ jid, name: n });
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

// Cache TTLs. Positive (we have a picture) refreshes daily because
// people occasionally change profile pictures. Negative (Meta said
// "no picture") refreshes much less often because "no profile pic"
// is the steady state for many contacts and re-asking is what
// triggers Meta's anti-abuse heuristics. ENV-overridable for
// debugging without rebuilding the bridge.
const AVATAR_TTL_MS         = Number(process.env.YORIK_WA_AVATAR_TTL_MS         || 24 * 60 * 60 * 1000);   // 1 day
const AVATAR_NEG_TTL_MS     = Number(process.env.YORIK_WA_AVATAR_NEG_TTL_MS     ||  7 * 24 * 60 * 60 * 1000); // 7 days
const AVATAR_MAX_CONCURRENT = Number(process.env.YORIK_WA_AVATAR_MAX_CONCURRENT || 4);

// Bridge-wide semaphore on outbound Meta calls. Without it, a single
// page-load with 100 WhatsApp contacts fires 100 parallel
// profilePictureUrl calls — exactly the burst pattern Meta uses to
// detect bots. Capped at AVATAR_MAX_CONCURRENT in flight; the rest
// queue. Per process, NOT per session — Meta sees ONE bridge IP
// regardless of how many user sessions live behind it.
let _avatarInFlight = 0;
const _avatarWaitQueue = [];
function _acquireAvatarSlot() {
  return new Promise((resolve) => {
    if (_avatarInFlight < AVATAR_MAX_CONCURRENT) {
      _avatarInFlight++;
      return resolve();
    }
    _avatarWaitQueue.push(resolve);
  });
}
function _releaseAvatarSlot() {
  const next = _avatarWaitQueue.shift();
  if (next) next();
  else _avatarInFlight--;
}

// In-flight dedupe: when the frontend renders the contact list,
// duplicate avatar requests for the same JID can arrive in the
// same millisecond (e.g. virtualised list re-rendering a row).
// Without this, each duplicate is its own Meta call. The Map keys
// by per-user safe-jid so different sessions don't collide.
const _avatarPending = new Map();

app.get("/users/:userId/profile-picture/:jid", async (req, res) => {
  const s = sessions.get(req.params.userId);
  if (!s || !s.connected) return res.status(503).json({ error: "not_connected" });
  const { jid } = req.params;
  const safe = jid.replace(/[^A-Za-z0-9@._-]/g, "_");
  const dir = avatarDir(req.params.userId);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  const path = join(dir, `${safe}.jpg`);
  const negPath = `${path}.none`;
  const { readFileSync, statSync: fsStat, writeFileSync } = await import("fs");

  // Fast path 1: cached image, still fresh.
  if (existsSync(path)) {
    const age = Date.now() - fsStat(path).mtimeMs;
    if (age < AVATAR_TTL_MS) {
      res.setHeader("content-type", "image/jpeg");
      res.setHeader("cache-control", "public, max-age=3600");
      return res.send(readFileSync(path));
    }
  }
  // Fast path 2: negative cache, still fresh. The original code
  // wrote `.none` but never read it — every 404'd contact then
  // generated a fresh Meta call on every page load. Honoring the
  // sentinel cuts page-load Meta traffic in half on typical accounts.
  if (existsSync(negPath)) {
    const age = Date.now() - fsStat(negPath).mtimeMs;
    if (age < AVATAR_NEG_TTL_MS) {
      // Cache-control hints to the browser too; together with the
      // backend's miss-cache the same JID won't even reach this
      // bridge for the rest of the negative window.
      res.setHeader("cache-control", "public, max-age=86400");
      return res.status(404).json({ error: "no_picture", cached: true });
    }
  }

  // Slow path: actually call Meta. Dedupe in-flight + cap concurrency
  // so a busy page can't blast Meta. Cleanup is in the finally.
  const dedupeKey = `${req.params.userId}:${safe}`;
  let pending = _avatarPending.get(dedupeKey);
  if (!pending) {
    pending = (async () => {
      await _acquireAvatarSlot();
      try {
        const url = await s.sock.profilePictureUrl(jid, "image").catch(() => null);
        if (!url) {
          writeFileSync(negPath, "");
          return { ok: false };
        }
        const r = await fetch(url);
        if (!r.ok) {
          // Meta CDN sometimes returns 403 for contacts who restricted
          // their photo visibility — same end-state for us as "no pic".
          // Cache that too so we don't retry every load.
          writeFileSync(negPath, "");
          return { ok: false };
        }
        const buf = Buffer.from(await r.arrayBuffer());
        writeFileSync(path, buf);
        return { ok: true, buf };
      } catch (e) {
        // Network error / Baileys threw — DON'T write the negative
        // cache (the contact may have a picture; we just couldn't
        // reach it right now). Return an error WITHOUT a long
        // cache so the next page load tries again.
        return { ok: false, transient: true, err: String(e) };
      } finally {
        _releaseAvatarSlot();
        _avatarPending.delete(dedupeKey);
      }
    })();
    _avatarPending.set(dedupeKey, pending);
  }

  const result = await pending;
  if (result.ok) {
    res.setHeader("content-type", "image/jpeg");
    res.setHeader("cache-control", "public, max-age=3600");
    return res.send(result.buf);
  }
  if (result.transient) {
    res.setHeader("cache-control", "no-store");
    return res.status(502).json({ error: "fetch_failed", detail: result.err });
  }
  res.setHeader("cache-control", "public, max-age=86400");
  res.status(404).json({ error: "no_picture" });
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


// Full session teardown — used by the "Disconnect WhatsApp" button in
// the UI. Three things have to happen for the next pair to be clean:
//   1. WhatsApp-side logout (polite — removes the device from the
//      user's phone "Linked Devices" list)
//   2. Disk-side auth wipe (otherwise Baileys reuses the dead creds
//      on the next session start)
//   3. Fresh session start (so a QR appears immediately for the new
//      account, no bridge restart needed)
//
// Tolerant of every "missing" state: if there's no session in memory,
// we still wipe the auth dir; if the logout call fails because Meta
// already de-paired us, we still wipe + restart. Idempotent — calling
// twice in a row is safe.
app.post("/users/:userId/disconnect", async (req, res) => {
  const { userId } = req.params;
  const result = {
    logged_out: false,
    auth_wiped: false,
    restarted:  false,
    warnings:   [],
  };

  // Step 1: WhatsApp-side logout. Tolerate any failure (already
  // disconnected, network blip, Meta refused) — we still want to
  // clear the local state.
  const s = sessions.get(userId);
  if (s && s.sock) {
    try {
      await s.sock.logout();
      result.logged_out = true;
    } catch (e) {
      result.warnings.push(`logout: ${String(e)}`);
    }
    sessions.delete(userId);
  }

  // Step 2: disk-side auth wipe. rm -rf the auth dir so Baileys can't
  // reuse the dead creds. Also wipe the persisted name-map — the next
  // account's contacts won't share names with the old one and keeping
  // stale entries would mis-name new pairings. Avatar cache is
  // intentionally left alone (per-JID, the same JID would resolve to
  // the same picture across accounts).
  try {
    const { rmSync, existsSync } = await import("fs");
    const dir = authDir(userId);
    if (existsSync(dir)) {
      rmSync(dir, { recursive: true, force: true });
      result.auth_wiped = true;
    }
    const nameMapFile = join(SESSIONS_DIR, userId, "name-map.json");
    if (existsSync(nameMapFile)) rmSync(nameMapFile, { force: true });
  } catch (e) {
    result.warnings.push(`auth-wipe: ${String(e)}`);
  }

  // Step 3: start a fresh session so a QR is ready for the new pair
  // without a bridge restart. The new session's connection.update
  // event will fire with the QR string and our existing broadcast
  // path will surface it to the frontend.
  try {
    await startSession(userId);
    result.restarted = true;
  } catch (e) {
    result.warnings.push(`restart: ${String(e)}`);
  }

  res.json({ ok: true, ...result });
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
