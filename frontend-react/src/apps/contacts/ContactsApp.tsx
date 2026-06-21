/**
 * Contacts app — identity hub.
 *
 * Three tabs: Active (default), Pending (auto-captured from email /
 * whatsapp, awaiting confirmation), Spam (known-bad — kept indexed so
 * future inbound from these channels is silently dropped). Counts on
 * the Pending / Spam tabs come from /api/contacts/_counts.
 *
 * Layout: two-pane on desktop (list ‖ detail), single-pane on mobile
 * (list collapses to selected detail). Person vs business toggles
 * which form fields show (legal name / tax id / IBAN only on business).
 *
 * The LLM uses the matching skills (add_contact, update_contact, …) for
 * its writes — those carry undo + confirm-modal. The UI talks straight
 * to /api/contacts/* because the user is in the form making the change
 * with intent.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { createPortal } from "react-dom";
import {
  UsersRound, Search, Plus, Trash2, Loader2, X, Check, Mail, Phone,
  MessageCircle, Globe, MapPin, Star, ShieldAlert, ChevronRight, Clock,
  Pencil, Phone as PhoneIcon, Cake, ExternalLink, MessageSquare,
  CalendarDays, Pin as PinIcon, PinOff,
  Briefcase, User as UserIcon, FileText, Send, Upload,
  Wand2, StopCircle, ChevronDown, AlertTriangle, Sparkles,
} from "lucide-react";
import { VcardImportModal } from "@/components/VcardImportModal";
import { useNavigate, useSearchParams } from "react-router-dom";
import { cn } from "@/lib/utils";
import { api, ApiError } from "@/lib/api";
import { Dock } from "@/components/Dock";
import { useAuth } from "@/components/AuthGate";
import type {
  Contact, ContactStatus, ContactChannel, ContactAddress, ChannelKind,
  AddressKind, ContactKind, StatusCounts,
  ContactTimeline, ContactTimelineItem,
} from "./types";
import { useApi } from "@/lib/useApi";

type Tab = "active" | "pending" | "spam";

const CHANNEL_ICONS: Record<ChannelKind, ReactElement> = {
  email:    <Mail className="w-3.5 h-3.5" />,
  phone:    <Phone className="w-3.5 h-3.5" />,
  whatsapp: <MessageCircle className="w-3.5 h-3.5" />,
  signal:   <MessageCircle className="w-3.5 h-3.5" />,
  telegram: <MessageCircle className="w-3.5 h-3.5" />,
  sms:      <MessageCircle className="w-3.5 h-3.5" />,
  website:  <Globe className="w-3.5 h-3.5" />,
  social:   <Globe className="w-3.5 h-3.5" />,
};

export function ContactsApp() {
  const [tab, setTab] = useState<Tab>("active");
  // Bumped whenever something OTHER than AutoClassifyButton kicks the
  // auto-classify pass — currently the "Re-review archived" button
  // chains into classify after moving rows back to pending. The bump
  // wakes AutoClassifyButton's status poll so its inline progress bar
  // lights up without the user having to navigate away and back.
  const [classifyKickCount, setClassifyKickCount] = useState(0);
  // Lifted up from TriageButton so AutoClassifyButton can auto-open
  // the modal on its running → done transition. Without this, the
  // user sees the progress bar fill, the page silently refreshes,
  // and nothing happens — they have to find and click Triage
  // themselves to see the LLM verdicts they just paid 2-5 min of
  // GPU time for.
  const [triageModalOpen, setTriageModalOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [counts, setCounts] = useState<StatusCounts>({ active: 0, pending: 0, spam: 0, archived: 0, pending_unclassified: 0, pending_classified: 0 });
  const [loading, setLoading] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  // Fallback for the case where a deep-linked id (e.g. clicking "Works at"
  // on a pending person whose employer is a pending business beyond the
  // list's first-200 page) isn't in the loaded list. The deep-link handler
  // populates this so the detail pane has something to render.
  const [deepLinked, setDeepLinked] = useState<Contact | null>(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  // Detail pane defaults to READ-mode now — Edit button flips it. Resets
  // on selection change so picking a different contact never lands
  // mid-edit.
  const [editing, setEditing] = useState(false);
  useEffect(() => { setEditing(false); }, [selectedId, creating]);

  // (togglePin defined below — needs `refresh` from useCallback above.)

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      // Ask for the backend's max so the whole address book renders.
      // The backend caps at 500; if your contacts grow past that we'll
      // need proper pagination instead of just bumping this number.
      const params = new URLSearchParams({ status: tab, limit: "500" });
      if (query.trim()) params.set("q", query.trim());
      const [list, c] = await Promise.all([
        api.get<Contact[]>(`/api/contacts?${params}`),
        api.get<StatusCounts>("/api/contacts/_counts"),
      ]);
      setContacts(list);
      setCounts(c);
    } catch (e: any) {
      console.error("contacts: load failed", e);
    } finally {
      setLoading(false);
    }
  }, [tab, query]);

  useEffect(() => { refresh(); }, [refresh]);

  // Deep-link: /contacts?contact=ID — used by chat's ContactsFoundCard.
  // Fetch the contact once to learn its status (active / pending / spam),
  // switch to the right tab so it's actually in the list, then select it.
  // Strip the query param after handling so a tab switch later doesn't
  // get fought by a stale URL.
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const raw = searchParams.get("contact");
    if (!raw) return;
    const id = Number(raw);
    if (!Number.isFinite(id) || id <= 0) return;
    (async () => {
      try {
        const c = await api.get<Contact>(`/api/contacts/${id}`);
        const targetTab: Tab =
          c.status === "pending" ? "pending"
          : c.status === "spam"  ? "spam"
          : "active";
        setTab(targetTab);
        setSelectedId(id);
        setDeepLinked(c);
      } catch (e) {
        console.error("contacts: deep-link load failed", e);
      } finally {
        const next = new URLSearchParams(searchParams);
        next.delete("contact");
        setSearchParams(next, { replace: true });
      }
    })();
    // Run once per ?contact= value; useSearchParams gives a fresh ref
    // each render so guard by the raw value.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams.get("contact")]);

  // Deep-link from the notification bell: /r/contacts?triage=open
  // jumps the user to Pending and pops the TriageModal so they can
  // review the LLM verdicts the background classify-pass produced.
  // Strip the param after opening so a refresh later doesn't keep
  // re-opening it.
  useEffect(() => {
    if (searchParams.get("triage") !== "open") return;
    setTab("pending");
    setTriageModalOpen(true);
    const next = new URLSearchParams(searchParams);
    next.delete("triage");
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams.get("triage")]);

  const selected = useMemo(
    () =>
      contacts.find(c => c.id === selectedId)
      || (deepLinked && deepLinked.id === selectedId ? deepLinked : null),
    [contacts, selectedId, deepLinked],
  );

  // employer_contact_id → display_name lookup for ContactListRow's
  // "at <business>" subline. Built from the already-loaded list so
  // there's no extra round-trip; if a person's employer happens to
  // sit on a different tab (e.g. pending while we view active) the
  // subline silently falls back to nothing — acceptable for v1.
  const employerNameById = useMemo(() => {
    const m = new Map<number, string>();
    for (const c of contacts) {
      if (c.kind === "business") m.set(c.id, c.display_name);
    }
    return m;
  }, [contacts]);

  const onSaved = useCallback(async (c: Contact) => {
    setSelectedId(c.id);
    await refresh();
  }, [refresh]);

  const onDeleted = useCallback(async () => {
    setSelectedId(null);
    await refresh();
  }, [refresh]);

  const togglePin = useCallback(async (c: Contact) => {
    try {
      await api.post(`/api/contacts/${c.id}/pin`, { pinned: !c.pinned });
      // Optimistic local toggle so the row reorders immediately; the
      // refetch underneath confirms.
      setContacts(prev => prev.map(x => x.id === c.id ? { ...x, pinned: !c.pinned } : x));
      void refresh();
    } catch (err: any) {
      alert(`Pin failed: ${err?.message || err}`);
    }
  }, [refresh]);

  // Drill-down state for mobile: when the user has tapped a row OR
  // hit "New contact", we hide the list and show the detail full-
  // width. Desktop still renders both panes side-by-side, so this
  // boolean only matters at < lg.
  const mobileDetailOpen = creating || selectedId !== null;
  const closeMobileDetail = () => { setSelectedId(null); setCreating(false); };

  return (
    <div className="min-h-screen flex flex-col bg-background">
      <main
        className="flex-1 flex flex-col max-w-7xl mx-auto w-full px-4 sm:px-8 pt-5 md:pt-10 pb-[max(8rem,calc(env(safe-area-inset-bottom)+6rem))] md:pb-32"
      >
        <header className="mb-6">
          <div className="flex items-center gap-3 mb-2">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-amber-500/30 to-rose-500/30 flex items-center justify-center shadow-md">
              <UsersRound className="w-5 h-5 text-amber-500" />
            </div>
            <span className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">Yorik · contacts</span>
          </div>
          <h1 className="text-xl sm:text-3xl font-semibold leading-tight">
            {counts.active} contact{counts.active !== 1 ? "s" : ""}
            {counts.pending > 0 && (
              <span className="text-amber-500 text-base font-normal ml-3">
                · {counts.pending} pending
              </span>
            )}
          </h1>
          {/* Subtitle hidden on mobile — pushed the first contact below
              the fold on a 667px-tall phone for little gain. Active
              count above is the load-bearing info. */}
          <p className="hidden md:block text-sm text-muted-foreground mt-2">
            People and businesses Yorik knows about. Used by Compose, Email and the agent
            to look up addresses and phone numbers instead of guessing.
          </p>
        </header>

        {/* Tabs — hidden on mobile when drilled into a detail
            (drill-down focus). Desktop side-by-side keeps tabs
            always visible since the list is always visible too. */}
        <div className={cn(
          "flex flex-wrap gap-2 mb-4 items-center",
          mobileDetailOpen && "hidden lg:flex",
        )}>
          {(["active", "pending", "spam"] as Tab[]).map(t => (
            <button
              key={t}
              onClick={() => { setTab(t); setSelectedId(null); }}
              className={cn(
                "text-xs px-3 py-1.5 rounded-full border transition flex items-center gap-1.5",
                tab === t
                  ? "bg-primary text-primary-foreground border-primary"
                  : "bg-card border-border text-muted-foreground hover:text-foreground",
              )}
            >
              {t === "active"  && `Active · ${counts.active}`}
              {t === "pending" && (
                <>
                  Pending · {counts.pending}
                  {counts.pending > 0 && tab !== "pending" && (
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                  )}
                </>
              )}
              {t === "spam"    && <><ShieldAlert className="w-3 h-3" />Spam · {counts.spam}</>}
            </button>
          ))}

          <div className="flex-1" />

          {/* Import + New-contact buttons hidden on mobile — the tab
              row was wrapping to 2-3 rows on a phone. Mobile gets a
              FAB (below) for New contact, the most common action.
              Import stays one-tap-deeper inside the FAB-adjacent
              ⋯ menu (deferred for v2; for now mobile users can
              still import via the settings page). */}
          <EnrichButton />
          <ExtractButton />
          <CrosslinkMailboxButton onDone={refresh} />
          <YorikAssistBulkButton tab={tab} counts={counts} onDone={refresh} />
          <CleanupPipeline
            tab={tab}
            counts={counts}
            classifyKickCount={classifyKickCount}
            setClassifyKickCount={setClassifyKickCount}
            onOpenTriage={() => setTriageModalOpen(true)}
            onRefresh={refresh}
          />
          {tab === "pending" && counts.archived > 0 && (
            <ReclassifyArchivedButton
              archivedCount={counts.archived}
              onDone={async () => {
                await refresh();
                // Tell AutoClassifyButton to re-poll its status so the
                // inline progress bar lights up for the freshly-kicked
                // classify run.
                setClassifyKickCount(k => k + 1);
              }}
            />
          )}
          <button
            onClick={() => { setImporting(true); }}
            className="hidden md:flex text-xs h-8 px-3 rounded-md bg-card border border-border text-foreground hover:bg-muted items-center gap-1.5"
            title="Import contacts from a .vcf file"
          >
            <Upload className="w-3.5 h-3.5" /> Import .vcf
          </button>
          <button
            onClick={() => { setCreating(true); setSelectedId(null); }}
            className="hidden md:flex text-xs h-8 px-3 rounded-md bg-primary text-primary-foreground hover:opacity-90 items-center gap-1.5"
          >
            <Plus className="w-3.5 h-3.5" /> New contact
          </button>
        </div>

        {/* Search — hidden on mobile when a detail/editor is open
            (drill-down mode), since the list it filters is also
            hidden. Desktop keeps it always visible. */}
        <div className={cn("mb-4 relative", mobileDetailOpen && "hidden lg:block")}>
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            // Short placeholder fits a 375px viewport without clipping;
            // aria-label keeps the longer hint for assistive tech.
            placeholder="Search contacts…"
            aria-label='Search by name, alias, or notes — "Grandma", "Example LLC", "plumber"'
            className="w-full h-11 md:h-10 pl-10 pr-3 bg-card border border-border rounded-xl text-sm focus:outline-none focus:ring-1 focus:ring-ring/40"
          />
        </div>

        {/* Two-pane layout. Mobile: drill-down — list visible by
            default, detail visible when a contact is selected or the
            user is creating one. Desktop (lg:+): both panes side-by-
            side as before. */}
        <div className="grid grid-cols-1 lg:grid-cols-[minmax(260px,380px)_1fr] gap-4 flex-1">
          {/* List — hidden on mobile while drilled into a detail. The
              max-h-[70vh] cap is dropped on mobile so the list fills
              the viewport when it's the only visible pane. */}
          <div className={cn(
            "flex flex-col gap-1 lg:max-h-[70vh] overflow-y-auto rounded-xl border border-border bg-card/40 p-1.5",
            mobileDetailOpen && "hidden lg:flex",
          )}>
            {loading && contacts.length === 0 && (
              <div className="flex items-center justify-center py-12 text-muted-foreground text-sm">
                <Loader2 className="w-4 h-4 animate-spin mr-2" /> Loading…
              </div>
            )}
            {!loading && contacts.length === 0 && (
              <div className="text-center py-12 text-muted-foreground text-sm italic">
                {tab === "active"  && (query ? "No matches." : "No contacts yet. Add one with the button above.")}
                {tab === "pending" && "Nothing pending — Yorik will park new contacts here when they auto-arrive from email/WhatsApp."}
                {tab === "spam"    && "No contacts marked as spam."}
              </div>
            )}
            {(() => {
              // Bucket the list into Pinned / Recent (last_interaction_at
              // within ~30d) / The rest. Pinned bubbles to the top
              // regardless of recency; Recent surfaces the 8 warmest
              // below that. The default "Contacts" bucket holds
              // everything else. Search/pending/spam tabs collapse to
              // a single bucket because grouping there is noise.
              if (query.trim() || tab !== "active") {
                return contacts.map(c => (
                  <ContactListRow
                    key={c.id}
                    contact={c}
                    employerName={c.employer_contact_id ? employerNameById.get(c.employer_contact_id) : undefined}
                    selected={selectedId === c.id && !creating}
                    onClick={() => { setSelectedId(c.id); setCreating(false); }}
                    onTogglePin={() => togglePin(c)}
                    onAction={(kind) => {
                      if (kind === "needs-channel") {
                        setSelectedId(c.id); setCreating(false);
                      }
                    }}
                    // Inline delete only on Pending — that's the triage
                    // list where the user is sifting through extracted
                    // rows. Active/spam delete still goes through
                    // ContactView's fuller flow.
                    onDelete={tab === "pending" ? async () => {
                      try {
                        await api.delete(`/api/contacts/${c.id}`);
                        if (selectedId === c.id) setSelectedId(null);
                        await refresh();
                      } catch (err: any) {
                        alert(`Delete failed: ${err?.message || err}`);
                      }
                    } : undefined}
                  />
                ));
              }
              const groups = groupContacts(contacts);
              return groups.map(([label, rows]) => (
                <div key={label} className="mb-2">
                  <div className="px-2 pt-2 pb-1 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold flex items-center gap-1.5">
                    {label === "Pinned" && <Star className="w-2.5 h-2.5 text-rose-500" />}
                    {label === "Recent" && <Clock className="w-2.5 h-2.5" />}
                    {label}
                    <span className="opacity-60">· {rows.length}</span>
                  </div>
                  {rows.map(c => (
                    <ContactListRow
                      key={c.id}
                      contact={c}
                      employerName={c.employer_contact_id ? employerNameById.get(c.employer_contact_id) : undefined}
                      selected={selectedId === c.id && !creating}
                      onClick={() => { setSelectedId(c.id); setCreating(false); }}
                      onTogglePin={() => togglePin(c)}
                      onAction={(kind) => {
                        if (kind === "needs-channel") {
                          setSelectedId(c.id); setCreating(false);
                        }
                      }}
                    />
                  ))}
                </div>
              ));
            })()}
          </div>

          {/* Detail — read-mode card by default, switch to editor on
              Edit click (or auto when creating). Massively simpler
              than dropping the user straight into a 10-field form.
              Mobile: hidden unless user has tapped a contact or hit
              "New" — drill-down. Desktop: always visible. */}
          <div className={cn(
            "rounded-xl border border-border bg-card/40 p-4 lg:p-6 min-h-[400px]",
            !mobileDetailOpen && "hidden lg:block",
          )}>
            {/* Back-to-list — only on mobile. The list pane is
                hidden underneath while drilled in, so without this
                there's no way back to browse. */}
            {mobileDetailOpen && (
              <button
                type="button"
                onClick={closeMobileDetail}
                className="lg:hidden mb-3 -ml-1 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
                aria-label="Back to contact list"
              >
                <ChevronRight className="w-4 h-4 rotate-180" />
                Back to contacts
              </button>
            )}
            {creating ? (
              <ContactEditor
                contact={null}
                onSaved={(c) => { setCreating(false); onSaved(c); }}
                onCancel={() => setCreating(false)}
                onDeleted={() => {}}
              />
            ) : selected && editing ? (
              <ContactEditor
                key={selected.id}
                contact={selected}
                onSaved={(c) => { setEditing(false); onSaved(c); }}
                onCancel={() => setEditing(false)}
                onDeleted={onDeleted}
                onStatusChange={refresh}
              />
            ) : selected ? (
              <ContactView
                key={selected.id}
                contact={selected}
                onEdit={() => setEditing(true)}
                onTogglePin={() => togglePin(selected)}
              />
            ) : (
              <div className="h-full flex items-center justify-center text-sm text-muted-foreground italic">
                Select a contact to view or edit.
              </div>
            )}
          </div>
        </div>
      </main>

      {/* Mobile FAB — New contact. Bottom-LEFT (matches the calendar +
          email convention, leaves bottom-right for the VoiceFab).
          Hidden when drilled into a detail/editor (no point creating
          another contact while the current one is open). */}
      {!mobileDetailOpen && (
        <button
          type="button"
          onClick={() => { setCreating(true); setSelectedId(null); }}
          className="lg:hidden fixed left-4 bottom-[max(5.5rem,calc(env(safe-area-inset-bottom)+4.5rem))] z-30 w-14 h-14 rounded-full bg-primary text-primary-foreground shadow-lg flex items-center justify-center hover:opacity-90 active:scale-95 transition"
          aria-label="New contact"
          title="New contact"
        >
          <Plus className="w-6 h-6" strokeWidth={2.5} />
        </button>
      )}

      <Dock activeAppId="contacts" />
      {importing && (
        <VcardImportModal
          onClose={() => setImporting(false)}
          onApplied={async () => { setImporting(false); await refresh(); }}
        />
      )}
      {triageModalOpen && createPortal(
        <TriageModal
          onClose={() => setTriageModalOpen(false)}
          onApplied={async () => { await refresh(); }}
        />,
        document.body,
      )}
    </div>
  );
}



// Avatar that prefers the WhatsApp profile picture (via the bridge
// proxy at /api/whatsapp/avatar/<jid>) when the contact has a
// whatsapp channel. Falls back gracefully:
//   - no whatsapp channel → initials / icon (immediate)
//   - whatsapp channel but the fetch 404s → initials / icon
//   - whatsapp channel + fetch returns 200 → image
//
// The fetch is gated by IntersectionObserver: off-screen rows do NOT
// request their avatars. A long contact list with 100+ WhatsApp
// contacts thus produces ~20 initial requests (the visible window)
// instead of 100 simultaneous calls. Combined with the bridge's
// negative-cache + concurrency cap, this keeps Meta-traffic well
// under the rate-limit threshold even on a hot page load.
function ContactAvatar({
  contact, size, className,
}: {
  contact: Contact;
  size: "sm" | "lg";
  className?: string;
}) {
  const jid = (contact.channels || [])
    .find(ch => ch.kind === "whatsapp")?.value;

  // imgLoaded === null   → not tried yet
  // imgLoaded === true   → render the <img>
  // imgLoaded === false  → render the initials/icon fallback
  const [imgLoaded, setImgLoaded] = useState<boolean | null>(null);
  // hasBeenVisible flips true the first time this row scrolls within
  // ~200px of the viewport. Stays true after — we don't want avatars
  // unmounting and re-fetching when the user scrolls past.
  const [hasBeenVisible, setHasBeenVisible] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setImgLoaded(jid ? null : false);
  }, [jid]);

  useEffect(() => {
    if (!jid || hasBeenVisible) return;
    const el = wrapRef.current;
    if (!el) return;
    const obs = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          setHasBeenVisible(true);
          obs.disconnect();
          break;
        }
      }
    }, { rootMargin: "200px" });  // start fetching just before scroll-into-view
    obs.observe(el);
    return () => obs.disconnect();
  }, [jid, hasBeenVisible]);

  const dims = size === "lg"
    ? "w-14 h-14 text-base"
    : "w-8 h-8 text-xs";
  const tone = contact.kind === "business"
    ? "bg-blue-500/15 text-blue-500"
    : "bg-amber-500/15 text-amber-500";
  const iconSize = size === "lg" ? "w-6 h-6" : "w-3.5 h-3.5";

  // Bottom-right dot indicator when Yorik AI is enabled for this contact.
  // Small ring-card halo so the dot reads cleanly against any row
  // background (default, selected, pending, spam tints).
  const assistDotSize = size === "lg" ? "w-3.5 h-3.5" : "w-2.5 h-2.5";
  return (
    <div className={cn("relative shrink-0", className)}>
      <div ref={wrapRef} className={cn(
        dims,
        "rounded-full flex items-center justify-center font-semibold overflow-hidden",
        imgLoaded !== true && tone,
      )}>
        {imgLoaded !== false && jid && hasBeenVisible && (
          <img
            src={`/api/whatsapp/avatar/${encodeURIComponent(jid)}`}
            alt=""
            onLoad={() => setImgLoaded(true)}
            onError={() => setImgLoaded(false)}
            className={cn(
              "w-full h-full object-cover",
              imgLoaded === true ? "block" : "hidden",
            )}
          />
        )}
        {imgLoaded !== true && (
          contact.kind === "business"
            ? <Briefcase className={iconSize} />
            : <span>{initials(bestContactName(contact))}</span>
        )}
      </div>
      {contact.yorik_assist_enabled && (
        <span
          className={cn(
            "absolute -bottom-0.5 -right-0.5 rounded-full bg-emerald-500 ring-2 ring-card",
            assistDotSize,
          )}
          title="Yorik assist enabled — new messages from this contact may surface suggestions"
          aria-label="Yorik assist enabled"
        />
      )}
    </div>
  );
}


// ───────────────────────── list row ─────────────────────────

