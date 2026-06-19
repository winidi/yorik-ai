/**
 * RecipientPicker — small popover next to recipient-name args in the
 * Compose arg panel. Pick a contact → fills both the name field AND
 * any sibling address field that shares the prefix.
 *
 * Templates name their args wildly differently (mieter_name, recipient_name,
 * najemca_imie_nazwisko, locatore_nome, …). We detect "looks like a name
 * field" by suffix and then guess the sibling address arg by prefix.
 * The mapping is in NAME_SUFFIXES / ADDRESS_SUFFIXES below.
 */

import { useEffect, useRef, useState } from "react";
import { UsersRound, Search, Loader2, Briefcase, X, Mail, Phone, MapPin } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import type { Contact } from "../contacts/types";

// Lowercased suffix → matches `_name`, `name` end-of-key, etc.
// The leading underscore is optional so a key like `recipientname` would
// also match. Ordered roughly by likely length so longer suffixes win.
export const NAME_SUFFIXES = [
  "_imie_nazwisko",   // pl: first + last name
  "imie_nazwisko",
  "_nazwisko",        // pl: last name
  "nazwisko",
  "_nazwa",           // pl: business name
  "nazwa",
  "_nome",            // it
  "nome",
  "_name",            // en / de stem
  "name",
] as const;

// Address-side suffixes. Detection is "starts with the same prefix as a
// matched name arg and ends with one of these."
export const ADDRESS_SUFFIXES = [
  "_address_line1",   // en (US-style multi-line)
  "_address",         // en
  "address",
  "_adresse",         // de
  "adresse",
  "_indirizzo",       // it
  "indirizzo",
  "_adres",           // pl
  "adres",
] as const;

export interface RecipientFillResult {
  /** New value for the name arg (= contact.display_name). */
  name: string;
  /** New value for the matching address arg, if one was found. */
  address?: string;
  /** The key of the address arg we filled, so the caller can write it. */
  addressKey?: string;
  /** Contact id — caller can stash this on draft state so the
   *  "save this address?" prompt later knows the recipient was picked
   *  from the hub (skip the prompt). */
  contactId: number;
  /**
   * Per-role value mapping the caller uses to fill every group-tagged
   * arg from the picked contact. The legacy {name, address, addressKey}
   * fields above are kept for back-compat with the single-recipient
   * code path; new code should prefer iterating fillsByRole.
   *
   * Keys are template `role` values; values come from the picked
   * contact's stored fields (display_name / addresses / channels /
   * iban / tax_id / etc.). Roles with no available data are absent.
   */
  fillsByRole: Partial<Record<string, string>>;
}

/**
 * Map a single contact to per-role values for the role enum that
 * templates use under `ask_user_for_args[].role`. Roles with no
 * matching contact data are absent from the result.
 */
export function contactRoleValues(c: Contact): Partial<Record<string, string>> {
  const out: Partial<Record<string, string>> = {};
  out["recipient_name"] = c.display_name || "";
  if (c.legal_name) out["recipient_legal_name"] = c.legal_name;
  if (c.tax_id) out["recipient_tax_id"] = c.tax_id;
  if (c.iban) out["recipient_iban"] = c.iban;
  if (c.salutation_pref) out["recipient_salutation"] = c.salutation_pref;
  if (c.birthday) out["recipient_birthday"] = c.birthday;
  if (c.language_pref) out["recipient_language"] = c.language_pref;
  const addr = formatPrimaryAddress(c);
  if (addr) out["recipient_address"] = addr;
  for (const ch of (c.channels || [])) {
    const k = (ch.kind || "").toLowerCase();
    const v = (ch.value || "").trim();
    if (!v) continue;
    if (k === "email" && !out["recipient_email"]) out["recipient_email"] = v;
    else if (k === "phone" && !out["recipient_phone"]) out["recipient_phone"] = v;
    else if (k === "website" && !out["recipient_website"]) out["recipient_website"] = v;
  }
  // First / last name split — best-effort from display_name. Used by
  // templates that letter-greet ("Sehr geehrter Herr <last_name>").
  const parts = (c.display_name || "").trim().split(/\s+/);
  if (parts.length > 1) {
    out["recipient_first_name"] = parts.slice(0, -1).join(" ");
    out["recipient_last_name"]  = parts[parts.length - 1];
  } else if (parts.length === 1 && parts[0]) {
    out["recipient_first_name"] = parts[0];
  }
  return out;
}

