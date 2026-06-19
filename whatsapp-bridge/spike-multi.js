// Multi-session spike for the planned per-user WhatsApp bridge.
//
// Question this answers: can two makeWASocket() instances run in the
// same Node process with isolated auth-state, independent connection
// lifecycles, and tagged events — without Baileys' internals (logger,
// signal store, version cache) tripping over each other?
//
// If yes, the multi-tenant bridge refactor (one process, Map<userId,
// session>) is viable. If no, we pivot to one bridge container per
// user.
//
// SAFE TO RUN: uses /tmp/yorik-baileys-spike/* for auth state, so it
// does NOT touch the running bridge's /data/baileys-auth. The
// production WhatsApp session keeps working untouched.
//
// How to run (no Docker required, no risk to prod bridge):
//
//   cd whatsapp-bridge
//   npm install                      # one-time, creates ./node_modules
//   node spike-multi.js
//
// What to look for:
//
//   - "session A connecting" + "session B connecting" — both start
//   - Two DISTINCT QR strings printed (one per session). If you only
//     see one QR or the same QR twice, the lib has shared state.
//   - "[A] connection.update" + "[B] connection.update" interleaved
//     with no crashes — independent lifecycles confirmed.
//
// Optional full pair test (requires two WhatsApp accounts):
//
//   - Scan QR A with phone 1, scan QR B with phone 2.
//   - Send a message to one of the numbers.
//   - You should see "[A] messages.upsert" OR "[B] messages.upsert"
//     for that specific session — never both. That confirms event
//     isolation at the chat level.
//
// Exits cleanly on Ctrl-C. Auth state survives in /tmp so re-running
// is fast (no second QR scan). Wipe with `rm -rf /tmp/yorik-baileys-spike`.

import {
  default as makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
} from "@whiskeysockets/baileys";
import pino from "pino";
import { mkdirSync, existsSync } from "fs";

const SPIKE_ROOT = "/tmp/yorik-baileys-spike";
if (!existsSync(SPIKE_ROOT)) mkdirSync(SPIKE_ROOT, { recursive: true });

const logger = pino({ level: "warn" });

// Stand-in for "user_id". In the real bridge these become the actual
// SQL user ids from the family.db users table.
const SESSIONS = ["A", "B"];

async function startSession(tag) {
  const authDir = `${SPIKE_ROOT}/${tag}`;
  if (!existsSync(authDir)) mkdirSync(authDir, { recursive: true });

  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion();

  console.log(`[${tag}] starting with Baileys ${version.join(".")} auth=${authDir}`);

  const sock = makeWASocket({
    version,
    auth: state,
    logger,
    // Distinct browser names so WhatsApp shows them as separate linked
    // devices on each phone's "linked devices" screen.
    browser: [`Yorik-spike-${tag}`, "Chrome", "1.0"],
    printQRInTerminal: false,
    syncFullHistory: false, // spike doesn't need history; just connection test
    markOnlineOnConnect: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      console.log(`[${tag}] QR available (first 24 chars): ${qr.slice(0, 24)}…`);
      console.log(`[${tag}] full QR length=${qr.length}`);
    }
    if (connection) {
      console.log(`[${tag}] connection.update -> ${connection}` +
        (lastDisconnect ? ` (code=${lastDisconnect?.error?.output?.statusCode})` : ""));
    }
    if (connection === "open") {
      console.log(`[${tag}] CONNECTED as ${sock.user?.id} (${sock.user?.name})`);
    }
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    for (const m of messages) {
      const chat = m.key?.remoteJid;
      const fromMe = m.key?.fromMe ? "→" : "←";
      const text =
        m.message?.conversation ||
        m.message?.extendedTextMessage?.text ||
        "[non-text]";
      console.log(`[${tag}] messages.upsert ${type} ${fromMe} ${chat}: ${text.slice(0, 60)}`);
    }
  });

  return sock;
}

console.log(`── spike: starting ${SESSIONS.length} concurrent Baileys sessions ──`);
console.log(`auth state isolated under ${SPIKE_ROOT}`);
console.log(`prod bridge at /data/baileys-auth is NOT touched`);
console.log();

const sockets = await Promise.all(SESSIONS.map(startSession));

console.log();
console.log(`── ${sockets.length} sessions started. Ctrl-C to exit. ──`);

process.on("SIGINT", () => {
  console.log("\nshutting down…");
  for (const s of sockets) {
    try { s.end(); } catch {}
  }
  process.exit(0);
});