function ContactListRow({
  contact, employerName, selected, onClick, onAction, onTogglePin, onDelete,
}: {
  contact: Contact;
  /** display_name of the linked business (mig 045). When set we render
   *  a small "at <business>" subline so the row signals the
   *  employer-employee relation without the user having to open the
   *  detail pane. Undefined when no employer is linked or the
   *  employer isn't in the current loaded list. */
  employerName?: string;
  selected: boolean;
  onClick: () => void;
  onAction: (kind: "letter" | "email" | "whatsapp" | "needs-channel") => void;
  /** Pin/unpin toggle. Pinned contacts bubble to the "★ Pinned"
   *  section at the top of the sidebar list. */
  onTogglePin: () => void;
  /** Hover-delete on pending rows so the user doesn't have to drill
   *  into the detail pane to clear an extracted contact they don't
   *  want. Hidden for active/spam — those go through ContactView's
   *  fuller flow. Undefined = button not rendered. */
  onDelete?: () => void;
}) {
  const primaryChannel = contact.channels[0];
  const emailCh = contact.channels.find(c => c.kind === "email");
  const waCh    = contact.channels.find(c => c.kind === "whatsapp");
  // Letter is always available — Compose handles "no address" gracefully
  // (it just leaves the recipient block partial and the user can fill it).
  const navigate = useNavigate();

  function go(e: React.MouseEvent, kind: "letter" | "email" | "whatsapp") {
    e.stopPropagation();
    if (kind === "letter") {
      navigate(`/compose?contact_id=${contact.id}`);
      return;
    }
    if (kind === "email") {
      if (!emailCh) { onAction("needs-channel"); return; }
      navigate(`/email?to=${encodeURIComponent(emailCh.value)}`);
      return;
    }
    if (kind === "whatsapp") {
      if (!waCh) { onAction("needs-channel"); return; }
      // Channel value is now the FULL jid (including @s.whatsapp.net or
      // @lid). Older rows that store just digits get the suffix appended
      // here as a defensive fallback so legacy data still routes.
      const jid = waCh.value.includes("@") ? waCh.value : `${waCh.value}@s.whatsapp.net`;
      navigate(`/whatsapp?chat=${encodeURIComponent(jid)}`);
      return;
    }
  }

  return (
    <div
      onClick={onClick}
      className={cn(
        "w-full text-left rounded-lg px-3 py-2 flex items-center gap-3 transition cursor-pointer group",
        selected ? "bg-primary/10 border border-primary/30" : "hover:bg-muted/40 border border-transparent",
      )}
    >
      <ContactAvatar contact={contact} size="sm" />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium truncate flex items-center gap-1.5">
          {bestContactName(contact)}
          {contact.status === "pending" && (
            <span className="text-[9px] uppercase tracking-wider px-1 rounded bg-amber-500/15 text-amber-500">pending</span>
          )}
        </div>
        <div className="text-[11px] text-muted-foreground truncate flex items-center gap-1.5">
          {employerName && (
            <span className="inline-flex items-center gap-1 text-blue-500/80">
              <Briefcase className="w-2.5 h-2.5" />
              at {employerName}
            </span>
          )}
          {contact.relation && (
            <>
              {employerName && <span className="opacity-40">·</span>}
              <span>{contact.relation}</span>
            </>
          )}
          {primaryChannel && (
            <>
              {(employerName || contact.relation) && <span className="opacity-40">·</span>}
              <span className="opacity-60">{primaryChannel.value}</span>
            </>
          )}
        </div>
      </div>

      {/* Action icons — quick-jump to Compose / Email / WhatsApp. The
          icons stay visible on the selected row but only fade in on hover
          otherwise, to keep the list calm. Letter is always enabled;
          Email/WA grey out when that channel isn't on the contact (click
          still opens the editor so the user can add it). */}
      {/* Quick-action icons: mobile always-visible (no hover state on
          touch — invisible icons can't be discovered). Desktop keeps
          the hover-reveal pattern so the resting list stays calm. */}
      <div className={cn(
        "flex items-center gap-0.5 shrink-0 transition-opacity",
        selected ? "opacity-100" : "opacity-100 md:opacity-0 md:group-hover:opacity-100",
      )}>
        <RowActionIcon
          icon={contact.pinned ? <PinOff className="w-3.5 h-3.5" /> : <PinIcon className="w-3.5 h-3.5" />}
          title={contact.pinned ? "Unpin from top" : "Pin to top"}
          onClick={(e) => { e.stopPropagation(); onTogglePin(); }}
          enabled
          tone={contact.pinned ? "rose" : undefined}
        />
        <RowActionIcon
          icon={<FileText className="w-3.5 h-3.5" />}
          title="Write a letter to this contact"
          onClick={(e) => go(e, "letter")}
          enabled
          tone="violet"
        />
        <RowActionIcon
          icon={<Mail className="w-3.5 h-3.5" />}
          title={emailCh ? `Email ${emailCh.value}` : "No email on file — click to add one"}
          onClick={(e) => go(e, "email")}
          enabled={!!emailCh}
          tone="blue"
        />
        <RowActionIcon
          icon={<MessageCircle className="w-3.5 h-3.5" />}
          title={waCh ? `Open WhatsApp chat with ${contact.display_name}` : "No WhatsApp on file — click to add one"}
          onClick={(e) => go(e, "whatsapp")}
          enabled={!!waCh}
          tone="emerald"
        />
        {onDelete && (
          <RowActionIcon
            icon={<Trash2 className="w-3.5 h-3.5" />}
            title="Delete this contact (asks for confirmation)"
            onClick={(e) => {
              e.stopPropagation();
              if (confirm(`Delete "${contact.display_name}"? This can't be undone.`)) {
                onDelete();
              }
            }}
            enabled
            tone="rose"
          />
        )}
      </div>

      {contact.pinned && (
        <PinIcon className="w-3 h-3 text-rose-500 shrink-0" />
      )}
      <ChevronRight className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
    </div>
  );
}

function RowActionIcon({ icon, title, onClick, enabled, tone }: {
  icon: React.ReactNode;
  title: string;
  onClick: (e: React.MouseEvent) => void;
  enabled: boolean;
  tone?: "violet" | "blue" | "emerald" | "rose";
}) {
  const toneCls = enabled ? ({
    violet:  "hover:bg-violet-500/15 hover:text-violet-500",
    blue:    "hover:bg-blue-500/15 hover:text-blue-500",
    emerald: "hover:bg-emerald-500/15 hover:text-emerald-500",
    rose:    "bg-rose-500/15 text-rose-500 hover:bg-rose-500/25",
  } as const)[tone || "violet"] : "opacity-40 hover:bg-muted/50";
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn(
        "p-1.5 rounded-md text-muted-foreground transition",
        toneCls,
      )}
    >
      {icon}
    </button>
  );
}

function initials(name: string): string {
  return name.trim().split(/\s+/).slice(0, 2).map(s => s[0]?.toUpperCase() || "").join("") || "?";
}

// Modality preference for falling back to channel.display_name when
// contact.display_name is still the JID-prefix fallback (purely digits).
// WhatsApp wins because it's the most likely modality to carry a
// human pushName. Adding Telegram/Signal later = append here.
const CHANNEL_NAME_PRIORITY: ReadonlyArray<string> = [
  "whatsapp", "telegram", "signal", "sms", "email",
];

// Format a WhatsApp JID as a readable phone-style label. Strips the
// @s.whatsapp.net suffix and prepends a + so it looks like the number
// it represents. @lid stays as a short fingerprint — there's no
// phone number to recover. Used for the last-resort fallback when
// no name is available anywhere.
function formatJidForDisplay(jid: string): string {
  if (!jid) return "";
  if (jid.endsWith("@s.whatsapp.net")) {
    const digits = jid.slice(0, jid.length - "@s.whatsapp.net".length);
    return digits ? `+${digits}` : jid;
  }
  if (jid.endsWith("@lid")) {
    const digits = jid.slice(0, jid.length - "@lid".length);
    return `WhatsApp #${digits.slice(-6)}`;
  }
  return jid;
}

// Best display name for a contact, respecting precedence:
//   1. contact.display_name if it's a real human-readable name
//      (not empty, not purely digits — which is our JID-fallback)
//   2. The display_name from the first channel that has one, ordered
//      by CHANNEL_NAME_PRIORITY
//   3. The formatted value of the first channel (e.g. +49 1234)
//   4. Empty fallback "?"
function bestContactName(contact: Contact): string {
  const d = (contact.display_name || "").trim();
  if (d && !/^[0-9]+$/.test(d)) return d;
  const chans = contact.channels || [];
  for (const kind of CHANNEL_NAME_PRIORITY) {
    const ch = chans.find(c => c.kind === kind && (c.display_name || "").trim());
    if (ch) return (ch.display_name || "").trim();
  }
  // Last resort: format the JID/value of the first channel.
  if (chans.length > 0) return formatJidForDisplay(chans[0].value);
  // Truly nothing — fall back to whatever display_name held (probably
  // the numeric JID-prefix), or a placeholder.
  return d || "?";
}

/** Parse "Paperless doc #1234" tokens in extraction notes and render
 *  them as links into the Documents app. The notes come out of
 *  contact_extractor.py:write_pending_contact_from_doc as a plain
 *  sentence so the previous version (just text) was readable but the
 *  user couldn't jump to the source doc to verify the extraction. */
function renderNotesWithDocLinks(notes: string): React.ReactNode {
  // Matches "doc #123" or "doc#123" or "Doc #123". The leading word
  // is captured loosely so "Paperless doc #1234" AND "Possible match:
  // contact #5" can be distinguished — only doc-prefixed ones get
  // linked.
  const re = /(\b[Dd]oc\s*#\s*(\d+)\b)/g;
  const parts: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(notes)) !== null) {
    if (m.index > last) parts.push(notes.slice(last, m.index));
    const docId = m[2];
    parts.push(
      <a
        key={`${m.index}-${docId}`}
        href={`/r/documents?doc=${encodeURIComponent(docId)}&source=paperless`}
        className="text-violet-500 hover:underline"
        title="Open the source document"
      >
        {m[1]}
      </a>
    );
    last = m.index + m[0].length;
  }
  if (last < notes.length) parts.push(notes.slice(last));
  return parts.length ? parts : notes;
}


/** Bucket contacts into Pinned / Recent (warm in the last ~30 days) /
 *  Contacts (the rest, alphabetical). Empty buckets are dropped so
 *  the sidebar doesn't show headers with nothing under them. */
function groupContacts(
  list: Contact[],
): Array<[string, Contact[]]> {
  const pinned: Contact[] = [];
  const recent: Contact[] = [];
  const rest:   Contact[] = [];

  const RECENT_CUTOFF_MS = 30 * 24 * 60 * 60 * 1000;
  const now = Date.now();
  for (const c of list) {
    if (c.pinned) { pinned.push(c); continue; }
    const t = c.last_interaction_at || c.last_used_at;
    if (t) {
      const d = new Date(t).getTime();
      if (!Number.isNaN(d) && now - d <= RECENT_CUTOFF_MS) {
        recent.push(c);
        continue;
      }
    }
    rest.push(c);
  }

  // Cap "Recent" to the warmest 8 — the rest live in the main bucket
  // so the section header stays readable.
  const RECENT_CAP = 8;
  const recentSorted = recent.sort((a, b) => {
    const ta = a.last_interaction_at || a.last_used_at || "";
    const tb = b.last_interaction_at || b.last_used_at || "";
    return tb < ta ? -1 : tb > ta ? 1 : a.display_name.localeCompare(b.display_name);
  });
  const recentTop = recentSorted.slice(0, RECENT_CAP);
  const recentOverflow = recentSorted.slice(RECENT_CAP);

  const allRest = [...rest, ...recentOverflow].sort(
    (a, b) => a.display_name.localeCompare(b.display_name),
  );

  const out: Array<[string, Contact[]]> = [];
  if (pinned.length)    out.push(["Pinned", pinned]);
  if (recentTop.length) out.push(["Recent", recentTop]);
  if (allRest.length)   out.push(["Contacts", allRest]);
  return out;
}


// ───────────────────────── view (read-mode) ─────────────────────────
//
// Default surface when a contact is selected. Replaces the "always
// drop the user straight into a 10-field edit form" pattern with a
// clean read-mode card: big channel buttons (the verbs the user is
// actually here for), an activity timeline (last emails / events /
// drafts with this contact), and a compact details block. The
// `[Edit]` button flips to ContactEditor when the user wants to
// change something.