/** Detect the name suffix on a key (or null). Match is case-insensitive. */
export function detectNameSuffix(key: string): string | null {
  const lower = key.toLowerCase();
  for (const s of NAME_SUFFIXES) {
    if (lower.endsWith(s)) return s;
  }
  return null;
}

/** Given a name-arg key and the full set of arg keys, find the sibling
 * address key. Returns null if no sibling matches the prefix. */
export function findAddressKeyForName(nameKey: string, allKeys: string[]): string | null {
  const suffix = detectNameSuffix(nameKey);
  if (!suffix) return null;
  const prefix = nameKey.slice(0, nameKey.length - suffix.length).toLowerCase();
  // Search all other keys for one that begins with the same prefix and
  // ends with one of the address suffixes.
  for (const k of allKeys) {
    if (k === nameKey) continue;
    const klow = k.toLowerCase();
    if (!klow.startsWith(prefix)) continue;
    for (const as of ADDRESS_SUFFIXES) {
      if (klow.endsWith(as)) return k;
    }
  }
  return null;
}

/** Format a contact's primary postal address as a single multi-line
 * string suitable for letterhead. Picks home → work → billing → shipping
 * → first available. */
export function formatPrimaryAddress(c: Contact): string {
  if (!c.addresses || c.addresses.length === 0) return "";
  const order = ["home", "work", "billing", "shipping", "other"];
  const sorted = [...c.addresses].sort((a, b) =>
    order.indexOf(a.kind) - order.indexOf(b.kind),
  );
  const a = sorted[0];
  const lines = [
    a.line1,
    a.line2,
    [a.postcode, a.city].filter(Boolean).join(" "),
    a.region,
    a.country,
  ].map(s => (s || "").trim()).filter(Boolean);
  return lines.join("\n");
}

interface Props {
  /** All current arg keys — used to find the sibling address arg when
   * `precomputedAddressKey` is not provided. */
  allArgKeys: string[];
  /** The name-arg key this picker is anchored to. */
  nameKey: string;
  /** Called with the chosen contact + the address arg key (if any). */
  onPick: (r: RecipientFillResult) => void;
  /** Pre-resolved address arg key (from declarative role + contact_group).
   * When `undefined`, fall back to prefix detection over allArgKeys.
   * When explicit `null`, skip address fill (template intentionally has
   * no address sibling for this name slot). */
  precomputedAddressKey?: string | null;
  /** Optional label suffix for the button — set when the template
   * declared a `contact_group` so the user can tell pickers apart
   * ("From contacts — Arbeitgeber" vs "From contacts — Verwalter"). */
  groupLabel?: string;
}

