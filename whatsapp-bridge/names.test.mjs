// The name Yorik shows must be the one WhatsApp shows. These are the
// rules that decide it — run with `node --test whatsapp-bridge/`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { nextName, resolveName, bareJid, nameRank } from "./names.js";

const session = (names, aliases = {}) => {
  const s = {
    nameByJid: new Map(Object.entries(names)),
    pnByLid: new Map(Object.entries(aliases)),
    lidByPn: new Map(Object.entries(aliases).map(([lid, pn]) => [pn, lid])),
  };
  return s;
};

test("a pushName does not overwrite the address-book name", () => {
  const prev = { name: "Elena Müller", source: "book" };
  assert.equal(nextName(prev, "Ela ✨", "push"), null);
});

test("the address-book name overwrites a pushName", () => {
  const prev = { name: "Ela ✨", source: "push" };
  assert.deepEqual(nextName(prev, "Elena Müller", "book"),
                   { name: "Elena Müller", source: "book" });
});

test("a rename on the phone arrives (same source, new name)", () => {
  const prev = { name: "Elena Müller", source: "book" };
  assert.deepEqual(nextName(prev, "Elena Schmidt", "book"),
                   { name: "Elena Schmidt", source: "book" });
});

test("nothing to do when the name is unchanged", () => {
  const prev = { name: "Elena Müller", source: "book" };
  assert.equal(nextName(prev, "  Elena Müller  ", "book"), null);
});

test("an empty name is never learned", () => {
  assert.equal(nextName(null, "   ", "book"), null);
});

test("a chat name loses to the address book but beats a pushName", () => {
  assert.equal(nextName({ name: "Elena Müller", source: "book" }, "Elena M.", "chat"), null);
  assert.deepEqual(nextName({ name: "Ela ✨", source: "push" }, "Elena M.", "chat"),
                   { name: "Elena M.", source: "chat" });
});

test("the name on the number answers for the LID the chat runs under", () => {
  const s = session(
    { "4915123456789@s.whatsapp.net": { name: "Elena Müller", source: "book" },
      "22227383536847@lid": { name: "Ela ✨", source: "push" } },
    { "22227383536847@lid": "4915123456789@s.whatsapp.net" },
  );
  const r = resolveName(s, "22227383536847@lid");
  assert.equal(r.name, "Elena Müller");
  assert.equal(r.source, "book");
});

test("a LID with no name of its own borrows the number's", () => {
  const s = session(
    { "4915123456789@s.whatsapp.net": { name: "Elena Müller", source: "book" } },
    { "22227383536847@lid": "4915123456789@s.whatsapp.net" },
  );
  assert.equal(resolveName(s, "22227383536847@lid").name, "Elena Müller");
});

test("a tie keeps the name that belongs to the asked-for address", () => {
  const s = session(
    { "4915123456789@s.whatsapp.net": { name: "Number side", source: "push" },
      "22227383536847@lid": { name: "LID side", source: "push" } },
    { "22227383536847@lid": "4915123456789@s.whatsapp.net" },
  );
  assert.equal(resolveName(s, "22227383536847@lid").name, "LID side");
});

test("an unknown JID resolves to nothing", () => {
  assert.equal(resolveName(session({}), "4915999999999@s.whatsapp.net"), null);
  assert.equal(resolveName(null, "x@lid"), null);
});

test("device suffixes are stripped, the rest is left alone", () => {
  assert.equal(bareJid("4915785800852:38@s.whatsapp.net"), "4915785800852@s.whatsapp.net");
  assert.equal(bareJid("7232943059091:38@lid"), "7232943059091@lid");
  assert.equal(bareJid("4915123456789@s.whatsapp.net"), "4915123456789@s.whatsapp.net");
  assert.equal(bareJid(null), "");
});

test("an unknown source ranks below every known one", () => {
  assert.equal(nameRank("nonsense"), 0);
  assert.deepEqual(nextName({ name: "x", source: "nonsense" }, "Elena", "push"),
                   { name: "Elena", source: "push" });
});