// Per-contact "Yorik assist" toggle — flips yorik_assist_enabled on
// the contact row. When OFF (default), the suggestion engine skips
// any message from this contact entirely, never sending body text
// to the LLM. This is the contact-level privacy gate from the
// 3-layer hierarchy (master → source → contact).
function YorikAssistRow({ contact }: { contact: Contact }) {
  const [enabled, setEnabled] = useState<boolean>(!!contact.yorik_assist_enabled);
  const [saving, setSaving] = useState(false);

  // Reset when the user switches contacts.
  useEffect(() => { setEnabled(!!contact.yorik_assist_enabled); }, [contact.id, contact.yorik_assist_enabled]);

  async function toggle() {
    const next = !enabled;
    setSaving(true);
    setEnabled(next);  // optimistic
    try {
      await api.patch(`/api/contacts/${contact.id}`, { yorik_assist_enabled: next });
    } catch (e: any) {
      setEnabled(!next);
      alert(e?.message || "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-card/40 px-3 py-2">
      <div className="text-xs">
        <div className="font-medium">Yorik assist</div>
        <div className="text-muted-foreground">
          Let Yorik analyse messages from this contact and suggest one-click actions.
        </div>
      </div>
      <button
        onClick={toggle}
        disabled={saving}
        className={cn(
          "shrink-0 relative inline-flex h-5 w-9 items-center rounded-full transition",
          enabled ? "bg-violet-500" : "bg-muted",
          saving && "opacity-60 cursor-wait",
        )}
        aria-pressed={enabled}
        title={enabled ? "Disable Yorik assist for this contact" : "Enable Yorik assist for this contact"}
      >
        <span className={cn(
          "inline-block h-3.5 w-3.5 transform rounded-full bg-white transition",
          enabled ? "translate-x-5" : "translate-x-1",
        )} />
      </button>
    </div>
  );
}

function ContactView({
  contact, onEdit, onTogglePin,
}: {
  contact: Contact;
  onEdit: () => void;
  onTogglePin: () => void;
}) {
  const navigate = useNavigate();
  const timelineApi = useApi<ContactTimeline>(`/api/contacts/${contact.id}/timeline?limit=8`, [contact.id]);
  const timeline = timelineApi.data;

  // Employer / employees linkage (mig 045). A person with an
  // employer_contact_id fetches their employer's row so we can show
  // "At ExampleCo Boutique" + click-through. A business fetches the
  // list of its linked employees so we can render "People here". Both
  // endpoints return [] / 404 cleanly when there's no linkage; we just
  // hide the section in that case.
  const employerApi = useApi<Contact>(
    contact.kind === "person" && contact.employer_contact_id
      ? `/api/contacts/${contact.employer_contact_id}`
      : null,
    [contact.employer_contact_id],
  );
  const employer = employerApi.data;

  const employeesApi = useApi<Contact[]>(
    contact.kind === "business"
      ? `/api/contacts/${contact.id}/employees`
      : null,
    [contact.id],
  );
  const employees = employeesApi.data || [];

  const email   = contact.channels.find(c => c.kind === "email");
  const phone   = contact.channels.find(c => c.kind === "phone");
  const wa      = contact.channels.find(c => c.kind === "whatsapp");
  const website = contact.channels.find(c => c.kind === "website");

  return (
    <div className="space-y-5">
      {/* Header — avatar + name + identity chips + pin/edit */}
      <header className="flex items-start gap-4">
        <ContactAvatar contact={contact} size="lg" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-xl font-semibold leading-tight truncate">
              {bestContactName(contact)}
            </h2>
            {contact.pinned && (
              <PinIcon className="w-3.5 h-3.5 text-rose-500" />
            )}
            {contact.status === "pending" && (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-500 font-medium">pending</span>
            )}
            {contact.status === "spam" && (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-red-500/15 text-red-500 font-medium flex items-center gap-1">
                <ShieldAlert className="w-3 h-3" />spam
              </span>
            )}
          </div>
          <div className="text-xs text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
            <span>{contact.kind === "business" ? "Business" : "Person"}</span>
            {contact.role && (
              <><span className="opacity-40">·</span><span>{contact.role}</span></>
            )}
            {contact.relation && <><span className="opacity-40">·</span><span>{contact.relation}</span></>}
            {contact.aliases.length > 0 && (
              <><span className="opacity-40">·</span><span className="italic opacity-80">{contact.aliases.join(", ")}</span></>
            )}
            {contact.birthday && (
              <><span className="opacity-40">·</span>
                <span className="inline-flex items-center gap-1"><Cake className="w-3 h-3" />{contact.birthday}</span>
              </>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <button
            onClick={onTogglePin}
            className={cn(
              "h-10 w-10 md:h-8 md:w-8 inline-flex items-center justify-center rounded-md border transition",
              contact.pinned
                ? "bg-rose-500/15 text-rose-500 border-rose-500/30 hover:bg-rose-500/20"
                : "border-border bg-card text-muted-foreground hover:text-foreground hover:bg-muted",
            )}
            title={contact.pinned ? "Unpin from top" : "Pin to top"}
            aria-label={contact.pinned ? "Unpin contact" : "Pin contact"}
          >
            {contact.pinned ? <PinOff className="w-4 h-4 md:w-3.5 md:h-3.5" /> : <PinIcon className="w-4 h-4 md:w-3.5 md:h-3.5" />}
          </button>
          <button
            onClick={onEdit}
            className="h-10 md:h-8 px-3 inline-flex items-center gap-1.5 rounded-md border border-border bg-card text-foreground hover:bg-muted text-xs"
            title="Edit contact details"
            aria-label="Edit contact"
          >
            <Pencil className="w-4 h-4 md:w-3.5 md:h-3.5" /> Edit
          </button>
        </div>
      </header>

      <YorikAssistRow contact={contact} />

      {/* Employer link — shown for kind="person" rows with a linked
          employer_contact_id (mig 045). Clicking jumps to the business
          contact so the user can see the address / IBAN / other people
          there without having to remember the company name and search
          for it. Hidden when there's no employer linkage. */}
      {employer && (
        <button
          type="button"
          onClick={() => navigate(`/contacts?contact=${employer.id}`)}
          className="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg border border-border bg-blue-500/5 hover:bg-blue-500/10 hover:border-blue-500/40 transition text-left"
          title={`Open ${employer.display_name}`}
        >
          <div className="w-8 h-8 rounded-full bg-blue-500/15 text-blue-500 flex items-center justify-center shrink-0">
            <Briefcase className="w-4 h-4" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground/80 font-semibold">
              Works at
            </div>
            <div className="text-sm font-medium truncate">
              {employer.display_name}
            </div>
          </div>
          <ChevronRight className="w-4 h-4 text-muted-foreground shrink-0" />
        </button>
      )}

      {/* Big channel buttons — the verbs the user actually came for. */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <BigActionBtn
          icon={<Mail className="w-4 h-4" />}
          label="Email"
          sub={email?.value}
          disabledHint={email ? undefined : "No email on file"}
          onClick={() => { if (email) navigate(`/email?to=${encodeURIComponent(email.value)}`); else onEdit(); }}
        />
        <BigActionBtn
          icon={<FileText className="w-4 h-4" />}
          label="Letter"
          sub={contact.addresses[0]?.city || "via Compose"}
          onClick={() => navigate(`/compose?contact_id=${contact.id}`)}
        />
        <BigActionBtn
          icon={<MessageCircle className="w-4 h-4" />}
          label="WhatsApp"
          sub={wa?.value}
          disabledHint={wa ? undefined : "No WhatsApp on file"}
          onClick={() => {
            if (!wa) { onEdit(); return; }
            const jid = wa.value.includes("@") ? wa.value : `${wa.value}@s.whatsapp.net`;
            navigate(`/whatsapp?chat=${encodeURIComponent(jid)}`);
          }}
        />
        <BigActionBtn
          icon={<PhoneIcon className="w-4 h-4" />}
          label="Call"
          sub={phone?.value}
          disabledHint={phone ? undefined : "No phone on file"}
          href={phone ? `tel:${phone.value}` : undefined}
          onClick={phone ? undefined : onEdit}
        />
      </div>

      {/* Activity timeline — what Yorik knows about this person.
          Loads lazily; collapses gracefully when there's nothing. */}
      <section>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2 flex items-center gap-1.5">
          <Clock className="w-2.5 h-2.5" /> Activity
          {timeline && timeline.total > 0 && (
            <span className="opacity-60">· {timeline.total}</span>
          )}
        </div>
        {timelineApi.loading && !timeline && (
          <div className="text-xs text-muted-foreground italic py-2">Loading…</div>
        )}
        {timeline && timeline.items.length === 0 && (
          <div className="text-xs text-muted-foreground italic py-2">
            No emails, events, or drafts with this contact yet.
          </div>
        )}
        {timeline && timeline.items.length > 0 && (
          <TimelineList items={timeline.items} />
        )}
      </section>

      {/* People at this business — only renders for kind='business'
          contacts that have at least one person row pointing at them
          via employer_contact_id. Empty list (or person-kind) hides
          the section entirely. */}
      {contact.kind === "business" && employees.length > 0 && (
        <section>
          <h3 className="text-[10px] uppercase tracking-wider text-muted-foreground/80 font-semibold mb-2">
            People here · {employees.length}
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {employees.map(p => (
              <button
                key={p.id}
                onClick={() => navigate(`/contacts?contact=${p.id}`)}
                className="text-left flex items-start gap-3 p-3 rounded-lg border border-border hover:border-primary/40 hover:bg-muted/30 transition"
              >
                <div className="w-9 h-9 rounded-full bg-amber-500/15 text-amber-500 flex items-center justify-center text-xs font-semibold shrink-0">
                  {initials(p.display_name)}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium truncate">
                    {p.display_name}
                  </div>
                  {p.role && (
                    <div className="text-[11px] text-muted-foreground truncate">
                      {p.role}
                    </div>
                  )}
                </div>
              </button>
            ))}
          </div>
        </section>
      )}

      {/* Compact details — channels, addresses, notes. No fields to
          edit here; click [Edit] above to change anything. */}
      <DetailsBlock contact={contact} />
    </div>
  );
}


function BigActionBtn({
  icon, label, sub, onClick, href, disabledHint,
}: {
  icon: React.ReactNode;
  label: string;
  sub?: string;
  onClick?: () => void;
  href?: string;
  disabledHint?: string;
}) {
  const tone = disabledHint
    ? "opacity-60 border-dashed"
    : "hover:border-primary/40 hover:shadow-sm";
  const inner = (
    <>
      <div className={cn(
        "w-8 h-8 rounded-md flex items-center justify-center shrink-0",
        disabledHint ? "bg-muted/40 text-muted-foreground" : "bg-primary/10 text-primary",
      )}>{icon}</div>
      <div className="text-left flex-1 min-w-0">
        <div className="text-sm font-medium leading-tight">{label}</div>
        {/* Sub-line hidden on mobile when empty (was "—" placeholder
            that added visual noise in already-cramped 165px cards).
            When present it shows on all sizes since it's actionable
            info (the actual email / phone / city). */}
        {(disabledHint || sub) && (
          <div className="text-[11px] text-muted-foreground truncate">
            {disabledHint || sub}
          </div>
        )}
      </div>
    </>
  );
  const cls = cn(
    "flex items-center gap-2.5 px-3 py-2 rounded-lg border border-border bg-card transition no-underline text-foreground",
    tone,
  );
  if (href) {
    return <a href={href} className={cls} title={label}>{inner}</a>;
  }
  return (
    <button type="button" onClick={onClick} className={cls} title={disabledHint || label}>
      {inner}
    </button>
  );
}


function TimelineList({ items }: { items: ContactTimelineItem[] }) {
  const navigate = useNavigate();
  const iconFor = (kind: ContactTimelineItem["kind"]) => {
    if (kind === "email") return <Mail className="w-3 h-3" />;
    if (kind === "event") return <CalendarDays className="w-3 h-3" />;
    return <FileText className="w-3 h-3" />;
  };
  return (
    <ol className="space-y-1.5">
      {items.map((it, i) => (
        <li
          key={i}
          onClick={() => navigate(it.link)}
          className="px-2.5 py-2 rounded-md border border-border bg-card hover:bg-muted/50 cursor-pointer transition flex items-start gap-2.5"
        >
          <div className="mt-0.5 w-5 h-5 rounded-md bg-muted text-muted-foreground flex items-center justify-center shrink-0">
            {iconFor(it.kind)}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-xs font-medium truncate flex items-center gap-1.5">
              {it.title}
              {it.direction === "outgoing" && (
                <span className="text-[9px] uppercase tracking-wider opacity-60">sent</span>
              )}
            </div>
            {it.sub && (
              <div className="text-[11px] text-muted-foreground truncate">{it.sub}</div>
            )}
          </div>
          <div className="text-[10px] text-muted-foreground tabular-nums shrink-0 mt-0.5">
            {fmtShortDate(it.when)}
          </div>
          <ExternalLink className="w-3 h-3 text-muted-foreground/60 shrink-0 mt-1" />
        </li>
      ))}
    </ol>
  );
}


/**
 * Header button that triggers the LLM contact-enrichment background
 * job and shows live progress while it runs. Polls the workers
 * heartbeat (which the enricher updates per-contact) every 2s and
 * flips into a "Stop · 142/520 · 87 proposals" pill mid-run. Stop
 * is cooperative — current contact finishes its LLM call, then the
 * walk exits cleanly.
 */
// Bulk "Enable Yorik assist" — scoped to the visible tab (active or
// pending). Saves the user from clicking 200+ row toggles. Hidden on
// the spam tab; nobody wants AI suggestions for senders they marked
// as spam.
function YorikAssistBulkButton({
  tab, counts, onDone,
}: {
  tab: "active" | "pending" | "spam";
  counts: { active: number; pending: number; spam: number; archived: number };
  onDone: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  if (tab === "spam") return null;
  const visibleCount = tab === "active" ? counts.active : counts.pending;
  if (visibleCount === 0) return null;

  async function run(enabled: boolean) {
    const verb = enabled ? "Enable" : "Disable";
    if (!confirm(
      `${verb} Yorik assist for all ${visibleCount} ${tab} contact${visibleCount === 1 ? "" : "s"}?\n\n` +
      `When enabled, Yorik may analyse new messages from these contacts and suggest one-click actions ` +
      `(replies, meeting slots). Toggle individual contacts off any time.`
    )) return;
    setBusy(true);
    try {
      await api.post<{ updated_count: number }>(
        "/api/contacts/yorik-assist/bulk",
        { scope: tab, enabled },
      );
      await onDone();
    } catch (e: any) {
      alert("Couldn't update: " + (e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      onClick={() => run(true)}
      disabled={busy}
      className="hidden md:flex text-xs h-8 px-3 rounded-md bg-card border border-border text-foreground hover:bg-muted items-center gap-1.5 disabled:opacity-50"
      title={`Enable Yorik assist for all ${visibleCount} ${tab} contacts`}
    >
      <Sparkles className="w-3.5 h-3.5" /> Enable AI · {visibleCount}
    </button>
  );
}


function EnrichButton() {
  const [busy, setBusy] = useState(false);
  const [worker, setWorker] = useState<{ status: string; detail: string;
    last_heartbeat_age_s: number | null } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await api.get<{ workers: Array<{ name: string; status: string;
        detail: string; last_heartbeat_age_s: number | null }> }>(
        "/api/dashboard/workers",
      );
      const w = (r.workers || []).find(x => x.name === "contact_enricher");
      setWorker(w || null);
    } catch {
      // workers endpoint failure is non-fatal; button just shows idle state
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [refresh]);

  // A worker is "running" when status is fresh-ish and the detail
  // line hasn't emitted DONE/STOPPED yet.
  const detail = worker?.detail || "";
  const fresh  = (worker?.last_heartbeat_age_s ?? 999) < 60;
  const terminal = detail.startsWith("DONE") || detail.startsWith("STOPPED") || detail.startsWith("CANCELLED");
  const running = !!worker && !terminal && (worker.status === "starting" || (worker.status === "ok" && fresh));

  async function start() {
    if (!confirm(
      "Enrich every contact by scanning emails, WhatsApp messages, and Paperless docs?\n\n" +
      "Proposals land in the edit-contact form as pre-filled fields + dropdowns of alternatives. " +
      "Nothing is changed until you save a contact. ~5-15 minutes for a couple hundred contacts " +
      "on a local LLM; runs in the background, safe to close this page."
    )) return;
    setBusy(true);
    try {
      await api.post("/api/contacts/enrich", {});
      refresh();
    } catch (e: any) {
      alert("Couldn't queue enrichment: " + (e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setBusy(true);
    try {
      await api.post("/api/contacts/enrich-cancel", {});
      refresh();
    } catch (e: any) {
      alert("Couldn't stop: " + (e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  if (running) {
    return (
      <button
        onClick={stop}
        disabled={busy}
        title={detail || "Enricher running…"}
        className="hidden md:flex text-xs h-8 px-3 rounded-md border border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300 hover:bg-amber-500/20 items-center gap-1.5 max-w-[260px]"
      >
        <StopCircle className="w-3.5 h-3.5 shrink-0" />
        <span className="truncate">{detail.slice(0, 40) || "Enriching…"}</span>
      </button>
    );
  }
  return (
    <button
      onClick={start}
      disabled={busy}
      className="hidden md:flex text-xs h-8 px-3 rounded-md bg-card border border-border text-foreground hover:bg-muted items-center gap-1.5"
      title="LLM-fill contact info from your emails, WhatsApp, and documents"
    >
      {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
      Enrich
    </button>
  );
}


// ─── ExtractButton ────────────────────────────────────────────────────
// Manually triggers the "walk every Paperless doc, propose new
// contacts" pipeline (backend/contact_extractor.py). Sibling to the
// EnrichButton above which is the bottom-up enrich-existing flow;
// this one is the top-down find-new flow.
//
// We poll /api/contacts/extractions/status so a click somewhere else
// in the household (another admin device) still shows the running
// state. After a successful kick-off the button navigates to
// Settings → Extractions where the review queue lives.

interface ExtractStatus {
  running:          boolean;
  pending:          number;
  progress:         { current: number; total: number; started_at: string | null };
  last_run_summary: Record<string, any>;
}

function ExtractButton() {
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [status, setStatus] = useState<ExtractStatus | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const navigate = useNavigate();

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.get<ExtractStatus>("/api/contact-extractions/status"));
    } catch {
      // Endpoint can 403 for non-admins — silently skip; button just
      // won't appear active.
    }
  }, []);

  useEffect(() => {
    refresh();
    // Faster cadence while running so the progress bar stays smooth;
    // slower when idle to keep the request rate low.
    const id = setInterval(refresh, status?.running ? 1500 : 4000);
    return () => clearInterval(id);
  }, [refresh, status?.running]);

  // The actual fire-the-scan call. Bound to the modal's confirm button
  // so the ugly browser confirm() is gone for good. On success, we
  // close the modal and let the button transition into the running
  // pill on the same page — NO automatic redirect to Settings, which
  // ripped focus out of /contacts mid-action. The user can still
  // click the running pill to jump to the queue if they want.
  async function confirmStart() {
    setBusy(true);
    setConfirmError(null);
    try {
      await api.post<{ started: boolean; already_running: boolean }>(
        "/api/contact-extractions/run", {},
      );
      refresh();
      setConfirmOpen(false);
    } catch (e: any) {
      setConfirmError(e?.message || "Couldn't start scan");
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setStopping(true);
    try {
      await api.post("/api/contact-extractions/stop", {});
      // Don't refresh aggressively — the worker stops on the next
      // doc boundary which can take a few seconds on a busy LLM.
      // The polling interval will catch the running=false flip.
    } catch (e: any) {
      setConfirmError(e?.message || "Couldn't stop scan");
    } finally {
      setStopping(false);
    }
  }

  return (
    <>
      {status?.running ? (() => {
        const cur = status.progress.current;
        const tot = status.progress.total;
        const pct = tot > 0 ? Math.min(100, Math.round((cur / tot) * 100)) : 0;
        return (
          <div className="hidden md:flex items-stretch h-8 rounded-md border border-pink-500/40 bg-pink-500/5 overflow-hidden">
            <button
              onClick={() => navigate("/contacts?tab=pending")}
              title="New contacts land in the Pending tab as the scan runs"
              className="relative flex items-center gap-1.5 pl-2.5 pr-3 text-xs text-pink-700 dark:text-pink-300 hover:bg-pink-500/10 transition min-w-[180px]"
            >
              <div
                className="absolute inset-y-0 left-0 bg-pink-500/20 transition-[width] duration-500"
                style={{ width: `${pct}%` }}
                aria-hidden
              />
              <Loader2 className="w-3.5 h-3.5 shrink-0 animate-spin relative" />
              <span className="relative tabular-nums">
                {cur}/{tot}
                {status.pending > 0 && (
                  <span className="opacity-70 ml-1.5">({status.pending} ready)</span>
                )}
              </span>
            </button>
            <button
              onClick={stop}
              disabled={stopping}
              title="Stop the scan (partial progress is kept)"
              className="px-2 border-l border-pink-500/40 text-pink-700 dark:text-pink-300 hover:bg-pink-500/15 transition disabled:opacity-60"
            >
              {stopping ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <StopCircle className="w-3.5 h-3.5" />}
            </button>
          </div>
        );
      })() : (
        <button
          onClick={() => { setConfirmError(null); setConfirmOpen(true); }}
          disabled={busy}
          className="hidden md:flex text-xs h-8 px-3 rounded-md bg-card border border-border text-foreground hover:bg-muted items-center gap-1.5"
          title="Walk every Paperless document and propose contacts. Review queue: Settings → Extractions."
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileText className="w-3.5 h-3.5" />}
          Scan docs
          {(status?.pending ?? 0) > 0 && (
            <span className="ml-1 px-1.5 rounded bg-pink-500/15 text-pink-700 dark:text-pink-300 text-[10px] tabular-nums">
              {status?.pending}
            </span>
          )}
        </button>
      )}

      {confirmOpen && (
        <ExtractConfirmModal
          busy={busy}
          error={confirmError}
          onCancel={() => { if (!busy) setConfirmOpen(false); }}
          onConfirm={confirmStart}
        />
      )}
    </>
  );
}


// ─── CrosslinkMailboxButton ─────────────────────────────────────────
// Enrich contacts that have no email channel by matching them against
// the IMAP corpus. Rule-based (no LLM): business name → domain root,
// person display_name → from_name fuzzy >= 0.85. Fast (seconds), high
// confidence only, every insert tagged source='mailbox_crosslink'.
//
// Sibling to EnrichButton (LLM-driven, slow, exhaustive) and to the
// extractor (top-down "find new contacts from docs"). This one is the
// targeted "find emails for contacts that have nothing".

type CrosslinkAddition = {
  contact_id: number;
  name: string;
  email: string;
  sender_name: string;
  confidence: string;
  kind: string;
};

type CrosslinkResult = {
  scanned: number;
  enriched: number;
  channels_added: number;
  senders_indexed: number;
  elapsed_s: number;
  skipped: Record<string, number>;
  additions: CrosslinkAddition[];
};

// ─── GroupByEmployerButton — propose business-employee links via LLM ──
// Click → POST /api/contacts/group-by-employer with dry_run. Returns
// a plan: for each business domain with ≥2 contacts, what's the
// canonical firm + which rows are employees vs the firm itself.
// User reviews, unchecks groups they don't want, hits Apply. The
// backend flips kind business→person where needed, strips firm
// prefix from display names, and sets employer_contact_id.

type GroupByEmployerMember = {
  id: number;
  kind: "person" | "business";
  display_name: string;
  email: string;
  type: "company" | "employee" | "skip";
  clean_name: string | null;
  is_canonical: boolean;
};

type GroupByEmployerGroup = {
  domain: string;
  existing_business_id: number | null;
  proposed_business: {
    display_name: string;
    legal_name: string | null;
    website: string | null;
    address: { line1: string | null; postcode: string | null;
               city: string | null; country: string | null } | null;
  };
  members: GroupByEmployerMember[];
};

type GroupByEmployerPlan = {
  groups: GroupByEmployerGroup[];
  stats: { domains_scanned: number; groups_proposed: number;
           members_total: number; elapsed_ms: number };
};

function GroupByEmployerButton({ onDone }: { onDone: () => Promise<void> | void }) {
  type State = "closed" | "loading" | "review" | "applying";
  const [state, setState] = useState<State>("closed");
  const [plan, setPlan] = useState<GroupByEmployerPlan | null>(null);
  // Set of (group domain) selected for apply. Pre-selected with all
  // groups; user unchecks ones they don't trust.
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  // Per-member override: maps member id → new classification, when
  // user changes the LLM's pick.
  const [overrides, setOverrides] = useState<Map<number, "employee" | "skip">>(new Map());

  async function start() {
    setState("loading");
    setPlan(null);
    setOverrides(new Map());
    try {
      const r = await api.post<GroupByEmployerPlan>(
        "/api/contacts/group-by-employer",
        { status: "active", dry_run: true },
      );
      setPlan(r);
      // Pre-select every group; user opts OUT, not IN.
      setSelectedGroups(new Set(r.groups.map(g => g.domain)));
      setState("review");
    } catch (e: any) {
      alert("Failed to build plan: " + (e?.message || e));
      setState("closed");
    }
  }

  function close() {
    setState("closed");
    setPlan(null);
    setOverrides(new Map());
  }

  async function apply() {
    if (!plan) return;
    setState("applying");
    const filteredGroups = plan.groups
      .filter(g => selectedGroups.has(g.domain))
      .map(g => ({
        ...g,
        members: g.members.map(m => ({
          ...m,
          type: overrides.get(m.id) || m.type,
        })),
      }));
    try {
      const r = await api.post<{
        groups_applied: number;
        businesses_created: number;
        businesses_reused: number;
        employees_linked: number;
        skipped_members: number;
      }>("/api/contacts/group-by-employer", {
        dry_run: false,
        plan: { groups: filteredGroups },
      });
      alert(
        `Applied ${r.groups_applied} groups.\n` +
        `${r.businesses_created} new business contacts created.\n` +
        `${r.businesses_reused} existing reused.\n` +
        `${r.employees_linked} employees linked.\n` +
        `${r.skipped_members} members skipped.`
      );
      await onDone();
      close();
    } catch (e: any) {
      alert("Apply failed: " + (e?.message || e));
      setState("review");
    }
  }

  return (
    <>
      <button
        onClick={start}
        className="hidden md:flex text-xs h-8 px-3 rounded-md bg-card border border-border text-foreground hover:bg-muted items-center gap-1.5"
        title="Detect multi-person firms among your contacts and group employees under one business contact"
      >
        <Briefcase className="w-3.5 h-3.5" /> Group by employer
      </button>

      {state === "loading" && createPortal(
        <div className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4">
          <div className="bg-card border border-border rounded-2xl p-6 max-w-sm text-center">
            <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2 text-primary" />
            <div className="text-sm font-medium">Scanning contacts…</div>
            <div className="text-xs text-muted-foreground mt-1">
              The LLM is reading email signatures to identify companies. Takes ~20-40 s.
            </div>
          </div>
        </div>,
        document.body,
      )}

      {state === "review" && plan && createPortal(
        <GroupByEmployerReviewModal
          plan={plan}
          selectedGroups={selectedGroups}
          overrides={overrides}
          onToggleGroup={(domain) => {
            setSelectedGroups(s => {
              const next = new Set(s);
              if (next.has(domain)) next.delete(domain);
              else next.add(domain);
              return next;
            });
          }}
          onSetMember={(id, type) => {
            setOverrides(m => {
              const next = new Map(m);
              next.set(id, type);
              return next;
            });
          }}
          onCancel={close}
          onApply={apply}
        />,
        document.body,
      )}

      {state === "applying" && createPortal(
        <div className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4">
          <div className="bg-card border border-border rounded-2xl p-6 max-w-sm text-center">
            <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2 text-primary" />
            <div className="text-sm font-medium">Applying changes…</div>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}

function GroupByEmployerReviewModal({
  plan, selectedGroups, overrides, onToggleGroup, onSetMember,
  onCancel, onApply,
}: {
  plan: GroupByEmployerPlan;
  selectedGroups: Set<string>;
  overrides: Map<number, "employee" | "skip">;
  onToggleGroup: (domain: string) => void;
  onSetMember: (id: number, type: "employee" | "skip") => void;
  onCancel: () => void;
  onApply: () => void;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const selectedCount = selectedGroups.size;

  return (
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={onCancel}
    >
      <div
        className="w-full max-w-3xl max-h-[90vh] bg-card border border-border rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between gap-3">
          <div>
            <div className="font-semibold">Group by employer — review</div>
            <div className="text-xs text-muted-foreground mt-0.5">
              {plan.groups.length} groups proposed · {selectedCount} selected · scanned {plan.stats.domains_scanned} domains
            </div>
          </div>
          <button onClick={onCancel} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="px-5 py-3 border-b border-border bg-muted/20 space-y-1 text-[11px]">
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-500 shrink-0">Parent</span>
            <span className="text-muted-foreground">The parent business — others get linked under it</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-500 shrink-0">→ Person</span>
            <span className="text-muted-foreground">Currently a Business; will be reclassified as Person</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border border-rose-500/40 text-rose-600 dark:text-rose-400 bg-rose-500/5 shrink-0">Skipped</span>
            <span className="text-muted-foreground">Not linked to the parent — left alone</span>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
          {plan.groups.length === 0 && (
            <div className="text-sm text-muted-foreground italic text-center py-8">
              No groups proposed. The LLM didn't find any multi-person firms among your active contacts.
            </div>
          )}
          {plan.groups.map(g => {
            const isSelected = selectedGroups.has(g.domain);
            const pb = g.proposed_business;
            const addr = pb.address;
            const addrStr = addr ? [addr.line1, addr.postcode, addr.city, addr.country]
              .filter(Boolean).join(", ") : "";
            return (
              <div
                key={g.domain}
                className={cn(
                  "rounded-lg border transition",
                  isSelected ? "border-blue-500/40 bg-blue-500/[0.04]" : "border-border bg-card opacity-60",
                )}
              >
                <label className="flex items-start gap-3 p-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => onToggleGroup(g.domain)}
                    className="mt-1 shrink-0 accent-blue-500"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <div className="w-6 h-6 rounded-full bg-blue-500/15 text-blue-500 flex items-center justify-center shrink-0">
                        <Briefcase className="w-3 h-3" />
                      </div>
                      <span className="font-medium text-sm">{pb.display_name}</span>
                      {g.existing_business_id !== null && (
                        <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                          reuse #{g.existing_business_id}
                        </span>
                      )}
                      <span className="text-[11px] text-muted-foreground ml-auto">@{g.domain}</span>
                    </div>
                    {pb.legal_name && (
                      <div className="text-[11px] text-muted-foreground italic mt-0.5 ml-8">{pb.legal_name}</div>
                    )}
                    {addrStr && (
                      <div className="text-[11px] text-muted-foreground mt-0.5 ml-8">{addrStr}</div>
                    )}
                  </div>
                </label>

                {isSelected && (
                  <ul className="border-t border-border px-3 py-2 space-y-1">
                    {g.members.map(m => {
                      const effective = overrides.get(m.id) ?? m.type;
                      const isFirm = m.is_canonical || effective === "company";
                      const willFlip = m.kind === "business" && effective === "employee";
                      return (
                        <li key={m.id} className="text-[12px] flex items-center gap-2">
                          <span className={cn(
                            "shrink-0 w-1.5 h-1.5 rounded-full",
                            isFirm ? "bg-blue-500"
                              : effective === "employee" ? "bg-amber-500"
                              : "bg-muted-foreground/40",
                          )} />
                          <span className="font-mono text-[10px] text-muted-foreground/70">#{m.id}</span>
                          <span className="text-foreground truncate">
                            {m.clean_name && effective === "employee" && m.clean_name !== m.display_name ? (
                              <>
                                <span className="line-through text-muted-foreground/60">{m.display_name}</span>
                                {" → "}
                                <span className="font-medium">{m.clean_name}</span>
                              </>
                            ) : (
                              m.display_name
                            )}
                          </span>
                          <span className="text-muted-foreground/70 text-[10px]">{m.email}</span>
                          <span className="ml-auto flex items-center gap-1">
                            {isFirm && (
                              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-500">Parent</span>
                            )}
                            {!isFirm && willFlip && (
                              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-500" title="Currently a Business; will be reclassified as Person on Apply">→ Person</span>
                            )}
                            {!isFirm && (
                              <button
                                onClick={() => onSetMember(m.id, effective === "skip" ? "employee" : "skip")}
                                className={cn(
                                  "text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border transition",
                                  effective === "skip"
                                    ? "border-rose-500/40 text-rose-600 dark:text-rose-400 bg-rose-500/5"
                                    : "border-border text-muted-foreground hover:text-rose-600",
                                )}
                                title={effective === "skip" ? "Cancel skip" : "Skip this row — don't link it"}
                              >
                                {effective === "skip" ? "skipped" : "skip"}
                              </button>
                            )}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            );
          })}
        </div>

        <footer className="px-5 py-3 border-t border-border flex items-center justify-end gap-2">
          <button onClick={onCancel} className="text-sm px-3 py-1.5 rounded-md hover:bg-muted text-muted-foreground">
            Cancel
          </button>
          <button
            onClick={onApply}
            disabled={selectedCount === 0}
            className="text-sm px-4 py-1.5 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Apply to {selectedCount} group{selectedCount === 1 ? "" : "s"}
          </button>
        </footer>
      </div>
    </div>
  );
}

function CrosslinkMailboxButton({ onDone }: { onDone: () => Promise<void> | void }) {
  type State = "idle" | "running" | "result";
  const [state, setState] = useState<State>("idle");
  const [result, setResult] = useState<CrosslinkResult | null>(null);

  async function start() {
    setState("running");
    setResult(null);
    try {
      const r = await api.post<CrosslinkResult>("/api/contacts/crosslink-mailbox", {});
      setResult(r);
      setState("result");
      if (r.channels_added > 0) {
        await onDone();
      }
    } catch (err: any) {
      alert(`Cross-link failed: ${err?.message || err}`);
      setState("idle");
    }
  }

  return (
    <>
      <button
        onClick={start}
        disabled={state === "running"}
        className="hidden md:flex text-xs h-8 px-3 rounded-md bg-card border border-border text-foreground hover:bg-muted items-center gap-1.5 disabled:opacity-50"
        title="Find emails in your inbox that match contacts without an email channel"
      >
        {state === "running"
          ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
          : <Mail className="w-3.5 h-3.5" />}
        Link mail
      </button>

      {state === "running" && createPortal(
        <div className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4">
          <div className="w-full max-w-sm bg-card border border-border rounded-2xl shadow-2xl overflow-hidden p-6">
            <div className="flex items-center gap-2.5 mb-3">
              <Loader2 className="w-5 h-5 animate-spin text-primary shrink-0" />
              <div className="font-semibold">Scanning your inbox…</div>
            </div>
            <CrosslinkProgressLine />
            <div className="text-[11px] text-muted-foreground/80 leading-relaxed mt-3">
              Matching contacts without an email channel against senders in email_messages.
              Conservative rules only — high-confidence links go through; the rest are reported as "skipped".
            </div>
          </div>
        </div>,
        document.body,
      )}

      {state === "result" && result && createPortal(
        <CrosslinkResultModal
          result={result}
          onClose={() => { setResult(null); setState("idle"); }}
        />,
        document.body,
      )}
    </>
  );
}

function CrosslinkProgressLine() {
  const [progress, setProgress] = useState<{ current: number; total: number; label: string }>(
    { current: 0, total: 0, label: "" },
  );
  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      try {
        const p = await api.get<{ current: number; total: number; label: string; done: boolean }>(
          "/api/contacts/crosslink-mailbox/progress",
        );
        if (stopped) return;
        if (!p.done && p.total > 0) {
          setProgress({ current: p.current, total: p.total, label: p.label || "" });
        }
      } catch { /* silent */ }
    };
    tick();
    const id = window.setInterval(tick, 700);
    return () => { stopped = true; window.clearInterval(id); };
  }, []);

  const pct = progress.total > 0
    ? Math.min(100, Math.round((progress.current / progress.total) * 100))
    : null;

  if (pct === null) {
    return (
      <div className="text-[11px] text-muted-foreground">
        Indexing senders…
      </div>
    );
  }
  return (
    <>
      <div className="h-1.5 bg-muted rounded-full overflow-hidden mb-2">
        <div className="h-full bg-primary transition-all duration-500 ease-out" style={{ width: `${pct}%` }} />
      </div>
      <div className="flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{progress.current} / {progress.total} contacts</span>
        <span>{pct}%</span>
      </div>
      {progress.label && (
        <div className="text-[11px] text-muted-foreground truncate font-mono mt-1">
          {progress.label}
        </div>
      )}
    </>
  );
}

function CrosslinkResultModal({
  result, onClose,
}: { result: CrosslinkResult; onClose: () => void }) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const skippedTotal = Object.values(result.skipped || {}).reduce((a, b) => a + b, 0);
  const addedAny = result.channels_added > 0;

  return (
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl max-h-[85vh] bg-card border border-border rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between gap-3">
          <div>
            <div className="font-semibold">Mailbox cross-link — done</div>
            <div className="text-xs text-muted-foreground mt-0.5">
              Scanned {result.scanned} contact{result.scanned === 1 ? "" : "s"} ·
              {" "}{result.senders_indexed} sender{result.senders_indexed === 1 ? "" : "s"} indexed ·
              {" "}{result.elapsed_s}s
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          <div className={cn(
            "p-3 rounded-lg border text-sm",
            addedAny
              ? "border-emerald-500/40 bg-emerald-500/5 text-emerald-800 dark:text-emerald-300"
              : "border-border bg-muted/30 text-muted-foreground",
          )}>
            {addedAny
              ? <><strong>{result.channels_added}</strong> email channel{result.channels_added === 1 ? "" : "s"} linked to <strong>{result.enriched}</strong> contact{result.enriched === 1 ? "" : "s"}.</>
              : <>No new email links found. All senders in your inbox either already belong to a contact or didn't match any contact without an email.</>}
          </div>

          {addedAny && (
            <div>
              <div className="text-[11px] uppercase tracking-wider text-muted-foreground mb-2">Newly linked</div>
              <ul className="space-y-1.5">
                {result.additions.map((a, i) => (
                  <li key={i} className="text-[12px] flex items-start gap-2 p-2 rounded border border-border bg-background/40">
                    <span className="font-mono text-[10px] text-muted-foreground/70 mt-0.5">#{a.contact_id}</span>
                    <div className="min-w-0 flex-1">
                      <div className="font-medium">{a.name}</div>
                      <div className="text-muted-foreground">
                        {a.email}
                        {a.sender_name && a.sender_name.toLowerCase() !== a.name.toLowerCase() && (
                          <span className="ml-1.5 text-muted-foreground/70">(sender: {a.sender_name})</span>
                        )}
                        <span className="ml-1.5 text-[10px] uppercase tracking-wider text-muted-foreground/60">· {a.confidence}</span>
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {skippedTotal > 0 && (
            <details className="rounded-lg border border-border bg-muted/20">
              <summary className="cursor-pointer p-3 text-sm font-medium select-none">
                Skipped ({skippedTotal})
              </summary>
              <div className="px-3 pb-3 space-y-1 text-[12px]">
                {Object.entries(result.skipped).map(([reason, n]) => n > 0 && (
                  <div key={reason} className="flex justify-between border-t border-border/40 pt-1">
                    <span className="text-muted-foreground">{reason.replace(/_/g, " ")}</span>
                    <span className="font-mono">{n}</span>
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>

        <footer className="px-5 py-3 border-t border-border flex justify-end">
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded-md text-sm font-medium bg-primary text-primary-foreground hover:bg-primary/90"
          >
            Done
          </button>
        </footer>
      </div>
    </div>
  );
}


// ─── TriageButton — fast keyboard-driven approval/dismiss for pending ───
// Click → opens a full-screen modal listing pending contacts (newest first).
// Each row carries the source-doc summary so the user decides without
// drilling in. Per-row state: untouched / approve (→active) / dismiss (→spam).
// Keyboard: Y/A approve, N/D dismiss, S skip, ↑↓ cursor, Cmd-Enter apply,
// Esc close. Pagination at 100/page to keep the DOM light.

// Four-way verdict: gentler than the legacy approve/dismiss split.
//   active_person   → status=active, kind=person
//   active_business → status=active, kind=business
//   archived        → status=archived (gentle; no sender block)
//   spam            → status=spam (sender-block hook)
type TriageDecision = "active_person" | "active_business" | "archived" | "spam" | null;

type TriageChannel = { kind: string; value: string };
type TriageAddress = { line1: string; postcode: string; city: string; country: string };

type TriageItem = {
  id: number;
  name: string;
  kind: "person" | "business";
  summary: string;
  channels: TriageChannel[];
  addresses: TriageAddress[];
  doc_id?: number | null;
  // LLM-suggested verdict from /api/contacts/triage/auto-classify.
  // null when the LLM pass hasn't run yet (or returned an unparseable
  // verdict for this row).
  triage_verdict?:    TriageDecision;
  triage_reason?:     string | null;
  triage_confidence?: "low" | "medium" | "high" | null;
};

type TriageListResponse = {
  items: TriageItem[];
  total: number;
  limit: number;
  offset: number;
  kind: string | null;
};

// ─── CleanupPipeline — numbered steps for the per-tab cleanup ritual ───
//
// Solves the "wall of 8-11 unsorted buttons" problem on the contacts
// header. Each tab has a small linear pipeline:
//   Pending: 1. Classify  →  2. Dedupe  →  3. Review & promote
//   Active:  1. Dedupe    →  2. Group by employer
//
// The pipeline doesn't replace the underlying components — it just
// wraps them in a numbered strip with subtle ↦ connectors and a
// step-state badge ("ready · N" / "✓ done" / hidden when nothing to
// do). Each step button stays independently clickable; numbers are
// guidance, not a lock.
// localStorage keys for "last run" stamps. Used by the pipeline to
// flip a step's badge to ✓ after the user has run it, even when the
// underlying count metric ("any merge candidates left?") would still
// say "ready" (we don't pre-compute that cheaply). Per-browser, not
// per-user — fine for v1, can move to user_profiles JSON later.
const _LS_LAST_RUN = {
  dedupePending: "yorik:contacts:lastrun:dedupe:pending",
  dedupeActive:  "yorik:contacts:lastrun:dedupe:active",
  groupByEmp:    "yorik:contacts:lastrun:group-by-employer",
};
// How long after running do we consider a step "done"? Long enough that
// the user notices the green ✓ ("yeah, I did that"); short enough that
// stale data eventually surfaces again as actionable.
const _LAST_RUN_FRESH_MS = 7 * 24 * 60 * 60 * 1000;  // 7 days

function _isFresh(key: string): boolean {
  try {
    const v = localStorage.getItem(key);
    if (!v) return false;
    const t = Date.parse(v);
    if (isNaN(t)) return false;
    return (Date.now() - t) < _LAST_RUN_FRESH_MS;
  } catch { return false; }
}
function _stampRun(key: string): void {
  try { localStorage.setItem(key, new Date().toISOString()); } catch {}
}

function CleanupPipeline({
  tab, counts, classifyKickCount, setClassifyKickCount, onOpenTriage, onRefresh,
}: {
  tab: "active" | "pending" | "spam";
  counts: StatusCounts;
  classifyKickCount: number;
  setClassifyKickCount: React.Dispatch<React.SetStateAction<number>>;
  onOpenTriage: () => void;
  onRefresh: () => Promise<void> | void;
}) {
  // Bumped whenever a wrapped onDone fires so the badge logic re-reads
  // localStorage. (localStorage writes don't fire React re-renders.)
  const [runTick, setRunTick] = useState(0);
  const dedupePendingDone = useMemo(
    () => _isFresh(_LS_LAST_RUN.dedupePending),
    [runTick, counts.pending],
  );
  const dedupeActiveDone = useMemo(
    () => _isFresh(_LS_LAST_RUN.dedupeActive),
    [runTick, counts.active],
  );
  const groupByEmpDone = useMemo(
    () => _isFresh(_LS_LAST_RUN.groupByEmp),
    [runTick, counts.active],
  );

  async function wrap(key: string) {
    _stampRun(key);
    setRunTick(t => t + 1);
    await onRefresh();
  }

  if (tab === "spam") return null;
  // Don't render the whole strip when there's no work either tab can
  // possibly do — keeps the empty state of the header clean.
  if (tab === "pending" && counts.pending === 0) return null;
  if (tab === "active" && counts.active < 2) return null;

  return (
    <div className="hidden md:inline-flex items-stretch gap-1.5 rounded-md border border-border bg-muted/30 px-1.5 py-1">
      {tab === "pending" && (
        <>
          <PipelineStep
            number={1}
            label="Classify"
            badge={
              (counts.pending_unclassified ?? counts.pending) > 0
                ? `${counts.pending_unclassified ?? counts.pending} ready`
                : "✓"
            }
          >
            <AutoClassifyButton
              onDone={onRefresh}
              externalKick={classifyKickCount}
              onComplete={onOpenTriage}
            />
          </PipelineStep>
          <PipelineArrow />
          <PipelineStep
            number={2}
            label="Dedupe"
            badge={
              counts.pending <= 1 || dedupePendingDone ? "✓" : "ready"
            }
            dimmed={counts.pending <= 1}
          >
            <DedupeButton onDone={() => wrap(_LS_LAST_RUN.dedupePending)} status="pending" />
          </PipelineStep>
          <PipelineArrow />
          <PipelineStep
            number={3}
            label="Review"
            badge={counts.pending > 0 ? `${counts.pending} ready` : "✓"}
          >
            <TriageButton onOpen={onOpenTriage} />
          </PipelineStep>
        </>
      )}

      {tab === "active" && (
        <>
          <PipelineStep
            number={1}
            label="Dedupe"
            badge={
              counts.active <= 1 || dedupeActiveDone ? "✓" : "ready"
            }
            dimmed={counts.active <= 1}
          >
            <DedupeButton onDone={() => wrap(_LS_LAST_RUN.dedupeActive)} status="active" />
          </PipelineStep>
          <PipelineArrow />
          <PipelineStep
            number={2}
            label="Group by employer"
            badge={groupByEmpDone ? "✓" : "ready"}
          >
            <GroupByEmployerButton onDone={() => wrap(_LS_LAST_RUN.groupByEmp)} />
          </PipelineStep>
        </>
      )}
    </div>
  );
}

function PipelineStep({
  number, label, badge, dimmed = false, children,
}: {
  number: number;
  label: string;
  badge: string;
  dimmed?: boolean;
  children: React.ReactNode;
}) {
  const isDone = badge === "✓";
  return (
    <div className={cn(
      "flex flex-col items-stretch gap-0.5 px-1 transition",
      dimmed && "opacity-50",
    )}>
      <div className="flex items-center gap-1.5 px-1 text-[10px] text-muted-foreground select-none">
        <span className={cn(
          "inline-flex w-4 h-4 rounded-full items-center justify-center text-[9px] font-semibold shrink-0",
          isDone
            ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300"
            : "bg-primary/20 text-primary",
        )}>{isDone ? "✓" : number}</span>
        <span className="font-medium text-foreground/80 truncate">{label}</span>
        <span className={cn(
          "ml-auto text-[10px] uppercase tracking-wider shrink-0",
          isDone ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground",
        )} title={badge}>
          {badge}
        </span>
      </div>
      {children}
    </div>
  );
}

function PipelineArrow() {
  return (
    <div className="self-center text-muted-foreground/40 select-none px-0.5">→</div>
  );
}


function TriageButton({ onOpen }: { onOpen: () => void }) {
  return (
    <button
      onClick={onOpen}
      className="hidden md:flex text-xs h-8 px-3 rounded-md bg-card border border-border text-foreground hover:bg-muted items-center gap-1.5"
      title="Fast keyboard-driven approval/dismiss for pending contacts"
    >
      <Check className="w-3.5 h-3.5" /> Triage
    </button>
  );
}

function TriageModal({ onClose, onApplied }: {
  onClose: () => void;
  onApplied: () => Promise<void> | void;
}) {
  const [items, setItems] = useState<TriageItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [kindFilter, setKindFilter] = useState<"person" | "business" | null>(null);
  // Data-completeness filters. When all unchecked → bulk actions apply to
  // every visible row (current behaviour). When any checked → bulk actions
  // act only on rows that have ALL the checked signals. Lets the user
  // bulk-approve "every contact with a phone+email" without picking
  // through 100+ cards.
  const [requireEmail, setRequireEmail]     = useState(false);
  const [requirePhone, setRequirePhone]     = useState(false);
  const [requireAddress, setRequireAddress] = useState(false);
  const [decisions, setDecisions] = useState<Map<number, TriageDecision>>(new Map());
  const [cursor, setCursor] = useState(0);
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  // Inline email preview — row id of the currently-expanded contact, or
  // null when nothing is expanded. Single-open at a time so the modal
  // doesn't balloon vertically when the user has many contacts open.
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const PAGE_SIZE = 100;

  // Items matching the current filter set. When no filter is on this is
  // every item (so "Approve all" semantics stay correct).
  const matchingItems = useMemo(() => {
    if (!requireEmail && !requirePhone && !requireAddress) return items;
    return items.filter(it => {
      const ch = it.channels || [];
      if (requireEmail   && !ch.some(c => c.kind === "email")) return false;
      if (requirePhone   && !ch.some(c => c.kind === "phone" || c.kind === "whatsapp" || c.kind === "sms")) return false;
      if (requireAddress && (it.addresses || []).length === 0) return false;
      return true;
    });
  }, [items, requireEmail, requirePhone, requireAddress]);

  const filterActive = requireEmail || requirePhone || requireAddress;

  const fetchPage = useCallback(async (newOffset: number, newKind: typeof kindFilter) => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(newOffset) });
      if (newKind) qs.set("kind", newKind);
      const r = await api.get<TriageListResponse>(`/api/contacts/triage/list?${qs}`);
      // Defensive — if the backend isn't restarted yet (endpoint missing
      // or shape mismatch), keep items as [] so .length / .map don't blow
      // up the render. The empty-state shows "Inbox zero" which at least
      // doesn't crash the page.
      const fetched = Array.isArray(r?.items) ? r.items : [];
      setItems(fetched);
      setTotal(Number(r?.total) || 0);
      setOffset(Number(r?.offset) || 0);
      setCursor(0);
      // Pre-fill decisions from LLM verdicts for any newly-fetched
      // rows that don't already have a user decision. Existing
      // decisions (the user has explicitly overridden a row earlier)
      // are preserved.
      setDecisions(prev => {
        const next = new Map(prev);
        for (const it of fetched) {
          if (!next.has(it.id) && it.triage_verdict) {
            next.set(it.id, it.triage_verdict);
          }
        }
        return next;
      });
    } catch (err: any) {
      console.error("triage: fetch failed", err);
      setItems([]);
      setTotal(0);
      alert("Couldn't load triage list: " + (err?.message || err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchPage(0, null); }, [fetchPage]);

  const decide = useCallback((id: number, d: TriageDecision) => {
    setDecisions(prev => {
      const next = new Map(prev);
      if (d === null) next.delete(id);
      else next.set(id, d);
      return next;
    });
  }, []);

  const approveAll = useCallback(() => {
    // Legacy bulk-approve still useful when the LLM pass hasn't run:
    // marks every matching row as active_person.
    setDecisions(prev => {
      const next = new Map(prev);
      for (const it of matchingItems) next.set(it.id, "active_person");
      return next;
    });
  }, [matchingItems]);

  const dismissAll = useCallback(() => {
    setDecisions(prev => {
      const next = new Map(prev);
      for (const it of matchingItems) next.set(it.id, "spam");
      return next;
    });
  }, [matchingItems]);

  const acceptLLMAll = useCallback(() => {
    // "Trust Yorik for everyone on this page" — pre-fills decisions
    // from each row's triage_verdict. Skips rows the LLM didn't
    // classify (verdict missing or null).
    setDecisions(prev => {
      const next = new Map(prev);
      for (const it of matchingItems) {
        if (it.triage_verdict) next.set(it.id, it.triage_verdict);
      }
      return next;
    });
  }, [matchingItems]);

  const clearPage = useCallback(() => {
    setDecisions(prev => {
      const next = new Map(prev);
      // Clear visible-on-page decisions only — keeps decisions from other
      // pages intact for the eventual apply.
      for (const it of items) next.delete(it.id);
      return next;
    });
  }, [items]);

  const apply = useCallback(async () => {
    // Group decisions into the four buckets the new triage_apply
    // endpoint expects. An empty body short-circuits — no round-trip
    // when the user hasn't decided anything yet.
    const active_person   = [...decisions].filter(([, d]) => d === "active_person").map(([id]) => id);
    const active_business = [...decisions].filter(([, d]) => d === "active_business").map(([id]) => id);
    const archived        = [...decisions].filter(([, d]) => d === "archived").map(([id]) => id);
    const spam            = [...decisions].filter(([, d]) => d === "spam").map(([id]) => id);
    const totalDecisions = active_person.length + active_business.length + archived.length + spam.length;
    if (totalDecisions === 0) return;
    setApplying(true);
    try {
      const r = await api.post<{
        active_person: number; active_business: number;
        archived: number; spam: number;
      }>(
        "/api/contacts/triage/apply",
        { active_person, active_business, archived, spam },
      );
      setDecisions(new Map());
      await onApplied();
      await fetchPage(0, kindFilter);
      console.log(
        `Applied: ${r.active_person} person, ${r.active_business} business, ` +
        `${r.archived} archived, ${r.spam} spam.`,
      );
    } catch (err: any) {
      alert("Apply failed: " + (err?.message || err));
    } finally {
      setApplying(false);
    }
  }, [decisions, onApplied, fetchPage, kindFilter]);

  // Keyboard shortcuts. Tied to the cursor row.
  //   p / 1   → active_person
  //   b / 2   → active_business
  //   a / 3   → archived
  //   s / 4   → spam
  //   0 / x   → clear decision
  //   ↓ / j   → next row (without changing decision — accepts LLM verdict if pre-filled)
  //   ↑ / k   → previous row
  //   ⌘↵     → apply
  //   Esc    → close
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (applying) return;
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea") return;
      if (e.key === "Escape") { onClose(); return; }
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); apply(); return; }
      if (items.length === 0) return;
      const current = items[cursor];
      if (!current) return;
      const advance = () => setCursor(c => Math.min(c + 1, items.length - 1));
      const k = e.key.toLowerCase();
      if (k === "p" || k === "1") {
        e.preventDefault(); decide(current.id, "active_person"); advance();
      } else if (k === "b" || k === "2") {
        e.preventDefault(); decide(current.id, "active_business"); advance();
      } else if (k === "a" || k === "3") {
        e.preventDefault(); decide(current.id, "archived"); advance();
      } else if (k === "s" || k === "4") {
        e.preventDefault(); decide(current.id, "spam"); advance();
      } else if (k === "0" || k === "x") {
        e.preventDefault(); decide(current.id, null); advance();
      } else if (e.key === "ArrowDown" || k === "j" || e.key === " ") {
        e.preventDefault(); advance();
      } else if (e.key === "ArrowUp" || k === "k") {
        e.preventDefault();
        setCursor(c => Math.max(c - 1, 0));
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, cursor, decide, apply, applying, onClose]);

  // Scroll cursor row into view when it changes
  useEffect(() => {
    const node = listRef.current?.querySelector(`[data-cursor="true"]`) as HTMLElement | null;
    node?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [cursor]);

  const personCount   = [...decisions.values()].filter(d => d === "active_person").length;
  const businessCount = [...decisions.values()].filter(d => d === "active_business").length;
  const archivedCount = [...decisions.values()].filter(d => d === "archived").length;
  const spamCount     = [...decisions.values()].filter(d => d === "spam").length;
  const pendingChanges = personCount + businessCount + archivedCount + spamCount;
  // How many rows on the current page still have no decision (and no
  // pre-fill from an LLM verdict)?  Drives the empty-state hint
  // suggesting the user run auto-classify.
  const undecidedOnPage = items.filter(it => !decisions.has(it.id)).length;

  return (
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={applying ? undefined : onClose}
    >
      <div
        className="w-full max-w-4xl h-[92vh] bg-card border border-border rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <header className="px-5 py-4 border-b border-border flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="font-semibold">Triage pending contacts</div>
            <div className="text-xs text-muted-foreground mt-0.5 truncate">
              {total} pending{kindFilter ? ` ${kindFilter}` : ""} ·
              {" "}showing {items.length} ·
              {" "}{pendingChanges} decision{pendingChanges === 1 ? "" : "s"} ready
              {personCount   > 0 && <span className="text-emerald-600 dark:text-emerald-400"> · {personCount} person</span>}
              {businessCount > 0 && <span className="text-sky-600 dark:text-sky-400"> · {businessCount} business</span>}
              {archivedCount > 0 && <span className="text-muted-foreground"> · {archivedCount} archive</span>}
              {spamCount     > 0 && <span className="text-rose-600 dark:text-rose-400"> · {spamCount} spam</span>}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {(["all", "person", "business"] as const).map(k => {
              const v = k === "all" ? null : k;
              const isActive = v === kindFilter;
              return (
                <button
                  key={k}
                  onClick={() => { setKindFilter(v); fetchPage(0, v); }}
                  disabled={applying || loading}
                  className={cn(
                    "text-[11px] px-2 py-1 rounded border transition disabled:opacity-50",
                    isActive
                      ? "border-primary/40 bg-primary/5 text-primary"
                      : "border-border text-muted-foreground hover:text-foreground hover:bg-muted",
                  )}
                >
                  {k}
                </button>
              );
            })}
            <button
              onClick={onClose}
              disabled={applying}
              className="p-1.5 hover:bg-muted rounded-md text-muted-foreground disabled:opacity-50"
              aria-label="Close"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </header>

        {/* Filter row — narrow bulk actions to rows with specific data */}
        <div className="px-5 py-2 border-b border-border/60 flex items-center flex-wrap gap-x-4 gap-y-1.5 bg-muted/15">
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground/70 font-semibold shrink-0">
            Filter (bulk acts on rows that have ALL checked):
          </span>
          <label className={cn(
            "inline-flex items-center gap-1.5 text-[12px] cursor-pointer select-none px-2 py-1 rounded transition",
            requireEmail ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground",
          )}>
            <input
              type="checkbox"
              checked={requireEmail}
              onChange={(e) => setRequireEmail(e.target.checked)}
              className="accent-primary"
            />
            <Mail className="w-3 h-3" />
            Email
          </label>
          <label className={cn(
            "inline-flex items-center gap-1.5 text-[12px] cursor-pointer select-none px-2 py-1 rounded transition",
            requirePhone ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground",
          )}>
            <input
              type="checkbox"
              checked={requirePhone}
              onChange={(e) => setRequirePhone(e.target.checked)}
              className="accent-primary"
            />
            <Phone className="w-3 h-3" />
            Phone
          </label>
          <label className={cn(
            "inline-flex items-center gap-1.5 text-[12px] cursor-pointer select-none px-2 py-1 rounded transition",
            requireAddress ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground",
          )}>
            <input
              type="checkbox"
              checked={requireAddress}
              onChange={(e) => setRequireAddress(e.target.checked)}
              className="accent-primary"
            />
            <MapPin className="w-3 h-3" />
            Address
          </label>
          {filterActive && (
            <button
              onClick={() => { setRequireEmail(false); setRequirePhone(false); setRequireAddress(false); }}
              className="text-[11px] text-muted-foreground hover:underline ml-auto"
            >
              Clear filter
            </button>
          )}
          {filterActive && (
            <span className="text-[11px] text-muted-foreground">
              {matchingItems.length} of {items.length} on page match
            </span>
          )}
        </div>

        {/* Bulk toolbar — prominent action buttons */}
        <div className="px-5 py-3 border-b border-border flex items-center flex-wrap gap-2">
          <button
            onClick={acceptLLMAll}
            disabled={
              matchingItems.length === 0 ||
              applying ||
              loading ||
              !matchingItems.some(it => it.triage_verdict)
            }
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium border border-primary/50 bg-primary/10 text-primary hover:bg-primary/15 disabled:opacity-40 disabled:cursor-not-allowed"
            title="Trust Yorik's per-row verdicts and pre-fill all decisions on this page"
          >
            <Sparkles className="w-3.5 h-3.5" />
            Accept Yorik's verdicts
          </button>
          <button
            onClick={approveAll}
            disabled={matchingItems.length === 0 || applying || loading}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium border border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-40 disabled:cursor-not-allowed"
            title={filterActive
              ? "Mark every matching row as Person"
              : "Mark every visible row as Person"}
          >
            <Check className="w-3.5 h-3.5" />
            All as person
          </button>
          <button
            onClick={dismissAll}
            disabled={matchingItems.length === 0 || applying || loading}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium border border-rose-500/40 bg-rose-500/10 text-rose-700 dark:text-rose-300 hover:bg-rose-500/20 disabled:opacity-40 disabled:cursor-not-allowed"
            title={filterActive
              ? "Mark every matching row as Spam"
              : "Mark every visible row as Spam"}
          >
            <X className="w-3.5 h-3.5" />
            All as spam
          </button>
          <button
            onClick={clearPage}
            disabled={applying || loading}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium border border-border text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
            title="Clear all decisions on this page"
          >
            Clear page
          </button>
          <span className="ml-auto text-[11px] text-muted-foreground text-right hidden md:inline">
            <kbd className="font-mono px-1 rounded bg-muted/60">P</kbd>=person ·
            {" "}<kbd className="font-mono px-1 rounded bg-muted/60">B</kbd>=business ·
            {" "}<kbd className="font-mono px-1 rounded bg-muted/60">A</kbd>=archive ·
            {" "}<kbd className="font-mono px-1 rounded bg-muted/60">S</kbd>=spam ·
            {" "}<kbd className="font-mono px-1 rounded bg-muted/60">↑↓</kbd>=move ·
            {" "}<kbd className="font-mono px-1 rounded bg-muted/60">⌘↵</kbd>=apply
          </span>
        </div>

        {/* List */}
        <div ref={listRef} className="flex-1 overflow-y-auto px-3 py-3 space-y-1">
          {loading && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin mr-2" /> Loading…
            </div>
          )}
          {!loading && items.length === 0 && (
            <div className="text-center py-12 text-muted-foreground italic">
              No pending {kindFilter ?? "contact"}s. Inbox zero.
            </div>
          )}
          {!loading && items.map((it, idx) => {
            const decision = decisions.get(it.id) ?? null;
            const isCursor = idx === cursor;
            const channels = it.channels || [];
            const addresses = it.addresses || [];
            const hasData = channels.length > 0 || addresses.length > 0;
            // True when filter is active and this row does NOT match. Dimmed
            // so the user sees at a glance what "Approve N matching" will
            // touch and what it will leave alone.
            const isOutsideFilter = filterActive && !matchingItems.includes(it);
            return (
              <div
                key={it.id}
                data-cursor={isCursor ? "true" : "false"}
                onClick={() => setCursor(idx)}
                className={cn(
                  "rounded-lg border p-3.5 transition cursor-pointer flex items-start gap-4",
                  isCursor && "ring-2 ring-primary/50",
                  decision === "active_person"   && "bg-emerald-500/5 border-emerald-500/40",
                  decision === "active_business" && "bg-sky-500/5 border-sky-500/40",
                  decision === "archived"        && "bg-muted/40 border-border",
                  decision === "spam"            && "bg-rose-500/5 border-rose-500/40",
                  !decision && "bg-card border-border hover:bg-muted/30",
                  isOutsideFilter && !decision && "opacity-40",
                )}
              >
                {/* Left: info column */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="text-sm font-semibold truncate">{it.name}</span>
                    <span className="text-[10px] uppercase tracking-wider text-muted-foreground/70 px-1.5 py-0.5 rounded bg-muted/40">
                      {it.kind}
                    </span>
                    {!hasData && (
                      <span className="text-[10px] text-muted-foreground/50 italic">name only</span>
                    )}
                    <span className="ml-auto text-[10px] font-mono text-muted-foreground/40">#{it.id}</span>
                  </div>

                  {it.summary && (
                    <div className="text-[12.5px] text-muted-foreground mt-1.5 leading-snug line-clamp-2">
                      {it.summary}
                    </div>
                  )}

                  {it.triage_reason && (
                    <div className="mt-1.5 text-[11.5px] flex items-start gap-1.5 text-foreground/70 leading-snug">
                      <Sparkles className="w-3 h-3 text-primary/70 mt-[2px] shrink-0" />
                      <span className="italic">
                        Yorik: {it.triage_reason}
                        {it.triage_confidence && (
                          <span className="ml-1 text-[10px] uppercase tracking-wider text-muted-foreground/70 not-italic">
                            · {it.triage_confidence}
                          </span>
                        )}
                      </span>
                    </div>
                  )}

                  {(channels.length > 0 || addresses.length > 0) && (
                    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
                      {channels.map((ch, i) => {
                        const Icon = ch.kind === "email"  ? Mail
                                   : ch.kind === "phone"  ? Phone
                                   : ch.kind === "whatsapp" ? MessageCircle
                                   : ch.kind === "website" ? Globe
                                   : Mail;
                        return (
                          <span key={`ch-${i}`} className="inline-flex items-center gap-1 text-foreground/85">
                            <Icon className="w-3 h-3 text-muted-foreground/70 shrink-0" />
                            <span className="truncate max-w-[280px]">{ch.value}</span>
                          </span>
                        );
                      })}
                      {addresses.map((a, i) => {
                        const parts = [a.line1, a.postcode, a.city, a.country].filter(Boolean);
                        if (!parts.length) return null;
                        return (
                          <span key={`a-${i}`} className="inline-flex items-center gap-1 text-foreground/85">
                            <MapPin className="w-3 h-3 text-muted-foreground/70 shrink-0" />
                            <span className="truncate max-w-[340px]">{parts.join(", ")}</span>
                          </span>
                        );
                      })}
                    </div>
                  )}

                  {/* Show emails — inline preview of recent inbound emails
                      from this contact so the user can verify the LLM's
                      verdict without closing the modal. Single row open
                      at a time. */}
                  <div className="mt-2">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        setExpandedId(expandedId === it.id ? null : it.id);
                      }}
                      className="text-[11px] text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
                    >
                      <Mail className="w-3 h-3" />
                      {expandedId === it.id ? "Hide emails" : "Show emails"}
                      <ChevronRight className={cn(
                        "w-3 h-3 transition-transform",
                        expandedId === it.id && "rotate-90",
                      )} />
                    </button>
                    {expandedId === it.id && (
                      <TriageEmailPreview contactId={it.id} />
                    )}
                  </div>
                </div>

                {/* Right: four outcome chips (P/B/A/S). The chip whose
                    outcome matches the active decision is highlighted.
                    Clicking the active chip clears the decision. */}
                <div className="flex items-center gap-1 shrink-0 self-start pt-0.5">
                  <TriageChip
                    label="Person"
                    shortcut="P"
                    icon={Check}
                    active={decision === "active_person"}
                    tone="emerald"
                    onClick={(e) => { e.stopPropagation(); decide(it.id, decision === "active_person" ? null : "active_person"); }}
                    suggested={it.triage_verdict === "active_person"}
                  />
                  <TriageChip
                    label="Business"
                    shortcut="B"
                    icon={Globe}
                    active={decision === "active_business"}
                    tone="sky"
                    onClick={(e) => { e.stopPropagation(); decide(it.id, decision === "active_business" ? null : "active_business"); }}
                    suggested={it.triage_verdict === "active_business"}
                  />
                  <TriageChip
                    label="Archive"
                    shortcut="A"
                    icon={MapPin}
                    active={decision === "archived"}
                    tone="muted"
                    onClick={(e) => { e.stopPropagation(); decide(it.id, decision === "archived" ? null : "archived"); }}
                    suggested={it.triage_verdict === "archived"}
                  />
                  <TriageChip
                    label="Spam"
                    shortcut="S"
                    icon={X}
                    active={decision === "spam"}
                    tone="rose"
                    onClick={(e) => { e.stopPropagation(); decide(it.id, decision === "spam" ? null : "spam"); }}
                    suggested={it.triage_verdict === "spam"}
                  />
                </div>
              </div>
            );
          })}
        </div>

        {/* Footer */}
        <footer className="px-5 py-3 border-t border-border flex items-center gap-3">
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <button
              onClick={() => fetchPage(Math.max(0, offset - PAGE_SIZE), kindFilter)}
              disabled={offset === 0 || loading || applying}
              className="px-2 py-1 rounded border border-border hover:bg-muted disabled:opacity-30"
            >
              ← Prev
            </button>
            <span>
              {offset + 1}–{offset + items.length} of {total}
            </span>
            <button
              onClick={() => fetchPage(offset + PAGE_SIZE, kindFilter)}
              disabled={offset + items.length >= total || loading || applying}
              className="px-2 py-1 rounded border border-border hover:bg-muted disabled:opacity-30"
            >
              Next →
            </button>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={onClose}
              disabled={applying}
              className="px-3 py-2 rounded-md text-sm font-medium border border-border hover:bg-muted text-muted-foreground hover:text-foreground"
            >
              Close
            </button>
            <button
              onClick={apply}
              disabled={applying || pendingChanges === 0}
              className="px-4 py-2 rounded-md text-sm font-medium bg-primary text-primary-foreground hover:bg-primary/90 inline-flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {applying && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              {applying ? "Applying…" : `Apply ${pendingChanges} change${pendingChanges === 1 ? "" : "s"}`}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}


// One outcome button in the triage modal's per-row chip row. Compact
// (icon + first letter) on narrow screens, with the keyboard shortcut
// shown on hover. The "suggested" prop adds a subtle ring around chips
// the LLM verdict pre-filled, so the user can spot Yorik's recommendation
// even when they've already overridden it.
// ─── TriageEmailPreview — inline last-emails for one row ───────────
// Renders inside an expanded triage row so the user can verify the
// LLM's verdict without closing the modal. Reuses the same
// /api/contacts/{id}/timeline endpoint the contact detail view uses,
// filtered to email items only.
function TriageEmailPreview({ contactId }: { contactId: number }) {
  const api_ = useApi<ContactTimeline>(
    `/api/contacts/${contactId}/timeline?limit=5`,
    [contactId],
  );
  const items = (api_.data?.items || []).filter(it => it.kind === "email");

  if (api_.loading) {
    return (
      <div className="mt-2 text-[11px] text-muted-foreground italic">
        Loading emails…
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="mt-2 text-[11px] text-muted-foreground italic">
        No emails found from this sender.
      </div>
    );
  }
  return (
    <ol className="mt-2 space-y-1 border-l-2 border-border pl-3">
      {items.map((it, i) => (
        <li key={i}>
          {/* Anchor (not navigate) → opens in a new tab so the triage
              modal stays untouched. Cmd/Ctrl+click also works natively,
              and the URL is visible on hover for keyboard users.

              `it.link` is React-Router-relative (e.g. "/email?msg=42").
              When opening in a new browser tab we need the absolute
              SPA URL "/r/email?msg=42" — the legacy vanilla frontend
              lives at /email and shows the wrong app. Inside React
              Router (navigate(it.link)) the /r basename gets added
              automatically; only raw browser navigations need it
              spelled out. */}
          <a
            href={`/r${it.link}`}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="block group text-[11.5px] leading-snug rounded px-1.5 -mx-1.5 py-0.5 hover:bg-muted/50 transition cursor-pointer"
            title="Open this email in a new tab"
          >
            <div className="flex items-baseline gap-2">
              <span className={cn(
                "text-[9px] uppercase tracking-wider shrink-0 px-1 rounded",
                it.direction === "outgoing"
                  ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                  : "bg-muted text-muted-foreground",
              )}>
                {it.direction === "outgoing" ? "sent" : "in"}
              </span>
              <span className="font-medium truncate group-hover:underline">{it.title}</span>
              <ExternalLink className="w-2.5 h-2.5 text-muted-foreground/40 group-hover:text-muted-foreground shrink-0" />
              {it.when && (
                <span className="ml-auto text-[10px] text-muted-foreground/60 tabular-nums shrink-0">
                  {fmtShortDate(it.when)}
                </span>
              )}
            </div>
            {it.sub && (
              <div className="text-[11px] text-muted-foreground line-clamp-1 mt-0.5">{it.sub}</div>
            )}
          </a>
        </li>
      ))}
    </ol>
  );
}


function TriageChip({
  label, shortcut, icon: Icon, tone, active, suggested, onClick,
}: {
  label: string;
  shortcut: string;
  icon: any;
  tone: "emerald" | "sky" | "muted" | "rose";
  active: boolean;
  suggested?: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  // Tailwind v4 JIT requires the full class strings — can't interpolate.
  const toneActive: Record<typeof tone, string> = {
    emerald: "border-emerald-500/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
    sky:     "border-sky-500/60 bg-sky-500/15 text-sky-700 dark:text-sky-300",
    muted:   "border-border bg-muted text-foreground",
    rose:    "border-rose-500/60 bg-rose-500/15 text-rose-700 dark:text-rose-300",
  };
  const toneHover: Record<typeof tone, string> = {
    emerald: "hover:border-emerald-500/40 hover:bg-emerald-500/5 hover:text-emerald-700 dark:hover:text-emerald-300",
    sky:     "hover:border-sky-500/40 hover:bg-sky-500/5 hover:text-sky-700 dark:hover:text-sky-300",
    muted:   "hover:border-border hover:bg-muted hover:text-foreground",
    rose:    "hover:border-rose-500/40 hover:bg-rose-500/5 hover:text-rose-700 dark:hover:text-rose-300",
  };
  return (
    <button
      onClick={onClick}
      title={`${label} (${shortcut})${suggested ? " — Yorik's verdict" : ""}`}
      className={cn(
        "inline-flex items-center gap-1 px-2 py-1.5 rounded-md border text-[11px] font-medium transition min-w-[28px] justify-center",
        active ? toneActive[tone] : `border-border text-muted-foreground ${toneHover[tone]}`,
        suggested && !active && "ring-1 ring-primary/30",
      )}
    >
      <Icon className="w-3 h-3" />
      <span className="hidden sm:inline">{label}</span>
    </button>
  );
}


// Kicks /api/contacts/triage/auto-classify. Polls status while the
// background job runs, shows a progress bar inline, and notifies the
// parent on completion so the TriageModal — if open — can refetch
// rows and pick up the new verdicts. Lives next to the existing
// TriageButton in the contacts toolbar.
function AutoClassifyButton({ onDone, externalKick, onComplete }: {
  onDone: () => Promise<void> | void;
  // Counter the parent bumps whenever someone OTHER than this button
  // kicks the auto-classify (e.g. the "Re-review archived" button
  // chains into classify). When this changes we re-poll status so
  // the inline progress bar picks up the new running state instead
  // of silently sitting idle.
  externalKick?: number;
  // Fired when the classify pass transitions from running → done
  // (regardless of who started it). Parent uses this to auto-open
  // the TriageModal — otherwise the user has to manually click
  // Triage to see the LLM verdicts.
  onComplete?: () => void;
}) {
  const [status, setStatus] = useState<{ status: string; total: number; done: number; last_error?: string | null } | null>(null);
  const [starting, setStarting] = useState(false);

  const refreshStatus = useCallback(async () => {
    try {
      const r = await api.get<{ status: string; total: number; done: number; last_error?: string | null }>(
        "/api/contacts/triage/auto-classify/status",
      );
      setStatus(r);
      return r;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => { refreshStatus(); }, [refreshStatus]);
  // Re-poll whenever an external kick happens — gives this button a
  // chance to switch into "running" display mode without the user
  // having to refresh the page.
  useEffect(() => {
    if (externalKick === undefined) return;
    refreshStatus();
  }, [externalKick, refreshStatus]);

  // Poll status during a running classify so the progress bar advances
  // live. Stop polling once we transition out of running. On the
  // running → done edge, fire onComplete so the parent can auto-open
  // the TriageModal — the whole point of this button is to set up
  // the verdicts; making the user click a second button after seeing
  // 348/348 finish is needless friction.
  useEffect(() => {
    if (status?.status !== "running") return;
    const id = setInterval(async () => {
      const r = await refreshStatus();
      if (r && r.status !== "running") {
        onDone();
        if (r.status === "done") {
          onComplete?.();
        }
      }
    }, 1500);
    return () => clearInterval(id);
  }, [status?.status, refreshStatus, onDone, onComplete]);

  async function start() {
    setStarting(true);
    try {
      await api.post("/api/contacts/triage/auto-classify", {});
      // Tiny delay so the server has time to flip status=running before
      // we poll — otherwise the button briefly shows "Idle" again.
      setTimeout(refreshStatus, 250);
    } catch (e: any) {
      alert("Couldn't start auto-classify: " + (e?.message || e));
    } finally {
      setStarting(false);
    }
  }

  const running = status?.status === "running";
  const pct = status && status.total > 0 ? Math.round((status.done / status.total) * 100) : 0;

  return (
    <div className="hidden md:flex items-center gap-2">
      <button
        onClick={start}
        disabled={starting || running}
        className={cn(
          "text-xs h-8 px-3 rounded-md border transition inline-flex items-center gap-1.5",
          running
            ? "border-primary/40 bg-primary/5 text-primary"
            : "bg-card border-border text-foreground hover:bg-muted",
        )}
        title="Run the LLM over every pending contact and pre-fill triage verdicts"
      >
        {(running || starting) ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
        {running ? `Auto-classifying ${status!.done}/${status!.total}` : "Auto-classify"}
      </button>
      {running && (
        <div className="w-24 h-1.5 rounded-full bg-muted overflow-hidden">
          <div className="h-full bg-primary transition-all duration-500" style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
}


// One-click "move every archived contact back to pending and kick a
// fresh auto-classify." Used after extending the signal collectors
// (e.g. the first triage pass was email-only; a later pass with
// WhatsApp + calendar signals should re-judge the archived rows that
// were classified blind). Shown only when counts.archived > 0.
function ReclassifyArchivedButton({
  archivedCount, onDone,
}: {
  archivedCount: number;
  onDone: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);

  async function run() {
    if (busy) return;
    const ok = window.confirm(
      `Move ${archivedCount} archived contact${archivedCount === 1 ? "" : "s"} back to Pending and re-run Yorik's triage on them? They'll get fresh verdicts based on the current signal collectors.`,
    );
    if (!ok) return;
    setBusy(true);
    let moved = 0;
    let started = false;
    try {
      const r = await api.post<{ moved: number }>(
        "/api/contacts/triage/reclassify-archived", {},
      );
      moved = r.moved || 0;
      // Chain into auto-classify — the only reason to move-archived
      // is to immediately re-judge, so chain the call to save a click.
      if (moved > 0) {
        await api.post("/api/contacts/triage/auto-classify", {});
        started = true;
      }
      // onDone refreshes parent counts AND bumps the AutoClassifyButton
      // kick counter so its progress bar lights up for the new run.
      await onDone();
      // Soft confirmation since there's no toast system here. The
      // AutoClassifyButton handles the live progress display from
      // here on.
      if (started) {
        // Browsers swallow confirms after async; alert is the
        // load-bearing UX feedback that "yes, it's running now,
        // watch the progress bar on the Auto-classify button."
        alert(
          `Moved ${moved} contact${moved === 1 ? "" : "s"} back to Pending. ` +
          `Yorik is re-classifying them now — watch the Auto-classify button for progress.`,
        );
      } else if (moved > 0) {
        alert(`Moved ${moved} contact${moved === 1 ? "" : "s"} back to Pending.`);
      } else {
        alert("Nothing to move.");
      }
    } catch (e: any) {
      alert("Re-classify failed: " + (e?.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      onClick={run}
      disabled={busy}
      className="hidden md:flex text-xs h-8 px-3 rounded-md bg-card border border-border text-foreground hover:bg-muted items-center gap-1.5"
      title={`Move ${archivedCount} archived contacts back to Pending and re-run Yorik's triage with the current signal collectors`}
    >
      {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
      {busy ? "Re-classifying…" : `Re-review ${archivedCount} archived`}
    </button>
  );
}


// ─── DedupeButton — LLM-assisted dedupe with per-group review ──────────
// Click → calls /api/contacts/dedupe-llm with dry_run:true. The endpoint
// pre-clusters pending contacts by aggressive-normalised name and asks
// the LLM to pick a canonical id per cluster using each row's channels
// + addresses as context. The returned plan opens in a modal where the
// user can uncheck any merge they don't trust (e.g. "Landkreis München
// + Jobcenter" — same district but conceptually distinct) before the
// apply call fires with the filtered plan.

interface DedupeSourceDoc {
  paperless_doc_id?: number | null;
  iban?: string | null;
  tax_id?: string | null;
  document_summary?: string | null;
}
interface DedupePlanMember {
  id: number;
  kind: string | null;
  name: string;
  channels: { kind: string; value: string }[];
  addresses: { line1?: string; postcode?: string; city?: string }[];
  source_documents?: DedupeSourceDoc[];
}
interface DedupeMergeGroup {
  canonical_id: number;
  member_ids: number[];
  reason: string;
  confidence: "high" | "medium";
  members: DedupePlanMember[];
}
interface DedupeSkipGroup {
  ids: number[];
  reason: string;
  members: DedupePlanMember[];
}
interface DedupePlan {
  merge: DedupeMergeGroup[];
  skip: DedupeSkipGroup[];
  stats: Record<string, number>;
  paperless_base_url?: string;
}

// Stable key for a skip group — sorted member ids joined. Survives
// re-fetches as long as the same ids are in the same skip group.
function skipKey(s: DedupeSkipGroup): string {
  return s.ids.slice().sort((a, b) => a - b).join(",");
}

// Pick a default canonical for a skip group the user is overriding:
// member with most (channels + addresses), ties broken by lowest id.
function pickDefaultCanonical(members: DedupePlanMember[]): number {
  let best = members[0];
  let bestScore = (best.channels.length + best.addresses.length);
  for (const m of members.slice(1)) {
    const score = m.channels.length + m.addresses.length;
    if (score > bestScore || (score === bestScore && m.id < best.id)) {
      best = m;
      bestScore = score;
    }
  }
  return best.id;
}

// Dedupe modal state machine.
//   closed   → no modal
//   chooser  → "pick what to dedupe" screen
//   loading  → LLM is building the plan; chooser is replaced by a
//              progress card so the user knows it's working
//   review   → plan is in, the user can pick which merges to apply
type DedupeState = "closed" | "chooser" | "loading" | "review";

function DedupeButton({
  onDone,
  status = "pending",
}: {
  onDone: () => Promise<void> | void;
  /** Which status bucket to dedupe. Defaults to 'pending' — the
   *  original use case (collapse before promoting). Pass 'active' to
   *  clean up duplicates that slipped through (Anthropic-x8, Amazon-x4)
   *  via the Active tab's "Dedupe actives" button. */
  status?: "pending" | "active";
}) {
  const [state, setState] = useState<DedupeState>("closed");
  const [plan, setPlan] = useState<DedupePlan | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Skip groups the user overrode — keyed by stable skipKey().
  const [overriddenSkips, setOverriddenSkips] = useState<Set<string>>(new Set());
  // Groups (merge + skip) the user marked as "not real contacts" — all
  // member rows get status='spam' on apply. Mutually exclusive with
  // selected / overriddenSkips: choosing one clears the other for that
  // group, so the modal can't ask to merge AND dismiss the same cluster.
  const [dismissedMerge, setDismissedMerge] = useState<Set<number>>(new Set());
  const [dismissedSkip, setDismissedSkip] = useState<Set<string>>(new Set());
  // User override of the canonical row for a merge group. Keyed by the
  // LLM's original canonical_id; value is the user-picked id. Allows
  // the user to fix cases where the LLM picked the wrong row (e.g.
  // chose the row with one phone over the row with three).
  const [customCanonicals, setCustomCanonicals] = useState<Map<number, number>>(new Map());
  // Same idea for overridden skip groups — keyed by skipKey().
  const [skipCanonicals, setSkipCanonicals] = useState<Map<string, number>>(new Map());
  const [kind, setKind] = useState<"business" | "person" | null>(null);
  const [applying, setApplying] = useState(false);
  const [counts, setCounts] = useState<{ business: number; person: number } | null>(null);

  async function openChooser() {
    setState("chooser");
    setPlan(null);
    setSelected(new Set());
    setOverriddenSkips(new Set());
    // Fetch counts so the chooser buttons can say "Businesses (491)".
    // Cheap call (~10ms) and only on chooser open; we don't preload.
    try {
      const r = await api.get<{ pending: number }>("/api/contacts/_counts");
      const both = await Promise.all([
        api.get<{ id: number }[]>(`/api/contacts?status=${status}&kind=business&limit=1`),
        api.get<{ id: number }[]>(`/api/contacts?status=${status}&kind=person&limit=1`),
        api.get<{ id: number }[]>(`/api/contacts?status=${status}&limit=2000`),
      ]).then(([_b, _p, all]) => {
        // Count by kind from a single-page fetch — limit=2000 should
        // comfortably cover any household's pending pile.
        let bi = 0, pe = 0;
        for (const c of all as any[]) {
          if (c.kind === "business") bi++;
          else if (c.kind === "person") pe++;
        }
        return { business: bi, person: pe };
      });
      setCounts(both);
    } catch {
      setCounts(null);  // chooser still renders; just without counts
    }
  }

  async function startPlan(forKind: "business" | "person" | null) {
    setKind(forKind);
    setState("loading");
    try {
      const r = await api.post<DedupePlan>("/api/contacts/dedupe-llm", {
        status,
        kind: forKind,
        dry_run: true,
      });
      // Default: pre-select high-confidence groups, leave medium for opt-in.
      const auto = new Set<number>(
        r.merge.filter(g => g.confidence === "high").map(g => g.canonical_id),
      );
      setSelected(auto);
      setOverriddenSkips(new Set());
      setDismissedMerge(new Set());
      setDismissedSkip(new Set());
      setCustomCanonicals(new Map());
      setSkipCanonicals(new Map());
      setPlan(r);
      setState("review");
    } catch (err: any) {
      alert(`Plan failed: ${err?.message || err}`);
      setState("closed");
    }
  }

  async function apply() {
    if (!plan) return;
    setApplying(true);
    try {
      const filteredMerge = plan.merge
        .filter(g => selected.has(g.canonical_id))
        .map(g => {
          // Honour user's canonical override if set.
          const canonical_id = customCanonicals.get(g.canonical_id) ?? g.canonical_id;
          return { ...g, canonical_id };
        });
      // Synthesise merge groups from overridden skips. Honour user's
      // canonical pick per skip group, else pick the most-complete row.
      const synth = plan.skip
        .filter(s => overriddenSkips.has(skipKey(s)))
        .map(s => {
          const key = skipKey(s);
          const canonical_id = skipCanonicals.get(key) ?? pickDefaultCanonical(s.members);
          return {
            canonical_id,
            member_ids: s.ids,
            reason: `MANUAL OVERRIDE: ${s.reason}`,
            confidence: "manual" as const,
          };
        });

      const allMerges = [...filteredMerge, ...synth];

      // Collect dismissed contact ids — flatten all member ids from
      // merge groups & skip groups the user flagged as "not real".
      // Server marks them status='spam' (no merge, no delete).
      const dismissIds: number[] = [];
      for (const g of plan.merge) {
        if (dismissedMerge.has(g.canonical_id)) dismissIds.push(...g.member_ids);
      }
      for (const s of plan.skip) {
        if (dismissedSkip.has(skipKey(s))) dismissIds.push(...s.ids);
      }

      const r = await api.post<{
        applied_groups: number; deleted_contacts: number;
        channels_moved: number; addresses_moved: number;
        employer_refs_repointed: number;
        dismissed_contacts?: number;
      }>("/api/contacts/dedupe-llm", {
        dry_run: false,
        plan: { merge: allMerges, dismiss: dismissIds },
      });
      const dismissedLine = r.dismissed_contacts
        ? `\n${r.dismissed_contacts} contact${r.dismissed_contacts === 1 ? "" : "s"} dismissed as spam.`
        : "";
      alert(
        `Applied ${r.applied_groups} merges` +
        (synth.length ? ` (${synth.length} manual)` : "") + `.\n` +
        `${r.deleted_contacts} contacts removed · ` +
        `${r.channels_moved} channels moved · ` +
        `${r.addresses_moved} addresses moved · ` +
        `${r.employer_refs_repointed} employer refs re-pointed.` +
        dismissedLine,
      );
      setPlan(null);
      setState("closed");
      await onDone();
    } catch (err: any) {
      alert(`Apply failed: ${err?.message || err}`);
    } finally {
      setApplying(false);
    }
  }

  function close() {
    setState("closed");
    setPlan(null);
  }

  function toggle(canonical_id: number) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(canonical_id)) next.delete(canonical_id);
      else next.add(canonical_id);
      return next;
    });
    // Merging excludes dismissing — clear any dismiss flag for this group.
    setDismissedMerge(prev => {
      if (!prev.has(canonical_id)) return prev;
      const next = new Set(prev); next.delete(canonical_id); return next;
    });
  }

  function toggleSkip(key: string) {
    setOverriddenSkips(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    setDismissedSkip(prev => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev); next.delete(key); return next;
    });
  }

  function toggleDismissMerge(canonical_id: number) {
    setDismissedMerge(prev => {
      const next = new Set(prev);
      if (next.has(canonical_id)) next.delete(canonical_id);
      else next.add(canonical_id);
      return next;
    });
    // Dismissing excludes merging — clear merge selection too.
    setSelected(prev => {
      if (!prev.has(canonical_id)) return prev;
      const next = new Set(prev); next.delete(canonical_id); return next;
    });
  }

  function toggleDismissSkip(key: string) {
    setDismissedSkip(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    setOverriddenSkips(prev => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev); next.delete(key); return next;
    });
  }

  function setCustomCanonical(originalCanonical: number, memberId: number) {
    setCustomCanonicals(prev => {
      const next = new Map(prev);
      next.set(originalCanonical, memberId);
      return next;
    });
  }

  function setSkipCanonical(key: string, memberId: number) {
    setSkipCanonicals(prev => {
      const next = new Map(prev);
      next.set(key, memberId);
      return next;
    });
  }

  function selectAll() {
    if (!plan) return;
    setSelected(new Set(plan.merge.map(g => g.canonical_id)));
    // Selecting clears any conflicting dismiss flags.
    setDismissedMerge(new Set());
  }
  function selectNone() { setSelected(new Set()); }

  function dismissAll() {
    if (!plan) return;
    // Bulk path: flag every merge group + every skip group for
    // dismissal. Used when the user knows the whole modal is
    // signature-cascade noise (board members on form letters etc.)
    // and just wants to nuke it. Clears any merge/override selections
    // because dismiss is mutually exclusive with them.
    setDismissedMerge(new Set(plan.merge.map(g => g.canonical_id)));
    setDismissedSkip(new Set(plan.skip.map(s => skipKey(s))));
    setSelected(new Set());
    setOverriddenSkips(new Set());
  }

  function clearAllSelections() {
    setSelected(new Set());
    setOverriddenSkips(new Set());
    setDismissedMerge(new Set());
    setDismissedSkip(new Set());
  }

  return (
    <>
      <button
        onClick={openChooser}
        disabled={state === "loading"}
        className="hidden md:flex text-xs h-8 px-3 rounded-md bg-card border border-border text-foreground hover:bg-muted items-center gap-1.5 disabled:opacity-50"
        title="LLM-assisted dedupe — review groups before applying"
      >
        {state === "loading" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <UsersRound className="w-3.5 h-3.5" />}
        Dedupe
      </button>

      {state === "chooser" && createPortal(
        <DedupeChooserModal
          counts={counts}
          onPick={startPlan}
          onCancel={close}
          status={status}
        />,
        document.body,
      )}

      {state === "loading" && createPortal(
        <DedupeLoadingModal kind={kind} />,
        document.body,
      )}

      {state === "review" && plan && createPortal(
        <DedupeReviewModal
          plan={plan}
          kind={kind}
          selected={selected}
          overriddenSkips={overriddenSkips}
          dismissedMerge={dismissedMerge}
          dismissedSkip={dismissedSkip}
          customCanonicals={customCanonicals}
          skipCanonicals={skipCanonicals}
          onToggle={toggle}
          onToggleSkip={toggleSkip}
          onToggleDismissMerge={toggleDismissMerge}
          onToggleDismissSkip={toggleDismissSkip}
          onPickCanonical={setCustomCanonical}
          onPickSkipCanonical={setSkipCanonical}
          onSelectAll={selectAll}
          onSelectNone={selectNone}
          onDismissAll={dismissAll}
          onClearAll={clearAllSelections}
          onSwitchKind={(k) => { setPlan(null); startPlan(k); }}
          onCancel={close}
          onApply={apply}
          applying={applying}
        />,
        document.body,
      )}
    </>
  );
}


function DedupeChooserModal({
  counts, onPick, onCancel, status = "pending",
}: {
  counts: { business: number; person: number } | null;
  onPick: (kind: "business" | "person") => void;
  onCancel: () => void;
  status?: "pending" | "active";
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={onCancel}
    >
      <div
        className="w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div>
            <div className="font-semibold">Dedupe {status} contacts</div>
            <div className="text-xs text-muted-foreground mt-0.5">
              Pick which group of {status} contacts to analyse. The LLM scans them and proposes safe merges.
            </div>
          </div>
          <button onClick={onCancel} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="p-5 space-y-3">
          <button
            onClick={() => onPick("business")}
            className="w-full flex items-center gap-3 p-4 rounded-lg border border-border hover:bg-muted/30 hover:border-primary/30 transition text-left"
          >
            <div className="w-9 h-9 rounded-full bg-blue-500/15 text-blue-500 flex items-center justify-center shrink-0">
              <Briefcase className="w-4 h-4" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="font-medium text-sm">Businesses</div>
              <div className="text-[12px] text-muted-foreground">
                {counts ? `${counts.business} ${status}` : "loading…"} · ~15 s
              </div>
            </div>
            <ChevronRight className="w-4 h-4 text-muted-foreground" />
          </button>

          <button
            onClick={() => onPick("person")}
            className="w-full flex items-center gap-3 p-4 rounded-lg border border-border hover:bg-muted/30 hover:border-primary/30 transition text-left"
          >
            <div className="w-9 h-9 rounded-full bg-amber-500/15 text-amber-500 flex items-center justify-center shrink-0">
              <UsersRound className="w-4 h-4" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="font-medium text-sm">Persons</div>
              <div className="text-[12px] text-muted-foreground">
                {counts ? `${counts.person} ${status}` : "loading…"} · ~1 min
              </div>
            </div>
            <ChevronRight className="w-4 h-4 text-muted-foreground" />
          </button>

          <div className="text-[11px] text-muted-foreground italic pt-2">
            You can switch to the other kind from inside the review modal.
          </div>
        </div>
      </div>
    </div>
  );
}


function DedupeLoadingModal({ kind }: { kind: "business" | "person" | null }) {
  const label = kind === "business" ? "businesses" : kind === "person" ? "persons" : "contacts";
  const [progress, setProgress] = useState<{ current: number; total: number; label: string }>(
    { current: 0, total: 0, label: "" },
  );

  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      try {
        const p = await api.get<{ current: number; total: number; label: string; done: boolean }>(
          "/api/contacts/dedupe-llm/progress",
        );
        if (stopped) return;
        if (!p.done && p.total > 0) {
          setProgress({ current: p.current, total: p.total, label: p.label || "" });
        }
      } catch { /* polling failures are silent — modal already shows we're working */ }
    };
    tick();
    const id = window.setInterval(tick, 700);
    return () => { stopped = true; window.clearInterval(id); };
  }, []);

  const pct = progress.total > 0
    ? Math.min(100, Math.round((progress.current / progress.total) * 100))
    : null;

  return (
    <div className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4">
      <div className="w-full max-w-sm bg-card border border-border rounded-2xl shadow-2xl overflow-hidden p-6">
        <div className="flex items-center gap-2.5 mb-3">
          <Loader2 className="w-5 h-5 animate-spin text-primary shrink-0" />
          <div className="font-semibold">Analysing {label}…</div>
        </div>

        {pct !== null ? (
          <>
            <div className="h-1.5 bg-muted rounded-full overflow-hidden mb-2">
              <div
                className="h-full bg-primary transition-all duration-500 ease-out"
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="flex items-center justify-between text-[11px] text-muted-foreground mb-3">
              <span>{progress.current} / {progress.total} clusters</span>
              <span>{pct}%</span>
            </div>
            {progress.label && (
              <div className="text-[11px] text-muted-foreground truncate font-mono mb-2">
                {progress.label}
              </div>
            )}
          </>
        ) : (
          <div className="text-[11px] text-muted-foreground mb-3">
            Pre-clustering by name…
          </div>
        )}

        <div className="text-[11px] text-muted-foreground/80 leading-relaxed">
          The LLM reviews each candidate group using channels, addresses and source documents.
        </div>
      </div>
    </div>
  );
}

function DedupeReviewModal({
  plan, kind, selected, overriddenSkips, dismissedMerge, dismissedSkip,
  customCanonicals, skipCanonicals,
  onToggle, onToggleSkip, onToggleDismissMerge, onToggleDismissSkip,
  onPickCanonical, onPickSkipCanonical,
  onSelectAll, onSelectNone, onDismissAll, onClearAll,
  onSwitchKind, onCancel, onApply, applying,
}: {
  plan: DedupePlan;
  kind: "business" | "person" | null;
  selected: Set<number>;
  overriddenSkips: Set<string>;
  dismissedMerge: Set<number>;
  dismissedSkip: Set<string>;
  customCanonicals: Map<number, number>;
  skipCanonicals: Map<string, number>;
  onToggle: (canonical_id: number) => void;
  onToggleSkip: (key: string) => void;
  onToggleDismissMerge: (canonical_id: number) => void;
  onToggleDismissSkip: (key: string) => void;
  onPickCanonical: (originalCanonical: number, memberId: number) => void;
  onPickSkipCanonical: (key: string, memberId: number) => void;
  onSelectAll: () => void;
  onSelectNone: () => void;
  onDismissAll: () => void;
  onClearAll: () => void;
  onSwitchKind: (k: "business" | "person") => void;
  onCancel: () => void;
  onApply: () => void;
  applying: boolean;
}) {
  const paperlessBase = plan.paperless_base_url || "";
  // Build a Paperless deep-link URL for a member's first source doc.
  // Paperless-ngx URL convention: /documents/{id}/details/
  function paperlessUrlFor(m: DedupePlanMember): string | null {
    const sd = m.source_documents?.find(d => d.paperless_doc_id);
    if (!sd?.paperless_doc_id || !paperlessBase) return null;
    return `${paperlessBase}/documents/${sd.paperless_doc_id}/details/`;
  }
  const [showSkipped, setShowSkipped] = useState(false);
  const selectedDeletions = plan.merge
    .filter(g => selected.has(g.canonical_id))
    .reduce((n, g) => n + g.member_ids.length - 1, 0);
  const overrideDeletions = plan.skip
    .filter(s => overriddenSkips.has(skipKey(s)))
    .reduce((n, s) => n + s.ids.length - 1, 0);
  const dismissedCount = plan.merge
    .filter(g => dismissedMerge.has(g.canonical_id))
    .reduce((n, g) => n + g.member_ids.length, 0)
    + plan.skip
      .filter(s => dismissedSkip.has(skipKey(s)))
      .reduce((n, s) => n + s.ids.length, 0);
  const totalSelected = selected.size + overriddenSkips.size + dismissedMerge.size + dismissedSkip.size;
  const totalDeletions = selectedDeletions + overrideDeletions;

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !applying) onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [applying, onCancel]);

  return (
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={applying ? undefined : onCancel}
    >
      <div
        className="w-full max-w-3xl max-h-[90vh] bg-card border border-border rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between gap-3">
          <div>
            <div className="font-semibold">Dedupe review — {kind ?? "pending"}</div>
            <div className="text-xs text-muted-foreground mt-0.5">
              {plan.merge.length} merge groups · {plan.skip.length} skipped ·
              {" "}{totalSelected} selected ({totalDeletions} merged away{dismissedCount > 0 ? `, ${dismissedCount} dismissed` : ""})
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onSwitchKind(kind === "business" ? "person" : "business")}
              disabled={applying}
              className="text-[11px] px-2 py-1 rounded border border-border hover:bg-muted text-muted-foreground hover:text-foreground"
              title="Run the plan on the other kind"
            >
              {kind === "business" ? "Persons →" : "Businesses →"}
            </button>
            <button
              onClick={onCancel}
              disabled={applying}
              className="p-1.5 hover:bg-muted rounded-md text-muted-foreground disabled:opacity-50"
              aria-label="Close"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </header>

        <div className="px-5 py-2 border-b border-border flex items-center flex-wrap gap-x-4 gap-y-1 text-[11px]">
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground/70 uppercase tracking-wider text-[10px]">Merge:</span>
            <button onClick={onSelectAll} className="text-primary hover:underline">All</button>
            <span className="text-muted-foreground/40">·</span>
            <button onClick={onSelectNone} className="text-muted-foreground hover:underline">None</button>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground/70 uppercase tracking-wider text-[10px]">Dismiss:</span>
            <button
              onClick={() => {
                if (confirm(
                  `Mark all ${plan.merge.length + plan.skip.length} groups as 'not a contact'?\n\n` +
                  `Every row in this view will be set to spam status when you click Apply. ` +
                  `Future scans won't surface them again. ` +
                  `(You can still un-dismiss specific groups before applying.)`
                )) onDismissAll();
              }}
              className="text-rose-600 dark:text-rose-400 hover:underline"
              title="Flag every group in this view as spam (review before applying)"
            >
              All as spam
            </button>
            <span className="text-muted-foreground/40">·</span>
            <button onClick={onClearAll} className="text-muted-foreground hover:underline">
              Clear
            </button>
          </div>
          <span className="ml-auto text-muted-foreground text-right">
            High-confidence pre-selected. Medium & Skip left untouched.
          </span>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
          {plan.merge.length === 0 && (
            <div className="text-sm text-muted-foreground italic text-center py-8">
              No merge candidates — the LLM didn't find anything to merge.
            </div>
          )}

          {plan.merge.map(g => {
            const isSel = selected.has(g.canonical_id);
            const isDis = dismissedMerge.has(g.canonical_id);
            return (
              <label
                key={g.canonical_id}
                className={cn(
                  "flex gap-3 p-3 rounded-lg border cursor-pointer transition",
                  isDis
                    ? "bg-rose-500/5 border-rose-500/40"
                    : isSel
                      ? "bg-primary/5 border-primary/40"
                      : "bg-card border-border hover:bg-muted/30",
                )}
              >
                <input
                  type="checkbox"
                  checked={isSel}
                  onChange={() => onToggle(g.canonical_id)}
                  disabled={isDis}
                  className="mt-1 shrink-0 accent-primary disabled:opacity-30"
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs font-medium">
                      {isDis ? `Dismiss ${g.member_ids.length} as spam` : `Merge ${g.member_ids.length} → 1`}
                    </span>
                    <span className={cn(
                      "text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded",
                      isDis
                        ? "bg-rose-500/10 text-rose-700 dark:text-rose-400"
                        : g.confidence === "high"
                          ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                          : "bg-amber-500/10 text-amber-700 dark:text-amber-400",
                    )}>
                      {isDis ? "dismiss" : g.confidence}
                    </span>
                    <button
                      type="button"
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); onToggleDismissMerge(g.canonical_id); }}
                      className={cn(
                        "ml-auto text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border transition",
                        isDis
                          ? "border-rose-500/60 text-rose-700 dark:text-rose-400 bg-rose-500/5"
                          : "border-border text-muted-foreground hover:text-rose-600 hover:border-rose-400/40",
                      )}
                      title={isDis ? "Cancel dismiss" : "These aren't real contacts — mark all as spam"}
                    >
                      <Trash2 className="w-3 h-3 inline mr-1 -mt-0.5" />
                      {isDis ? "Undo" : "Not a contact"}
                    </button>
                  </div>
                  <div className="mt-1.5 text-[12px] text-muted-foreground leading-relaxed">
                    {g.reason}
                  </div>
                  <ul className="mt-2 space-y-1.5">
                    {g.members.map(m => {
                      const activeCanonical = customCanonicals.get(g.canonical_id) ?? g.canonical_id;
                      const isCanon = m.id === activeCanonical;
                      const chans = m.channels.map(c => `${c.kind}=${c.value}`).join(", ");
                      const addr = m.addresses[0];
                      const addrStr = addr
                        ? [addr.line1, addr.postcode, addr.city].filter(Boolean).join(", ")
                        : "";
                      const pUrl = paperlessUrlFor(m);
                      return (
                        <li key={m.id} className="text-[12px] flex items-start gap-2">
                          <button
                            type="button"
                            onClick={(e) => { e.preventDefault(); e.stopPropagation(); onPickCanonical(g.canonical_id, m.id); }}
                            className={cn(
                              "shrink-0 w-4 h-4 inline-flex items-center justify-center mt-0.5 rounded transition",
                              isCanon ? "text-amber-500" : "text-muted-foreground/40 hover:text-amber-500/70",
                            )}
                            title={isCanon ? "This row is the canonical (data from the others merges into it)" : "Click to make this the canonical row"}
                            aria-label={isCanon ? "Canonical row" : "Pick as canonical"}
                          >
                            ★
                          </button>
                          <div className="min-w-0 flex-1">
                            <span className="font-mono text-[10px] text-muted-foreground/70">#{m.id}</span>
                            <span className="font-medium ml-2">{m.name}</span>
                            {pUrl && (
                              <a
                                href={pUrl}
                                target="_blank"
                                rel="noopener noreferrer"
                                onClick={(e) => e.stopPropagation()}
                                className="ml-1.5 inline-flex items-center text-primary/70 hover:text-primary"
                                title="Open source document in Paperless"
                              >
                                <FileText className="w-3 h-3" />
                              </a>
                            )}
                            {chans && (
                              <span className="text-muted-foreground"> · {chans}</span>
                            )}
                            {addrStr && (
                              <span className="text-muted-foreground/70 block ml-0 mt-0.5 text-[11px]">{addrStr}</span>
                            )}
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              </label>
            );
          })}

          {plan.skip.length > 0 && (
            <details className="rounded-lg border border-border bg-muted/20" open={showSkipped} onToggle={(e) => setShowSkipped((e.target as HTMLDetailsElement).open)}>
              <summary className="cursor-pointer p-3 text-sm font-medium select-none">
                Skipped by the LLM ({plan.skip.length}) — check any you want to merge anyway
                {overriddenSkips.size > 0 && (
                  <span className="ml-2 text-[11px] text-amber-600 dark:text-amber-400">
                    · {overriddenSkips.size} overridden
                  </span>
                )}
              </summary>
              <div className="px-3 pb-3 space-y-2">
                <div className="text-[11px] text-muted-foreground italic px-1 py-1">
                  These groups have similar names but the LLM was unsure (different addresses, tax IDs, or no source signal).
                  Check any you know are the same entity — Yorik picks the most-complete row as canonical.
                </div>
                {plan.skip.map((s, i) => {
                  const key = skipKey(s);
                  const isOverridden = overriddenSkips.has(key);
                  const isDis = dismissedSkip.has(key);
                  const activeCanonical = skipCanonicals.get(key) ?? pickDefaultCanonical(s.members);
                  return (
                    <div
                      key={i}
                      className={cn(
                        "flex gap-3 text-[12px] p-2 rounded border transition",
                        isDis
                          ? "bg-rose-500/5 border-rose-500/40"
                          : isOverridden
                            ? "bg-amber-500/5 border-amber-500/40"
                            : "bg-card border-border",
                      )}
                    >
                      <input
                        type="checkbox"
                        checked={isOverridden}
                        onChange={() => onToggleSkip(key)}
                        disabled={isDis}
                        className="mt-0.5 shrink-0 accent-amber-500 cursor-pointer disabled:opacity-30"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-start gap-2 mb-1">
                          <div className="text-muted-foreground italic flex-1">{s.reason}</div>
                          <button
                            type="button"
                            onClick={(e) => { e.preventDefault(); e.stopPropagation(); onToggleDismissSkip(key); }}
                            className={cn(
                              "shrink-0 text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border transition",
                              isDis
                                ? "border-rose-500/60 text-rose-700 dark:text-rose-400 bg-rose-500/5"
                                : "border-border text-muted-foreground hover:text-rose-600 hover:border-rose-400/40",
                            )}
                            title={isDis ? "Cancel dismiss" : "Mark all rows in this group as spam"}
                          >
                            <Trash2 className="w-3 h-3 inline mr-1 -mt-0.5" />
                            {isDis ? "Undo" : "Not a contact"}
                          </button>
                        </div>
                        {s.members.map(m => {
                          const isCanon = m.id === activeCanonical;
                          const chans = m.channels.map(c => `${c.kind}=${c.value}`).join(", ");
                          const addr = m.addresses[0];
                          const addrStr = addr
                            ? [addr.line1, addr.postcode, addr.city].filter(Boolean).join(", ")
                            : "";
                          const pUrl = paperlessUrlFor(m);
                          return (
                            <div key={m.id} className="text-[11.5px] flex items-start gap-1.5">
                              <button
                                type="button"
                                onClick={(e) => { e.preventDefault(); e.stopPropagation(); onPickSkipCanonical(key, m.id); }}
                                disabled={!isOverridden}
                                className={cn(
                                  "shrink-0 w-3.5 inline-flex justify-center rounded transition",
                                  isOverridden && isCanon && "text-amber-500",
                                  isOverridden && !isCanon && "text-muted-foreground/30 hover:text-amber-500/70",
                                  !isOverridden && "text-muted-foreground/20 cursor-default",
                                )}
                                title={
                                  !isOverridden
                                    ? "Check the group first to pick a canonical"
                                    : isCanon ? "This row will be the canonical"
                                    : "Click to make this the canonical"
                                }
                              >
                                ★
                              </button>
                              <div className="min-w-0 flex-1">
                                <span className="font-mono text-[10px] text-muted-foreground/70">#{m.id}</span>
                                <span className="ml-2">{m.name}</span>
                                {pUrl && (
                                  <a
                                    href={pUrl}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    onClick={(e) => e.stopPropagation()}
                                    className="ml-1.5 inline-flex items-center text-primary/70 hover:text-primary"
                                    title="Open source document in Paperless"
                                  >
                                    <FileText className="w-3 h-3" />
                                  </a>
                                )}
                                {chans && <span className="text-muted-foreground/80"> · {chans}</span>}
                                {addrStr && (
                                  <span className="text-muted-foreground/60 block text-[10.5px]">{addrStr}</span>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>
            </details>
          )}
        </div>

        <footer className="px-5 py-4 border-t border-border flex items-center gap-2">
          <button
            onClick={onCancel}
            disabled={applying}
            className="px-3 py-2 rounded-md text-sm font-medium border border-border hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-50"
          >
            Cancel
          </button>
          <div className="ml-auto text-[11px] text-muted-foreground">
            Merges are irreversible. Channels and addresses move to the canonical row; non-canonical rows are deleted.
          </div>
          <button
            onClick={onApply}
            disabled={applying || totalSelected === 0}
            className="px-4 py-2 rounded-md text-sm font-medium bg-primary text-primary-foreground hover:bg-primary/90 transition inline-flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {applying && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            {applying ? "Applying…" : `Apply ${totalSelected} change${totalSelected === 1 ? "" : "s"}`}
          </button>
        </footer>
      </div>
    </div>
  );
}


// ─── ExtractConfirmModal — replacement for the browser confirm() ──────
// Yorik-styled card-on-backdrop dialog. Same explanation copy the old
// confirm() carried, plus an inline error slot for any /run failure
// (network blip, 503, etc.) so we never resort to the system alert()
// which doesn't match anything else in the UI. Cancel is disabled
// while the request is in flight to prevent leaving a half-started
// scan behind.

function ExtractConfirmModal({
  busy, error, onCancel, onConfirm,
}: {
  busy:      boolean;
  error:     string | null;
  onCancel:  () => void;
  onConfirm: () => void;
}) {
  // Esc cancels (unless we're already mid-request).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !busy) onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onCancel]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
      onClick={() => { if (!busy) onCancel(); }}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl max-w-lg w-full p-6"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="flex items-start gap-3 mb-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-pink-500/30 to-violet-500/30 flex items-center justify-center shrink-0">
            <FileText className="w-5 h-5 text-pink-500" />
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-lg font-semibold leading-tight">Scan documents for contacts</h2>
            <p className="text-xs text-muted-foreground mt-0.5">Runs in the background — safe to leave this page.</p>
          </div>
          <button
            onClick={() => { if (!busy) onCancel(); }}
            disabled={busy}
            className="text-muted-foreground hover:text-foreground transition disabled:opacity-40"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <p className="text-sm leading-relaxed">
          Yorik will walk every Paperless document and propose contacts.
        </p>
        <ul className="text-xs text-muted-foreground mt-3 space-y-1.5 list-disc pl-5">
          <li>Regex pass: IBAN, email, phone, tax-ID — fast, deterministic.</li>
          <li>One short LLM call per doc for the sender block (name, business, address).</li>
          <li>First full pass over a large archive can take 1-3 hours on a local LLM.</li>
          <li>You can stop at any time — partial progress is kept.</li>
        </ul>
        <div className="mt-4 rounded-md bg-pink-500/10 border border-pink-500/30 px-3 py-2 text-xs text-pink-700 dark:text-pink-300">
          <strong>Where to find the results:</strong>{" "}
          <span className="text-pink-700/90 dark:text-pink-300/90">
            The Pending tab on this page. Each contact lands with
            its address, emails and phones already attached — open
            it, tweak whatever needs tweaking, and promote to Active.
            The button above shows a live counter while the scan runs
            and click-jumps you straight into the Pending tab.
          </span>
        </div>

        {error && (
          <div className="mt-4 text-xs text-red-600 dark:text-red-400 bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
            <AlertTriangle className="w-3.5 h-3.5 inline mr-1.5 -mt-0.5" />
            {error}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-6">
          <button
            onClick={onCancel}
            disabled={busy}
            className="text-sm h-9 px-4 rounded-md border border-border text-foreground hover:bg-muted transition disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={busy}
            className="text-sm h-9 px-4 rounded-md bg-pink-500 hover:bg-pink-600 text-white transition disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileText className="w-3.5 h-3.5" />}
            Start scan
          </button>
        </div>
      </div>
    </div>
  );
}


function DetailsBlock({ contact }: { contact: Contact }) {
  const channelGroups: Array<{ label: string; items: typeof contact.channels }> = [
    { label: "Email",    items: contact.channels.filter(c => c.kind === "email") },
    { label: "Phone",    items: contact.channels.filter(c => c.kind === "phone") },
    { label: "WhatsApp", items: contact.channels.filter(c => c.kind === "whatsapp") },
    { label: "Other",    items: contact.channels.filter(c =>
      !["email", "phone", "whatsapp"].includes(c.kind)) },
  ].filter(g => g.items.length > 0);

  const hasAnything = channelGroups.length > 0
    || contact.addresses.length > 0
    || !!contact.notes;
  if (!hasAnything) return null;

  return (
    <section className="border-t border-border pt-4 space-y-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
        Details
      </div>
      {channelGroups.map(g => (
        <div key={g.label}>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground/80 mb-0.5">{g.label}</div>
          {g.items.map(ch => {
            // Wrap actionable channel values in tel: / mailto: / WA
            // links so mobile users get one-tap dial / compose. text-sm
            // on mobile so phone numbers are readable + touch-friendly;
            // text-xs preserved on desktop.
            const href = ch.kind === "phone" ? `tel:${ch.value}`
                       : ch.kind === "email" ? `mailto:${ch.value}`
                       : ch.kind === "whatsapp"
                         ? `https://wa.me/${ch.value.replace(/[^\d]/g, "")}`
                         : undefined;
            const valueEl = (
              <span className="font-medium break-all">{ch.value}</span>
            );
            return (
              <div key={ch.id} className="text-sm md:text-xs flex items-center gap-2 py-0.5">
                {href
                  ? <a href={href} className="text-primary hover:underline">{valueEl}</a>
                  : valueEl}
                {ch.label && <span className="text-muted-foreground text-xs">({ch.label})</span>}
              </div>
            );
          })}
        </div>
      ))}
      {contact.addresses.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground/80 mb-0.5 flex items-center gap-1">
            <MapPin className="w-2.5 h-2.5" /> Addresses
          </div>
          {contact.addresses.map(a => {
            const addr = [a.line1, a.line2, [a.postcode, a.city].filter(Boolean).join(" "), a.country]
              .filter(Boolean).join(", ");
            // geo: links open Maps on iOS/Android; on desktop they
            // fall through to the default handler (usually nothing,
            // so we render plain text to avoid a dead-link look).
            return (
              <div key={a.id} className="text-sm md:text-xs py-0.5">
                <a
                  href={`https://maps.google.com/?q=${encodeURIComponent(addr)}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary hover:underline"
                >
                  {addr}
                </a>
                {a.kind && <span className="ml-2 text-muted-foreground text-xs">({a.kind})</span>}
              </div>
            );
          })}
        </div>
      )}
      {contact.notes && (
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground/80 mb-1">Notes</div>
          <div className="text-xs whitespace-pre-wrap leading-relaxed text-foreground/90">
            {renderNotesWithDocLinks(contact.notes)}
          </div>
        </div>
      )}
      {contact.kind === "business" && (contact.legal_name || contact.tax_id || contact.iban) && (
        <div className="rounded-md border border-blue-500/20 bg-blue-500/[0.04] p-2 space-y-0.5">
          {contact.legal_name && <div className="text-xs"><span className="text-muted-foreground">Legal:</span> {contact.legal_name}</div>}
          {contact.tax_id     && <div className="text-xs"><span className="text-muted-foreground">Tax ID:</span> {contact.tax_id}</div>}
          {contact.iban       && <div className="text-xs"><span className="text-muted-foreground">IBAN:</span> {contact.iban}</div>}
        </div>
      )}
    </section>
  );
}


function fmtShortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  const y = new Date(now); y.setDate(y.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "yesterday";
  if (now.getFullYear() === d.getFullYear()) {
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  return d.toLocaleDateString([], { year: "2-digit", month: "short", day: "numeric" });
}


// ───────────────────────── editor ─────────────────────────

function ContactEditor({
  contact, onSaved, onCancel, onDeleted, onStatusChange,
}: {
  contact: Contact | null;             // null → create-new mode
  onSaved: (c: Contact) => void;
  onCancel: () => void;
  onDeleted: () => void;
  onStatusChange?: () => void;
}) {
  const isNew = !contact;

  const [displayName, setDisplayName] = useState(contact?.display_name || "");
  // Person identity (mig 045). For kind='person' we render First / Last
  // as the primary input pair; display_name becomes either auto-derived
  // ("First Last") or a user override ("Aunt Klara"). For kind='business'
  // these stay empty and the existing display_name = business name path
  // continues unchanged.
  const [firstName, setFirstName] = useState(contact?.first_name || "");
  const [lastName,  setLastName]  = useState(contact?.last_name  || "");
  // When display_name was never customised away from the auto-derived
  // "First Last", keep auto-syncing it as the user types. Once the user
  // edits display_name directly, this ref flips and we stop overwriting
  // their choice. The initial value detects "matches auto-derived" so
  // an existing Maria Schmidt contact whose display_name is literally
  // "Maria Schmidt" still gets auto-sync; a "Aunt Klara" override does
  // not.
  const displayNameAutoSyncRef = useRef<boolean>(
    !contact?.display_name
    || contact.display_name === [contact.first_name, contact.last_name].filter(Boolean).join(" ").trim()
  );

  // Drive display_name from first + last while the user hasn't taken
  // it over manually. Only fires for kind='person' — business names
  // stay user-controlled.
  function updateFirst(v: string) {
    setFirstName(v);
    if (kind === "person" && displayNameAutoSyncRef.current) {
      setDisplayName([v, lastName].filter(s => s.trim()).join(" ").trim());
    }
  }
  function updateLast(v: string) {
    setLastName(v);
    if (kind === "person" && displayNameAutoSyncRef.current) {
      setDisplayName([firstName, v].filter(s => s.trim()).join(" ").trim());
    }
  }
  function updateDisplayName(v: string) {
    setDisplayName(v);
    // The moment the user edits display_name directly, stop auto-syncing.
    displayNameAutoSyncRef.current = false;
  }
  const [kind, setKind] = useState<ContactKind>(contact?.kind || "person");
  const [aliasesStr, setAliasesStr] = useState((contact?.aliases || []).join(", "));
  const [relation, setRelation] = useState(contact?.relation || "");
  const [birthday, setBirthday] = useState(contact?.birthday || "");
  // Language default: the user's own language, so new contacts inherit
  // the household's lingua franca unless explicitly changed.
  const { user } = useAuth();
  const [languagePref, setLanguagePref] = useState(
    contact?.language_pref || user.language || "de"
  );
  // Salutation default: 'du' (DACH norm — the formal Sie is the
  // exception, not the rule, in family/household contexts).
  const [salutationPref, setSalutationPref] = useState(contact?.salutation_pref || "du");
  const [legalName, setLegalName] = useState(contact?.legal_name || "");
  const [taxId, setTaxId] = useState(contact?.tax_id || "");
  const [iban, setIban] = useState(contact?.iban || "");
  const [notes, setNotes] = useState(contact?.notes || "");
  const [saving, setSaving] = useState(false);
  const [busyAction, setBusyAction] = useState<"promote" | "spam" | "delete" | null>(null);
  const [enriching, setEnriching] = useState(false);
  // Inline feedback banner — shown DURING the enrich call (with the
  // "scanning…" message) AND for ~6s after with the result count.
  // The button alone was way too quiet for a 5-30s LLM call.
  const [enrichBanner, setEnrichBanner] = useState<
    | { kind: "running" }
    | { kind: "success"; written: number; scanned: Record<string, number> }
    | { kind: "empty"; scanned: Record<string, number> }
    | { kind: "stopped" }
    | { kind: "error"; message: string }
    | null
  >(null);
  // AbortController for the in-flight enrich call — lets the Stop
  // button cancel the client-side wait. The backend LLM call keeps
  // going (one shot, can't really interrupt mid-call), but the user
  // gets the UI back immediately. Proposals show up on next refresh
  // if the backend completed them.
  const enrichAbortRef = useRef<AbortController | null>(null);

  async function runEnrich() {
    if (!contact || enriching) return;  // hard guard against double-click
    setEnriching(true);
    setEnrichBanner({ kind: "running" });
    const ctrl = new AbortController();
    enrichAbortRef.current = ctrl;
    try {
      const res = await fetch(`/api/contacts/${contact.id}/enrich`, {
        method:      "POST",
        credentials: "include",
        headers:     { "content-type": "application/json" },
        body:        "{}",
        signal:      ctrl.signal,
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        const msg = (body && typeof body === "object" && "detail" in body)
          ? String((body as any).detail) : `HTTP ${res.status}`;
        setEnrichBanner({ kind: "error", message: msg });
        return;
      }
      const r = body as { proposals_written?: number;
                          sources_scanned?: Record<string, number>;
                          error?: string };
      if (r.error) {
        setEnrichBanner({ kind: "error", message: r.error });
      } else {
        await proposalsApi.refetch();
        autoFilledRef.current = false;  // re-allow auto-fill for fresh proposals
        const scanned = r.sources_scanned || {};
        if ((r.proposals_written || 0) > 0) {
          setEnrichBanner({ kind: "success", written: r.proposals_written!, scanned });
        } else {
          setEnrichBanner({ kind: "empty", scanned });
        }
      }
    } catch (e: any) {
      if (e?.name === "AbortError") {
        setEnrichBanner({ kind: "stopped" });
      } else {
        setEnrichBanner({ kind: "error", message: e?.message || String(e) });
      }
    } finally {
      setEnriching(false);
      enrichAbortRef.current = null;
    }
    setTimeout(() => {
      setEnrichBanner(b => (b && b.kind !== "error") ? null : b);
    }, 6000);
  }

  function stopEnrich() {
    enrichAbortRef.current?.abort();
  }

  // Channels and addresses edit through their own sub-rows; the rest of
  // the form snapshots them at mount and refetches via onSaved.
  const channels = contact?.channels || [];
  const addresses = contact?.addresses || [];

  // LLM enrichment proposals (contact_enrichment_proposals). Fetched
  // once per contact open; auto-fills any EMPTY field with the
  // highest-confidence proposal on first load, then the user can
  // edit freely or swap to a different candidate via the dropdown
  // chip below each input.
  const proposalsApi = useApi<{
    contact_id:        number;
    by_field:          Record<string, Proposal[]>;
    sources_available: { emails: number; whatsapp: number; documents: number;
                         calendar: number; seeds: string[] };
  }>(
    contact?.id ? `/api/contacts/${contact.id}/proposals` : null,
    [contact?.id],
  );
  const proposalsByField = proposalsApi.data?.by_field || {};
  const sourcesAvailable = proposalsApi.data?.sources_available;
  const autoFilledRef = useRef(false);
  useEffect(() => {
    if (autoFilledRef.current) return;
    if (proposalsApi.loading) return;
    if (!contact?.id) return;
    autoFilledRef.current = true;
    const pick = (field: string): string | undefined =>
      proposalsByField[field]?.[0]?.proposed_value;
    // Only fill if currently empty. Setters short-circuit on no-op anyway,
    // but the explicit check matches the spec ("empty fields auto-fill").
    if (!relation       && pick("relation"))       setRelation(pick("relation")!);
    if (!birthday       && pick("birthday"))       setBirthday(pick("birthday")!);
    if (!languagePref   && pick("language_pref"))  setLanguagePref(pick("language_pref")!);
    if (!salutationPref && pick("salutation_pref")) setSalutationPref(pick("salutation_pref")!);
    if (!legalName      && pick("legal_name"))     setLegalName(pick("legal_name")!);
    if (!taxId          && pick("tax_id"))         setTaxId(pick("tax_id")!);
    if (!iban           && pick("iban"))           setIban(pick("iban")!);
    if (!notes          && pick("notes"))          setNotes(pick("notes")!);
    const kindProposal = pick("kind");
    if (kindProposal === "person" || kindProposal === "business") {
      // Only auto-flip kind if the user hasn't already chosen business
      // (defaulting to "person" is a non-decision, so a strong proposal
      // can override it).
      if (kind === "person" && kindProposal === "business") setKind("business");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proposalsApi.loading, contact?.id]);

  async function handleSave() {
    // Person: require first name (last is optional). Business: require
    // display_name (= business name). The header inputs enforce the
    // right placeholder in each mode.
    if (kind === "person" && !firstName.trim()) return;
    if (kind === "business" && !displayName.trim()) return;

    // Person without a user-set display_name → auto-derive from
    // First Last. Business is unchanged.
    const effectiveDisplayName = kind === "person"
      ? (displayName.trim() || [firstName, lastName].filter(s => s.trim()).join(" ").trim())
      : displayName.trim();

    const body: Record<string, unknown> = {
      display_name: effectiveDisplayName,
      first_name: kind === "person" ? (firstName.trim() || null) : null,
      last_name:  kind === "person" ? (lastName.trim()  || null) : null,
      kind,
      aliases: parseList(aliasesStr),
      relation: relation || null,
      birthday: birthday || null,
      language_pref: languagePref || null,
      salutation_pref: salutationPref || null,
      legal_name: kind === "business" ? legalName || null : null,
      tax_id:     kind === "business" ? taxId || null : null,
      iban:       kind === "business" ? iban || null : null,
      notes: notes || null,
    };

    setSaving(true);
    try {
      const saved = isNew
        ? await api.post<Contact>("/api/contacts", body)
        : await api.patch<Contact>(`/api/contacts/${contact!.id}`, body);
      onSaved(saved);
    } catch (e: any) {
      alert(`Save failed: ${e?.message || e}`);
    } finally {
      setSaving(false);
    }
  }

  async function promote() {
    if (!contact) return;
    setBusyAction("promote");
    try {
      const c = await api.post<Contact>(`/api/contacts/${contact.id}/promote`);
      onSaved(c);
      onStatusChange?.();
    } catch (e: any) {
      alert(`Promote failed: ${e?.message || e}`);
    } finally { setBusyAction(null); }
  }

  async function markSpam() {
    if (!contact) return;
    if (!confirm(`Mark "${contact.display_name}" as spam? Future inbound from their channels will be silently dropped (channels stay indexed).`)) return;
    setBusyAction("spam");
    try {
      const c = await api.post<Contact>(`/api/contacts/${contact.id}/spam`);
      onSaved(c);
      onStatusChange?.();
    } catch (e: any) {
      alert(`Mark spam failed: ${e?.message || e}`);
    } finally { setBusyAction(null); }
  }

  async function handleDelete() {
    if (!contact) return;
    if (!confirm(`Delete "${contact.display_name}" permanently? This removes the contact and all their channels + addresses. (To merely silence them, use Mark spam.)`)) return;
    setBusyAction("delete");
    try {
      await api.delete(`/api/contacts/${contact.id}`);
      onDeleted();
    } catch (e: any) {
      alert(`Delete failed: ${e?.message || e}`);
    } finally { setBusyAction(null); }
  }

  return (
    <div className="space-y-5">
      {/* Header row — name + kind toggle + status pill.
          Person: First / Last pair, with display_name auto-derived
          and an "Show as" override available below. Business: single
          name input as before (a business is its name). */}
      <div className="flex flex-wrap items-start gap-3">
        {kind === "person" ? (
          <div className="flex-1 min-w-[200px] flex flex-wrap gap-2">
            <input
              value={firstName}
              onChange={e => updateFirst(e.target.value)}
              placeholder="First name (required)"
              className="flex-1 min-w-[120px] h-11 md:h-9 px-2 bg-transparent text-lg font-semibold border-b border-border focus:outline-none focus:border-primary"
            />
            <input
              value={lastName}
              onChange={e => updateLast(e.target.value)}
              placeholder="Last name (optional)"
              className="flex-1 min-w-[120px] h-11 md:h-9 px-2 bg-transparent text-lg font-semibold border-b border-border focus:outline-none focus:border-primary"
            />
          </div>
        ) : (
          <div className="flex-1 min-w-[200px]">
            <input
              value={displayName}
              onChange={e => updateDisplayName(e.target.value)}
              placeholder="Business name (required)"
              className="w-full h-11 md:h-9 px-2 bg-transparent text-lg font-semibold border-b border-border focus:outline-none focus:border-primary"
            />
          </div>
        )}
        <div className="flex gap-1 rounded-md bg-muted p-0.5">
          <KindToggle current={kind} value="person"   label="Person"   icon={<UserIcon className="w-3 h-3" />}   onSelect={setKind} />
          <KindToggle current={kind} value="business" label="Business" icon={<Briefcase className="w-3 h-3" />} onSelect={setKind} />
        </div>
        {contact?.status === "pending" && (
          <span className="text-[10px] uppercase tracking-wider px-2 py-1 rounded-full bg-amber-500/15 text-amber-500 font-medium">pending</span>
        )}
        {contact?.status === "spam" && (
          <span className="text-[10px] uppercase tracking-wider px-2 py-1 rounded-full bg-red-500/15 text-red-500 font-medium flex items-center gap-1">
            <ShieldAlert className="w-3 h-3" />spam
          </span>
        )}
      </div>

      {/* Pending → quick promote/spam actions stay near the top so the
          common case (triage a pending row) is one click. */}
      {contact?.status === "pending" && (
        <div className="flex gap-2 text-xs">
          <button
            onClick={promote}
            disabled={!!busyAction}
            className="px-3 py-1.5 rounded-md bg-emerald-500 text-white hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5"
          >
            {busyAction === "promote" ? <Loader2 className="w-3 h-3 animate-spin" /> : <Star className="w-3 h-3" />}
            Confirm contact
          </button>
          <button
            onClick={markSpam}
            disabled={!!busyAction}
            className="px-3 py-1.5 rounded-md border border-border bg-card hover:bg-muted disabled:opacity-50 flex items-center gap-1.5"
          >
            <ShieldAlert className="w-3 h-3" /> Mark spam
          </button>
        </div>
      )}

      {/* Basics grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <LabelledInput label="Aliases (comma-separated)"
          value={aliasesStr} onChange={setAliasesStr} placeholder="Oma, Grossmutter" />
        <LabelledInput label="Relation"
          value={relation} onChange={setRelation} placeholder="grandmother, plumber, vendor"
          proposals={proposalsByField.relation} />
        <BirthdayField
          value={birthday}
          onChange={setBirthday}
          contactId={contact?.id}
        />
        <ChipField
          label="Language"
          value={languagePref}
          onChange={setLanguagePref}
          options={[
            { value: "de", label: "Deutsch" },
            { value: "en", label: "English" },
            { value: "fr", label: "Français" },
            { value: "it", label: "Italiano" },
            { value: "es", label: "Español" },
          ]}
          allowFreeText
        />
        <ChipField
          label="Salutation"
          value={salutationPref}
          onChange={setSalutationPref}
          options={[
            { value: "du", label: "du" },
            { value: "Sie", label: "Sie" },
            { value: "first-name", label: "First name" },
            { value: "formal", label: "Formal" },
          ]}
        />
      </div>

      {/* Business-only fields */}
      {kind === "business" && (
        <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
          <LabelledInput label="Legal name" value={legalName} onChange={setLegalName}
            placeholder="Acme Real Estate LLC"
            proposals={proposalsByField.legal_name} />
          <LabelledInput label="Tax ID / VAT / EIN" value={taxId} onChange={setTaxId}
            proposals={proposalsByField.tax_id} />
          <LabelledInput label="IBAN" value={iban} onChange={setIban}
            proposals={proposalsByField.iban} />
        </div>
      )}

      {/* Notes */}
      <div>
        <Label>Notes</Label>
        <textarea
          value={notes}
          onChange={e => setNotes(e.target.value)}
          rows={3}
          placeholder="Anything Yorik should remember — preferred greeting, things to ask, recent context."
          className="w-full px-2 py-1.5 bg-background border border-border rounded-md text-sm focus:outline-none focus:ring-1 focus:ring-ring/40"
        />
        <ProposalDropdown proposals={proposalsByField.notes} currentValue={notes} onPick={setNotes} />
      </div>

      {/* Channels & addresses — only meaningful for existing contacts. */}
      {!isNew && contact && (
        <>
          <ChannelsPanel contact={contact} onChanged={() => onStatusChange?.()} onContactReload={(c) => onSaved(c)} />
          <AddressesPanel contact={contact} onChanged={() => onStatusChange?.()} onContactReload={(c) => onSaved(c)} proposals={proposalsByField.address} />
        </>
      )}

      {isNew && (
        <p className="text-xs text-muted-foreground italic">
          Save first — then you can add channels (email, phone, WhatsApp) and addresses.
        </p>
      )}

      {/* Action bar */}
      <div className="flex flex-wrap gap-2 pt-2 border-t border-border">
        <button
          onClick={handleSave}
          disabled={saving
            || (kind === "person"   && !firstName.trim())
            || (kind === "business" && !displayName.trim())}
          className="text-xs px-4 py-1.5 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5"
        >
          {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
          {isNew ? "Create contact" : "Save changes"}
        </button>
        <button
          onClick={onCancel}
          className="text-xs px-3 py-1.5 rounded-md border border-border bg-card hover:bg-muted"
        >
          <X className="w-3 h-3 inline-block mr-1" /> Close
        </button>
        {contact && (
          enriching ? (
            <button
              onClick={stopEnrich}
              className="text-xs px-3 py-1.5 rounded-md border border-red-500/40 bg-red-500/10 hover:bg-red-500/15 text-red-700 dark:text-red-300 flex items-center gap-1.5"
              title="Stop waiting for the LLM (the call may still finish in the background)"
            >
              <StopCircle className="w-3 h-3" /> Stop enrichment
            </button>
          ) : (
            <button
              onClick={runEnrich}
              className="text-xs px-3 py-1.5 rounded-md border border-amber-500/30 bg-amber-500/10 hover:bg-amber-500/15 text-amber-700 dark:text-amber-300 flex items-center gap-1.5"
              title={
                sourcesAvailable
                  ? `${sourcesAvailable.emails} email(s), ${sourcesAvailable.whatsapp} WA msg(s), ${sourcesAvailable.documents} doc(s), ${sourcesAvailable.calendar} event(s) on file. ~5–30s LLM call.`
                  : "Re-scan emails, WhatsApp, docs, and calendar for fresh suggestions for THIS contact"
              }
            >
              <Wand2 className="w-3 h-3" /> Enrich this contact
              {sourcesAvailable && (
                <span className="ml-1 opacity-70 text-[10px] tabular-nums">
                  ({sourcesAvailable.emails}e · {sourcesAvailable.whatsapp}wa · {sourcesAvailable.documents}d · {sourcesAvailable.calendar}cal)
                </span>
              )}
            </button>
          )
        )}
        {contact && contact.status !== "spam" && (
          <button
            onClick={markSpam}
            disabled={!!busyAction}
            className="text-xs px-3 py-1.5 ml-auto rounded-md border border-border bg-card hover:bg-red-500/10 hover:border-red-500/30 hover:text-red-500 disabled:opacity-50 flex items-center gap-1.5"
          >
            {busyAction === "spam" ? <Loader2 className="w-3 h-3 animate-spin" /> : <ShieldAlert className="w-3 h-3" />}
            Mark spam
          </button>
        )}
        {contact && (
          <button
            onClick={handleDelete}
            disabled={!!busyAction}
            className={cn(
              "text-xs px-3 py-1.5 rounded-md border border-border bg-card hover:bg-red-500/10 hover:border-red-500/30 hover:text-red-500 disabled:opacity-50 flex items-center gap-1.5",
              contact.status === "spam" && "ml-auto",
            )}
          >
            {busyAction === "delete" ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3 h-3" />}
            Delete
          </button>
        )}
      </div>

      {/* Always-on hint when Yorik has no data on this contact — the
          enrich button would otherwise return 0 proposals with no
          obvious reason ("did it work?"). Only shown when proposals
          haven't been generated yet AND no source has data. */}
      {contact && sourcesAvailable && !enrichBanner &&
       Object.keys(proposalsByField).length === 0 &&
       (sourcesAvailable.emails + sourcesAvailable.whatsapp +
        sourcesAvailable.documents + sourcesAvailable.calendar) === 0 && (
        <div className="rounded-md border border-border bg-muted/40 p-3 text-xs text-muted-foreground flex items-start gap-2">
          <UserIcon className="w-4 h-4 shrink-0 mt-0.5 opacity-60" />
          <div className="flex-1 leading-relaxed">
            <strong className="text-foreground/80">Nothing on file yet</strong> — Yorik hasn't
            seen this contact in any emails, WhatsApp chats, documents, or calendar events,
            so running Enrich won't find anything to suggest. Add an email or WhatsApp
            channel below first, OR wait until you've exchanged a few messages with them.
            {sourcesAvailable.seeds.length > 0 && (
              <div className="mt-1 text-[10px] opacity-70 font-mono">
                Tried search seeds: {sourcesAvailable.seeds.join(" · ")}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Enrich feedback banner — visible during AND after the LLM call
          since the button spinner alone is way too quiet for a 5-30s op. */}
      {enrichBanner && (
        <div className={cn(
          "rounded-md border p-3 text-xs flex items-start gap-2",
          enrichBanner.kind === "running" && "bg-amber-500/10 border-amber-500/30 text-amber-700 dark:text-amber-300",
          enrichBanner.kind === "success" && "bg-emerald-500/10 border-emerald-500/30 text-emerald-700 dark:text-emerald-300",
          enrichBanner.kind === "empty"   && "bg-muted border-border text-muted-foreground",
          enrichBanner.kind === "stopped" && "bg-muted border-border text-muted-foreground",
          enrichBanner.kind === "error"   && "bg-red-500/10 border-red-500/30 text-red-700 dark:text-red-300",
        )}>
          {enrichBanner.kind === "running" && (
            <>
              <Loader2 className="w-4 h-4 animate-spin shrink-0 mt-0.5" />
              <div className="flex-1">
                <div className="font-medium">Scanning data for {displayName || "this contact"}…</div>
                <div className="text-[11px] mt-1 opacity-80 leading-relaxed">
                  Reading recent emails, WhatsApp messages, Paperless documents, and calendar events.
                  Then asking the LLM to extract address, birthday, relation, and other field
                  suggestions with source citations. Usually 5–30 seconds.
                </div>
              </div>
            </>
          )}
          {enrichBanner.kind === "success" && (
            <>
              <Check className="w-4 h-4 shrink-0 mt-0.5" />
              <div className="flex-1">
                <div className="font-medium">
                  {enrichBanner.written} new suggestion{enrichBanner.written === 1 ? "" : "s"} —
                  fields above are highlighted with a ⌄ chip.
                </div>
                <div className="text-[11px] mt-1 opacity-80">
                  Scanned {enrichBanner.scanned.emails || 0} email{enrichBanner.scanned.emails === 1 ? "" : "s"},
                  {" "}{enrichBanner.scanned.whatsapp || 0} WhatsApp message{enrichBanner.scanned.whatsapp === 1 ? "" : "s"},
                  {" "}{enrichBanner.scanned.documents || 0} document{enrichBanner.scanned.documents === 1 ? "" : "s"},
                  {" "}{enrichBanner.scanned.calendar || 0} calendar event{enrichBanner.scanned.calendar === 1 ? "" : "s"}.
                </div>
              </div>
              <button onClick={() => setEnrichBanner(null)} className="p-0.5 hover:opacity-70" aria-label="Dismiss">
                <X className="w-3 h-3" />
              </button>
            </>
          )}
          {enrichBanner.kind === "empty" && (
            <>
              <UserIcon className="w-4 h-4 shrink-0 mt-0.5 opacity-60" />
              <div className="flex-1">
                <div className="font-medium">No new information found.</div>
                <div className="text-[11px] mt-1 opacity-80">
                  Scanned {enrichBanner.scanned.emails || 0} email{enrichBanner.scanned.emails === 1 ? "" : "s"},
                  {" "}{enrichBanner.scanned.whatsapp || 0} WhatsApp message{enrichBanner.scanned.whatsapp === 1 ? "" : "s"},
                  {" "}{enrichBanner.scanned.documents || 0} document{enrichBanner.scanned.documents === 1 ? "" : "s"},
                  {" "}{enrichBanner.scanned.calendar || 0} calendar event{enrichBanner.scanned.calendar === 1 ? "" : "s"}.
                  Either nothing new since the last run, or the LLM couldn't confidently extract anything.
                </div>
              </div>
              <button onClick={() => setEnrichBanner(null)} className="p-0.5 hover:opacity-70" aria-label="Dismiss">
                <X className="w-3 h-3" />
              </button>
            </>
          )}
          {enrichBanner.kind === "stopped" && (
            <>
              <StopCircle className="w-4 h-4 shrink-0 mt-0.5 opacity-60" />
              <div className="flex-1">
                <div className="font-medium">Stopped.</div>
                <div className="text-[11px] mt-1 opacity-80">
                  The LLM may still finish in the background — if proposals appear, refresh this contact to see them.
                </div>
              </div>
              <button onClick={() => setEnrichBanner(null)} className="p-0.5 hover:opacity-70" aria-label="Dismiss">
                <X className="w-3 h-3" />
              </button>
            </>
          )}
          {enrichBanner.kind === "error" && (
            <>
              <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5" />
              <div className="flex-1">
                <div className="font-medium">Enrich failed</div>
                <div className="text-[11px] mt-1 opacity-90 font-mono break-all">{enrichBanner.message}</div>
              </div>
              <button onClick={() => setEnrichBanner(null)} className="p-0.5 hover:opacity-70" aria-label="Dismiss">
                <X className="w-3 h-3" />
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function parseList(s: string): string[] {
  return s.split(",").map(x => x.trim()).filter(Boolean);
}

function KindToggle({
  current, value, label, icon, onSelect,
}: {
  current: ContactKind;
  value: ContactKind;
  label: string;
  icon: ReactElement;
  onSelect: (v: ContactKind) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(value)}
      className={cn(
        "text-[11px] px-2.5 py-1 rounded flex items-center gap-1 transition",
        current === value ? "bg-background shadow-sm text-foreground" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {icon}{label}
    </button>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <label className="block text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">{children}</label>;
}

function LabelledInput({
  label, value, onChange, type = "text", placeholder, maxLength, proposals,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
  maxLength?: number;
  /** Optional LLM-enricher proposals for this field — renders a small
   *  dropdown chip below the input when at least one is present. */
  proposals?: Proposal[];
}) {
  return (
    <div>
      <Label>{label}</Label>
      <input
        type={type}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        maxLength={maxLength}
        // h-11 on mobile clears Apple HIG's 44pt minimum; desktop
        // keeps the compact h-8 for the dense editor layout.
        className="w-full h-11 md:h-8 px-2 bg-background border border-border rounded-md text-sm focus:outline-none focus:ring-1 focus:ring-ring/40"
      />
      <ProposalDropdown proposals={proposals} currentValue={value} onPick={onChange} />
    </div>
  );
}


// ─── LLM proposals (contact_enrichment_proposals) ─────────────────────

interface Proposal {
  id:             number;
  field_name:     string;
  proposed_value: string;
  confidence:     number;
  source_kind:    string;       // email_signature | email_body | whatsapp | paperless_doc | manual
  source_ref:     string | null;
  source_snippet: string | null;
  created_at:     string;
}

/**
 * Compact dropdown chip rendered below a field input. Shows
 * "N suggestion(s)" + chevron; opens a popover listing each
 * candidate with its confidence %, source snippet, and source kind.
 * Picking a candidate fills the field and closes the popover.
 */
function ProposalDropdown({
  proposals, currentValue, onPick,
}: {
  proposals?: Proposal[];
  currentValue: string;
  onPick: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!proposals || proposals.length === 0) return null;
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="mt-1 inline-flex items-center gap-1 text-[10px] text-amber-700 dark:text-amber-300 hover:bg-amber-500/10 px-1.5 py-0.5 rounded transition"
        title="Suggestions from the LLM contact enricher"
      >
        <Wand2 className="w-2.5 h-2.5" />
        {proposals.length} suggestion{proposals.length === 1 ? "" : "s"}
        <ChevronDown className={cn("w-3 h-3 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute z-50 left-0 top-full mt-1 w-[320px] max-w-[90vw] bg-card border border-border rounded-lg shadow-xl overflow-hidden">
            {proposals.map(p => {
              const isCurrent = p.proposed_value === currentValue;
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => { onPick(p.proposed_value); setOpen(false); }}
                  className={cn(
                    "w-full text-left px-3 py-2 hover:bg-muted/60 border-b border-border/60 last:border-b-0 transition",
                    isCurrent && "bg-primary/10",
                  )}
                >
                  <div className="flex items-center justify-between gap-2 text-xs">
                    <span className="font-medium truncate flex-1">{p.proposed_value}</span>
                    <span className="text-[10px] text-muted-foreground tabular-nums shrink-0">
                      {Math.round(p.confidence * 100)}%
                    </span>
                  </div>
                  {p.source_snippet && (
                    <div className="text-[10px] text-muted-foreground mt-1 line-clamp-2 italic">
                      "{p.source_snippet}"
                    </div>
                  )}
                  <div className="text-[9px] text-muted-foreground/70 mt-0.5 uppercase tracking-wider">
                    {p.source_kind.replace(/_/g, " ")}
                  </div>
                </button>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

function BirthdayField({
  value, onChange, contactId,
}: {
  value: string;
  onChange: (v: string) => void;
  contactId?: number;
}) {
  // Locale-formatted preview underneath the picker — the native
  // <input type=date> already gives a real calendar, but the format
  // shown in the input varies wildly across browsers/locales; the
  // preview row eliminates the "wait, is that month-day or day-month?"
  // moment users have on first encounter.
  const preview = value
    ? new Date(value + "T00:00:00").toLocaleDateString(undefined, {
        day: "numeric", month: "long", year: "numeric",
      })
    : "";

  // Auto-detect from WhatsApp history (most-common birthday-greeting
  // date). Only shown when (a) we know the contact id, (b) field is
  // currently empty, (c) the backend actually found something. Cheap
  // SQL query — runs once per contact view.
  const [suggestion, setSuggestion] = useState<{
    month_day: string; evidence_count: number; years_seen: number[];
  } | null>(null);
  useEffect(() => {
    if (!contactId || value) { setSuggestion(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const r = await api.get<{
          detected: { month: number; day: number; month_day: string } | null;
          evidence_count?: number;
          years_seen?: number[];
        }>(`/api/contacts/${contactId}/birthday-suggestion`);
        if (cancelled || !r.detected) return;
        setSuggestion({
          month_day:      r.detected.month_day,
          evidence_count: r.evidence_count || 0,
          years_seen:     r.years_seen || [],
        });
      } catch { /* silent — empty pill = no suggestion */ }
    })();
    return () => { cancelled = true; };
  }, [contactId, value]);

  function applySuggestion() {
    if (!suggestion) return;
    // We don't know the year — pick the median of years_seen, falling
    // back to a placeholder year so the date input is valid. The user
    // can change the year if they care; the year field is the least
    // important part for everyday reminders.
    const years = suggestion.years_seen;
    const guessYear = years.length > 0
      ? years[Math.floor(years.length / 2)]
      : 1990;
    onChange(`${guessYear}-${suggestion.month_day}`);
    setSuggestion(null);
  }

  return (
    <div>
      <Label>Birthday</Label>
      <input
        type="date"
        value={value}
        onChange={e => onChange(e.target.value)}
        className="w-full h-11 md:h-8 px-2 bg-background border border-border rounded-md text-sm focus:outline-none focus:ring-1 focus:ring-ring/40"
      />
      {preview && (
        <div className="text-[10px] text-muted-foreground mt-0.5 pl-1">{preview}</div>
      )}
      {suggestion && !value && (
        <button
          type="button"
          onClick={applySuggestion}
          className="mt-1 inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] bg-violet-500/10 border border-violet-500/30 text-violet-600 hover:bg-violet-500/15 transition"
          title={`Detected from ${suggestion.evidence_count} birthday message${suggestion.evidence_count === 1 ? "" : "s"} you sent`}
        >
          <Star className="w-2.5 h-2.5" />
          Likely {new Date("2000-" + suggestion.month_day + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "long" })}
          {suggestion.evidence_count > 1 && (
            <span className="opacity-60">· {suggestion.evidence_count} msgs</span>
          )}
        </button>
      )}
    </div>
  );
}

interface ChipOption { value: string; label: string }

function ChipField({
  label, value, onChange, options, allowFreeText = false,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: ChipOption[];
  /** If true, render a tiny inline input for "other" values so the user
   * can still record e.g. a niche language code that isn't in the
   * preset list. */
  allowFreeText?: boolean;
}) {
  const isPreset = options.some(o => o.value === value);
  const showFreeText = allowFreeText && !!value && !isPreset;
  return (
    <div>
      <Label>{label}</Label>
      <div className="flex flex-wrap gap-1.5 items-center">
        {options.map(opt => (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className={cn(
              "text-[11px] px-2.5 py-1 rounded-full border transition",
              value === opt.value
                ? "bg-primary text-primary-foreground border-primary"
                : "bg-card border-border text-muted-foreground hover:text-foreground",
            )}
          >
            {opt.label}
          </button>
        ))}
        {allowFreeText && (
          <input
            value={showFreeText ? value : ""}
            onChange={e => onChange(e.target.value)}
            placeholder="other…"
            maxLength={12}
            className={cn(
              "h-7 w-24 px-2 rounded-full text-[11px] focus:outline-none transition",
              showFreeText
                ? "bg-primary/15 text-primary border border-primary/40"
                : "bg-background border border-border text-muted-foreground placeholder:text-muted-foreground/60",
            )}
          />
        )}
      </div>
    </div>
  );
}

// ───────────────────────── channels ─────────────────────────

function ChannelsPanel({
  contact, onChanged, onContactReload,
}: {
  contact: Contact;
  onChanged: () => void;
  onContactReload: (c: Contact) => void;
}) {
  const [kind, setKind] = useState<ChannelKind>("email");
  const [value, setValue] = useState("");
  const [label, setLabel] = useState("");
  const [adding, setAdding] = useState(false);

  async function add() {
    if (!value.trim()) return;
    setAdding(true);
    try {
      const c = await api.post<Contact>(`/api/contacts/${contact.id}/channels`, {
        kind, value: value.trim(), label: label.trim() || null,
      });
      setValue(""); setLabel("");
      onContactReload(c);
      onChanged();
    } catch (e: any) {
      const msg = e instanceof ApiError && e.status === 409
        ? `That ${kind} is already on another contact: ${e.message}`
        : `Add channel failed: ${e?.message || e}`;
      alert(msg);
    } finally { setAdding(false); }
  }

  async function remove(ch: ContactChannel) {
    if (!confirm(`Remove ${ch.kind} ${ch.value}?`)) return;
    try {
      await api.delete(`/api/contacts/channels/${ch.id}`);
      // Refetch the contact to get the new channels list.
      const c = await api.get<Contact>(`/api/contacts/${contact.id}`);
      onContactReload(c);
      onChanged();
    } catch (e: any) {
      alert(`Remove failed: ${e?.message || e}`);
    }
  }

  return (
    <div>
      <Label>Channels — how to reach them</Label>
      <div className="space-y-1">
        {contact.channels.length === 0 && (
          <div className="text-xs text-muted-foreground italic px-2 py-1.5">No channels yet.</div>
        )}
        {contact.channels.map(ch => (
          <div key={ch.id} className="flex items-center gap-2 px-2 py-1.5 rounded-md border border-border bg-background text-sm">
            <span className="text-muted-foreground">{CHANNEL_ICONS[ch.kind] || <Globe className="w-3.5 h-3.5" />}</span>
            <span className="font-medium">{ch.value}</span>
            {ch.label && <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{ch.label}</span>}
            <span className="text-[10px] text-muted-foreground opacity-60 ml-auto">{ch.source}</span>
            <button
              onClick={() => remove(ch)}
              className="p-1 rounded text-muted-foreground hover:text-red-500 hover:bg-red-500/10"
              title="Remove channel"
            >
              <Trash2 className="w-3 h-3" />
            </button>
          </div>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        <select
          value={kind}
          onChange={e => setKind(e.target.value as ChannelKind)}
          className="h-11 md:h-8 px-2 bg-background border border-border rounded-md text-xs focus:outline-none"
        >
          <option value="email">Email</option>
          <option value="phone">Phone</option>
          <option value="whatsapp">WhatsApp</option>
          <option value="signal">Signal</option>
          <option value="telegram">Telegram</option>
          <option value="sms">SMS</option>
          <option value="website">Website</option>
          <option value="social">Social</option>
        </select>
        <input
          value={value}
          onChange={e => setValue(e.target.value)}
          placeholder={
            kind === "email"   ? "oma@example.com" :
            kind === "phone"   ? "+49 30 1234 5678" :
            kind === "website" ? "https://…" :
                                 "value"
          }
          className="flex-1 min-w-[180px] h-11 md:h-8 px-2 bg-background border border-border rounded-md text-sm focus:outline-none focus:ring-1 focus:ring-ring/40"
        />
        <input
          value={label}
          onChange={e => setLabel(e.target.value)}
          placeholder="label (mobile, work…)"
          className="w-32 h-11 md:h-8 px-2 bg-background border border-border rounded-md text-xs focus:outline-none"
        />
        <button
          onClick={add}
          disabled={adding || !value.trim()}
          className="text-xs h-8 px-3 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
        >
          {adding ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />} Add
        </button>
      </div>

      {/* When adding an email, scan the user's inbox for messages that
          might belong to this contact (matches name+aliases, plus a
          free-text fallback). Magic moment: "I already have an email
          from Anna — here it is." Click → fills the value field, user
          confirms with Add. */}
      {kind === "email" && (
        <InboxSuggestions
          contactId={contact.id}
          onPick={(addr) => setValue(addr)}
        />
      )}
    </div>
  );
}

function InboxSuggestions({
  contactId, onPick,
}: {
  contactId: number;
  onPick: (addr: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Array<{
    from_email: string; from_name: string;
    msg_count: number; last_seen: string | null; last_subject: string;
  }>>([]);
  const [loading, setLoading] = useState(true);
  const [hasRun, setHasRun] = useState(false);

  // Auto-run on mount with no query — pulls suggestions matched by name.
  // Re-runs whenever the (debounced) query changes.
  useEffect(() => {
    const handle = window.setTimeout(async () => {
      setLoading(true);
      try {
        const params = new URLSearchParams({ limit: "6" });
        if (query.trim()) params.set("q", query.trim());
        const r = await api.get<typeof results>(
          `/api/contacts/${contactId}/email-suggestions?${params}`,
        );
        setResults(r);
      } catch {
        setResults([]);
      } finally {
        setLoading(false);
        setHasRun(true);
      }
    }, query ? 250 : 0);
    return () => window.clearTimeout(handle);
  }, [contactId, query]);

  return (
    <div className="mt-2 rounded-md border border-dashed border-border bg-muted/20 p-2">
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold flex items-center gap-1">
          <Mail className="w-3 h-3" /> From your inbox
        </div>
        <div className="relative">
          <Search className="w-3 h-3 absolute left-1.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="search emails…"
            className="h-6 pl-5 pr-2 w-44 bg-background border border-border rounded text-[11px] focus:outline-none focus:ring-1 focus:ring-ring/30"
          />
        </div>
      </div>
      {loading && (
        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground py-1">
          <Loader2 className="w-3 h-3 animate-spin" /> Searching…
        </div>
      )}
      {!loading && hasRun && results.length === 0 && (
        <div className="text-[11px] text-muted-foreground italic py-1">
          {query ? "No matches in inbox." : "Nothing in your inbox matched this name — try searching above."}
        </div>
      )}
      {!loading && results.map(r => (
        <button
          key={r.from_email}
          type="button"
          onClick={() => onPick(r.from_email)}
          className="w-full text-left px-2 py-1.5 rounded hover:bg-muted/60 transition flex items-start gap-2"
          title="Click to fill the email field above"
        >
          <Mail className="w-3 h-3 mt-0.5 text-muted-foreground shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="text-xs flex items-center gap-1.5 truncate">
              <span className="font-medium">{r.from_email}</span>
              {r.from_name && <span className="text-muted-foreground truncate">· {r.from_name}</span>}
            </div>
            <div className="text-[10px] text-muted-foreground truncate flex items-center gap-1.5">
              <span>{r.msg_count} message{r.msg_count === 1 ? "" : "s"}</span>
              {r.last_seen && (
                <>
                  <span className="opacity-40">·</span>
                  <span>last on {new Date(r.last_seen).toLocaleDateString()}</span>
                </>
              )}
              {r.last_subject && (
                <>
                  <span className="opacity-40">·</span>
                  <span className="truncate">"{r.last_subject}"</span>
                </>
              )}
            </div>
          </div>
        </button>
      ))}
    </div>
  );
}

// ───────────────────────── addresses ─────────────────────────

function AddressesPanel({
  contact, onChanged, onContactReload, proposals,
}: {
  contact: Contact;
  onChanged: () => void;
  onContactReload: (c: Contact) => void;
  /** LLM proposals for the 'address' field — each value is a JSON
   *  blob with whatever subset of {line1,line2,postcode,city,country}
   *  the LLM could extract. Partial proposals are common (calendar
   *  events often only carry a city). */
  proposals?: Proposal[];
}) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<AddressKind>("home");
  const [line1, setLine1] = useState("");
  const [line2, setLine2] = useState("");
  const [postcode, setPostcode] = useState("");
  const [city, setCity] = useState("");
  const [country, setCountry] = useState("");
  const [adding, setAdding] = useState(false);

  // Pre-parse each address proposal once. Tolerates non-JSON values
  // (defensive: an older proposal row might have plain text).
  const parsedProposals = useMemo(() => {
    return (proposals || []).map(p => {
      let parts: { line1?: string; line2?: string; postcode?: string;
                   city?: string; country?: string } = {};
      try {
        const obj = JSON.parse(p.proposed_value);
        if (obj && typeof obj === "object") parts = obj;
      } catch {
        // Fall back: stick the whole string in line1
        parts = { line1: p.proposed_value };
      }
      const summary = [parts.line1, parts.postcode, parts.city, parts.country]
        .filter(Boolean).join(", ") || "(incomplete)";
      return { proposal: p, parts, summary };
    });
  }, [proposals]);

  function applyProposal(parts: { line1?: string; line2?: string;
                                  postcode?: string; city?: string;
                                  country?: string }) {
    // Open the form if it isn't already; fill ONLY the fields the
    // proposal actually carries (partial proposals leave the rest
    // empty for the user to type in — that's the "I only know the
    // city" case).
    setOpen(true);
    if (parts.line1)    setLine1(parts.line1);
    if (parts.line2)    setLine2(parts.line2);
    if (parts.postcode) setPostcode(parts.postcode);
    if (parts.city)     setCity(parts.city);
    if (parts.country)  setCountry(parts.country);
  }

  async function add() {
    if (!(line1 || postcode || city)) return;
    setAdding(true);
    try {
      const c = await api.post<Contact>(`/api/contacts/${contact.id}/addresses`, {
        kind, line1: line1 || null, line2: line2 || null,
        postcode: postcode || null, city: city || null,
        country: country || null,
      });
      setLine1(""); setLine2(""); setPostcode(""); setCity(""); setCountry("");
      setOpen(false);
      onContactReload(c);
      onChanged();
    } catch (e: any) {
      alert(`Add address failed: ${e?.message || e}`);
    } finally { setAdding(false); }
  }

  async function remove(a: ContactAddress) {
    if (!confirm(`Remove ${a.kind} address?`)) return;
    try {
      await api.delete(`/api/contacts/addresses/${a.id}`);
      const c = await api.get<Contact>(`/api/contacts/${contact.id}`);
      onContactReload(c);
      onChanged();
    } catch (e: any) {
      alert(`Remove failed: ${e?.message || e}`);
    }
  }

  return (
    <div>
      <Label>Addresses</Label>
      {parsedProposals.length > 0 && (
        <div className="mb-2 p-2 rounded-md bg-amber-500/5 border border-amber-500/20">
          <div className="text-[10px] text-amber-700 dark:text-amber-300 uppercase tracking-wider mb-1.5 flex items-center gap-1">
            <Wand2 className="w-2.5 h-2.5" /> Suggested addresses · click to pre-fill
          </div>
          <div className="flex flex-wrap gap-1.5">
            {parsedProposals.map(({ proposal, parts, summary }) => (
              <button
                key={proposal.id}
                type="button"
                onClick={() => applyProposal(parts)}
                title={proposal.source_snippet
                  ? `"${proposal.source_snippet}" — from ${proposal.source_kind.replace(/_/g, " ")}`
                  : `From ${proposal.source_kind.replace(/_/g, " ")}`}
                className="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded-md bg-card border border-amber-500/30 hover:bg-amber-500/10 hover:border-amber-500/50 transition text-left"
              >
                <MapPin className="w-2.5 h-2.5 text-amber-600 shrink-0" />
                <span className="max-w-[200px] truncate">{summary}</span>
                <span className="text-[9px] text-muted-foreground tabular-nums">
                  {Math.round(proposal.confidence * 100)}%
                </span>
              </button>
            ))}
          </div>
          <div className="text-[10px] text-muted-foreground mt-1.5 italic">
            Picking fills only what was found — type the rest yourself (e.g. street number for a city-only suggestion). You can always paste in a fresh address you got via phone.
          </div>
        </div>
      )}
      <div className="space-y-1">
        {contact.addresses.length === 0 && (
          <div className="text-xs text-muted-foreground italic px-2 py-1.5">No addresses yet.</div>
        )}
        {contact.addresses.map(a => (
          <div key={a.id} className="flex items-start gap-2 px-2 py-1.5 rounded-md border border-border bg-background text-sm">
            <MapPin className="w-3.5 h-3.5 text-muted-foreground mt-0.5" />
            <div className="flex-1 leading-tight">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{a.kind}{a.label ? ` · ${a.label}` : ""}</div>
              <div>{[a.line1, a.line2].filter(Boolean).join(", ")}</div>
              <div className="text-xs text-muted-foreground">{[a.postcode, a.city, a.region, a.country].filter(Boolean).join(" · ")}</div>
            </div>
            <button
              onClick={() => remove(a)}
              className="p-1 rounded text-muted-foreground hover:text-red-500 hover:bg-red-500/10"
              title="Remove address"
            >
              <Trash2 className="w-3 h-3" />
            </button>
          </div>
        ))}
      </div>
      {!open && (
        <button
          onClick={() => setOpen(true)}
          className="mt-2 text-xs h-8 px-3 rounded-md border border-border bg-card hover:bg-muted flex items-center gap-1.5"
        >
          <Plus className="w-3 h-3" /> Add address
        </button>
      )}
      {open && (
        // Stacks 1 column on mobile (each field gets its own row, no
        // cramped 2-up where city + postcode + country fought for
        // ~166px each at 375px wide). Desktop stays at 4 columns.
        <div className="mt-2 grid grid-cols-1 sm:grid-cols-4 gap-2 p-3 rounded-md border border-border bg-background/50">
          <select
            value={kind}
            onChange={e => setKind(e.target.value as AddressKind)}
            className="col-span-2 sm:col-span-1 h-11 md:h-8 px-2 bg-background border border-border rounded-md text-xs focus:outline-none"
          >
            <option value="home">Home</option>
            <option value="work">Work</option>
            <option value="billing">Billing</option>
            <option value="shipping">Shipping</option>
            <option value="other">Other</option>
          </select>
          <input value={line1} onChange={e => setLine1(e.target.value)} placeholder="Street + number"
            className="col-span-2 sm:col-span-3 h-11 md:h-8 px-2 bg-background border border-border rounded-md text-sm focus:outline-none" />
          <input value={line2} onChange={e => setLine2(e.target.value)} placeholder="Line 2 (optional)"
            className="col-span-2 sm:col-span-4 h-11 md:h-8 px-2 bg-background border border-border rounded-md text-sm focus:outline-none" />
          <input value={postcode} onChange={e => setPostcode(e.target.value)} placeholder="Postcode"
            className="h-11 md:h-8 px-2 bg-background border border-border rounded-md text-sm focus:outline-none" />
          <input value={city} onChange={e => setCity(e.target.value)} placeholder="City"
            className="col-span-2 h-11 md:h-8 px-2 bg-background border border-border rounded-md text-sm focus:outline-none" />
          <input value={country} onChange={e => setCountry(e.target.value)} placeholder="DE"
            maxLength={2}
            className="h-11 md:h-8 px-2 bg-background border border-border rounded-md text-sm focus:outline-none uppercase" />

          {/* On-demand scrape: opens the LLM extractor over WhatsApp+
              email passages with this contact. Cached after first run. */}
          <div className="col-span-2 sm:col-span-4">
            <AddressScrapePanel
              contactId={contact.id}
              onUse={(s) => {
                if (s.line1)    setLine1(s.line1);
                if (s.line2)    setLine2(s.line2);
                if (s.postcode) setPostcode(s.postcode);
                if (s.city)     setCity(s.city);
                if (s.country)  setCountry(s.country.toUpperCase());
              }}
            />
          </div>

          <div className="col-span-2 sm:col-span-4 flex gap-2 justify-end">
            <button
              onClick={() => setOpen(false)}
              className="text-xs px-3 py-1.5 rounded-md border border-border bg-card hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={add}
              disabled={adding || !(line1 || postcode || city)}
              className="text-xs px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
            >
              {adding ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />} Save
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

interface AddressCandidate {
  id: number;
  source_kind: "email" | "whatsapp" | string;
  source_ref: string | null;
  line1: string | null;
  line2: string | null;
  postcode: string | null;
  city: string | null;
  region: string | null;
  country: string | null;
  confidence: number | null;
  excerpt: string | null;
  scraped_at: string;
}

function AddressScrapePanel({
  contactId, onUse,
}: {
  contactId: number;
  onUse: (s: AddressCandidate) => void;
}) {
  const [data, setData] = useState<{
    scraped_at: string | null;
    from_cache?: boolean;
    candidates: AddressCandidate[];
    passages_scanned?: number;
  }>({ scraped_at: null, candidates: [] });
  const [loading, setLoading] = useState(true);
  const [scraping, setScraping] = useState(false);

  // Load cached suggestions on mount — cheap, returns instantly if no
  // prior scrape (just `{scraped_at: null, candidates: []}`).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.get<typeof data>(`/api/contacts/${contactId}/address-suggestions`);
        if (!cancelled) setData(r);
      } catch { /* silent */ }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [contactId]);

  async function runScrape() {
    setScraping(true);
    try {
      const r = await api.post<typeof data>(`/api/contacts/${contactId}/scrape-addresses`);
      setData(r);
    } catch (e: any) {
      alert(`Scrape failed: ${e.message || e}`);
    } finally { setScraping(false); }
  }

  if (loading) return null;

  return (
    <div className="rounded-md border border-dashed border-violet-500/30 bg-violet-500/5 p-2">
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <div className="text-[10px] uppercase tracking-wider text-violet-600 font-semibold flex items-center gap-1">
          <Star className="w-3 h-3" /> From your messages
        </div>
        <button
          type="button"
          onClick={runScrape}
          disabled={scraping}
          className="text-[11px] px-2 py-1 rounded-md bg-violet-500 text-white hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
          title={data.scraped_at
            ? `Last scraped ${new Date(data.scraped_at).toLocaleString()}`
            : "Yorik will scan WhatsApp + emails with this contact for any postal address"}
        >
          {scraping ? <Loader2 className="w-3 h-3 animate-spin" /> : <Search className="w-3 h-3" />}
          {data.scraped_at ? "Re-scan" : "Search messages"}
        </button>
      </div>

      {!scraping && data.scraped_at && data.candidates.length === 0 && (
        <div className="text-[11px] text-muted-foreground italic py-1">
          Yorik scanned the history and didn't find a postal address.
        </div>
      )}

      {scraping && (
        <div className="text-[11px] text-muted-foreground py-1 flex items-center gap-1.5">
          <Loader2 className="w-3 h-3 animate-spin" />
          Scanning {data.passages_scanned || ""} messages with Yorik — usually 3–5s…
        </div>
      )}

      {data.candidates.map(c => (
        <button
          key={c.id}
          type="button"
          onClick={() => onUse(c)}
          className="w-full text-left mt-1 px-2 py-1.5 rounded border border-violet-500/20 bg-background hover:bg-violet-500/10 transition flex items-start gap-2"
          title="Click to fill the address fields above"
        >
          <MapPin className="w-3 h-3 mt-0.5 text-violet-500 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="text-xs">
              {[c.line1, c.line2].filter(Boolean).join(", ")}
            </div>
            <div className="text-[10px] text-muted-foreground">
              {[c.postcode, c.city, c.region, c.country].filter(Boolean).join(" · ")}
              {c.confidence != null && (
                <span className="ml-2 opacity-60">· {Math.round(c.confidence * 100)}%</span>
              )}
              <span className="ml-2 opacity-60">· from {c.source_kind}</span>
            </div>
            {c.excerpt && (
              <div className="text-[10px] text-muted-foreground/80 italic mt-0.5 line-clamp-1">
                "{c.excerpt}"
              </div>
            )}
          </div>
        </button>
      ))}
    </div>
  );
}