export function RecipientPicker({ allArgKeys, nameKey, onPick, precomputedAddressKey, groupLabel }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Contact[]>([]);
  const [loading, setLoading] = useState(false);
  const popRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Search debounce — 200ms is enough to feel snappy without hammering.
  useEffect(() => {
    if (!open) return;
    const handle = window.setTimeout(async () => {
      setLoading(true);
      try {
        const params = new URLSearchParams({ status: "active", limit: "20" });
        if (query.trim()) params.set("q", query.trim());
        const list = await api.get<Contact[]>(`/api/contacts?${params}`);
        setResults(list);
      } catch (e) {
        console.error("recipient picker: search failed", e);
        setResults([]);
      } finally { setLoading(false); }
    }, 200);
    return () => window.clearTimeout(handle);
  }, [open, query]);

  // Click-outside + esc to close.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (popRef.current && !popRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Focus the search input the moment the popover opens.
  useEffect(() => {
    if (open) requestAnimationFrame(() => inputRef.current?.focus());
  }, [open]);

  function pick(c: Contact) {
    // precomputedAddressKey wins (set by parent from declarative role +
    // contact_group). Only fall back to prefix detection when the
    // template hasn't declared roles — then explicit `null` means
    // "intentionally no address sibling, skip address fill."
    const addressKey = precomputedAddressKey === undefined
      ? (findAddressKeyForName(nameKey, allArgKeys) || undefined)
      : (precomputedAddressKey || undefined);
    const address = addressKey ? formatPrimaryAddress(c) : undefined;
    onPick({
      contactId: c.id,
      name: c.display_name,
      address: address || undefined,
      addressKey,
      fillsByRole: contactRoleValues(c),
    });
    setOpen(false);
    setQuery("");
  }

  return (
    <div className="relative inline-flex" ref={popRef}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className={cn(
          // h-8 on mobile clears HIG minimum; desktop stays compact
          // at its previous ~18pt pill.
          "text-xs md:text-[9px] inline-flex items-center gap-1 px-2.5 md:px-1.5 h-8 md:h-auto md:py-0.5 rounded-full transition border",
          open
            ? "bg-amber-500/20 text-amber-600 border-amber-500/40"
            : "bg-amber-500/10 text-amber-600 border-amber-500/20 hover:bg-amber-500/15",
        )}
        title={groupLabel
          ? `Pick from contacts — fills ${groupLabel} name and address`
          : "Pick from contacts — fills name and address from your hub"}
        aria-label={groupLabel ? `Pick ${groupLabel} from contacts` : "Pick recipient from contacts"}
      >
        <UsersRound className="w-3.5 h-3.5 md:w-2.5 md:h-2.5" />
        <span className="md:hidden">{groupLabel ? `${groupLabel}` : "From contacts"}</span>
        <span className="hidden md:inline">{groupLabel || "contacts"}</span>
      </button>

      {open && (
        <div className="absolute z-30 right-0 top-full mt-1 w-72 rounded-lg border border-border bg-popover shadow-xl p-2 text-sm">
          <div className="relative mb-1.5">
            <Search className="w-3.5 h-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              ref={inputRef}
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search name, alias…"
              className="w-full h-7 pl-7 pr-7 bg-muted/60 rounded-md text-xs focus:outline-none focus:ring-1 focus:ring-ring/40"
            />
            {query && (
              <button
                onClick={() => setQuery("")}
                className="absolute right-1 top-1/2 -translate-y-1/2 p-0.5 text-muted-foreground hover:text-foreground"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>

          <div className="max-h-72 overflow-y-auto">
            {loading && (
              <div className="flex items-center justify-center py-4 text-muted-foreground">
                <Loader2 className="w-3 h-3 animate-spin" />
              </div>
            )}
            {!loading && results.length === 0 && (
              <div className="text-center py-3 text-[11px] text-muted-foreground italic">
                {query ? "No matches." : "No active contacts yet — add some in /r/contacts."}
              </div>
            )}
            {!loading && results.map(c => {
              const addr = formatPrimaryAddress(c);
              const hasEmail = (c.channels || []).some(ch =>
                (ch.kind || "").toLowerCase() === "email" && (ch.value || "").trim() !== "");
              const hasPhone = (c.channels || []).some(ch =>
                (ch.kind || "").toLowerCase() === "phone" && (ch.value || "").trim() !== "");
              const hasAddress = addr !== "";
              return (
                <button
                  key={c.id}
                  onClick={() => pick(c)}
                  className="w-full text-left rounded-md px-2 py-1.5 hover:bg-muted/60 transition flex gap-2 items-start"
                >
                  <div className={cn(
                    "w-6 h-6 shrink-0 rounded-full flex items-center justify-center text-[10px] font-semibold mt-0.5",
                    c.kind === "business" ? "bg-blue-500/15 text-blue-500" : "bg-amber-500/15 text-amber-500",
                  )}>
                    {c.kind === "business"
                      ? <Briefcase className="w-3 h-3" />
                      : c.display_name.trim().split(/\s+/).slice(0, 2).map(s => s[0]?.toUpperCase() || "").join("")}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <div className="text-xs font-medium truncate flex-1">{c.display_name}</div>
                      {(hasEmail || hasPhone || hasAddress) && (
                        <div className="inline-flex items-center gap-1 shrink-0 text-muted-foreground/70">
                          {hasEmail   && <Mail   className="w-3 h-3" aria-label="has email"   />}
                          {hasPhone   && <Phone  className="w-3 h-3" aria-label="has phone"   />}
                          {hasAddress && <MapPin className="w-3 h-3" aria-label="has address" />}
                        </div>
                      )}
                    </div>
                    {addr ? (
                      <div className="text-[10px] text-muted-foreground leading-tight whitespace-pre-line line-clamp-2">{addr}</div>
                    ) : (
                      <div className="text-[10px] text-muted-foreground italic">no address on file</div>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
