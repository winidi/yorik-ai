// How Yorik picks the name it shows for a WhatsApp chat.
//
// WhatsApp shows one name per person, chosen from a fixed order of
// sources. Yorik shows the same one, so that order lives here:
//
//   book      the name from the phone's address book, synced through
//             the app state (`contactAction.fullName`) — what WhatsApp
//             itself puts in the chat list. A group's subject counts
//             as one too: it is the real name of the group.
//   business  the verified name of a business account.
//   chat      the name Baileys carries on a chat row in the history
//             sync. Usually the address-book name, but we cannot tell
//             for sure, so it ranks below `book`.
//   push      the name the other person chose for themselves. The last
//             resort, and the UI marks it with a leading "~", the way
//             WhatsApp marks it.
//
// A weaker source never overwrites a stronger one. An equally strong
// one does, so that renaming someone on the phone arrives here.
//
// Kept in its own file so it can be tested without opening a socket:
// `node --test whatsapp-bridge/`.

export const NAME_RANK = { push: 1, chat: 2, business: 2, book: 3 };

export const nameRank = (source) => NAME_RANK[source] || 0;

// "4915123456789:38@s.whatsapp.net" → "4915123456789@s.whatsapp.net".
// Device suffixes appear on our own JIDs and on group participants.
export const bareJid = (j) => String(j || "").replace(/:\d+(?=@)/, "");

// One person, two addresses: WhatsApp increasingly runs chats under a
// LID (`...@lid`) while the address-book name arrives under the phone
// number. `session.pnByLid` ties the two together, so a question about
// either address is answered from whichever entry is stronger.
export function aliasesOf(session, jid) {
  const out = [];
  if (!session || !jid) return out;
  const pn = session.pnByLid?.get(jid);
  if (pn) out.push(pn);
  const lid = session.lidByPn?.get(jid);
  if (lid) out.push(lid);
  return out;
}

// The name to show for a JID: the strongest entry for it or for the
// same person's other address. Ties keep the JID's own entry.
export function resolveName(session, jid) {
  if (!session || !jid) return null;
  const candidates = [session.nameByJid?.get(jid)];
  for (const alt of aliasesOf(session, jid)) candidates.push(session.nameByJid?.get(alt));
  let best = null;
  for (const c of candidates) {
    if (!c?.name) continue;
    if (!best || nameRank(c.source) > nameRank(best.source)) best = c;
  }
  return best || null;
}

// Decide what a name learned now does to what we already have.
// Returns the entry to store, or null to keep what is there.
export function nextName(prev, name, source) {
  const n = (name || "").trim();
  if (!n) return null;
  if (prev) {
    if (nameRank(source) < nameRank(prev.source)) return null;   // never downgrade
    if (prev.name === n && prev.source === source) return null;  // no-op
  }
  return { name: n, source };
}
