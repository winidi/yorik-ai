/**
 * Yorik Email — three-pane inbox wired to /api/email/*.
 *
 * Layout: sidebar (accounts + folders + compose) | message list (filtered
 * by selected account/folder) | reader (with HTML rendering + reply).
 *
 * State held at this level is intentionally simple — selected account,
 * selected folder, selected message, composer state — passed to children
 * as props. If/when the app grows past that, we'll factor a context or
 * adopt zustand, but not yet.
 */

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  Inbox, Star, Send, Archive, Trash2, Search, Pencil, AlertCircle,
  Reply, ReplyAll, Forward, MoreVertical, Paperclip, Plus, CornerDownRight,
  RefreshCw, Settings as SettingsIcon, AlertTriangle, Loader2,
  Mail, Folder, FileEdit, ShieldAlert, Clock, MessageSquare, Sparkles,
  MailX, Menu, ArrowLeft, Mic, Square, X,
  Bell, Calendar, Newspaper, Receipt, Users,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { useApi } from "@/lib/useApi";
import { api } from "@/lib/api";
import type {
  EmailAccount, EmailAttachment, EmailMessageRow, EmailMessageDetail, EmailFolder,
} from "./types";
import { AccountWizard } from "./AccountWizard";
import { Composer, type ComposeDraft } from "./Composer";
import { HtmlBody } from "./HtmlBody";
import { SuggestionPanel } from "./SuggestionPanel";
import { Dock } from "@/components/Dock";
import { PersonHover } from "@/components/PersonCard";

type SemanticFolder = "inbox" | "sent" | "all";

interface FolderSelection {
  /** Selected specific folder (when user clicked one in the sidebar)
   *  OR null for the semantic "unified inbox / all sent / all" views. */
  folderId: number | null;
  semantic: SemanticFolder;
  unreadOnly: boolean;
  /** Mig 024: show only starred (cross-account). */
  starredOnly?: boolean;
  /** Mig 024: show currently-snoozed messages (the dedicated view). */
  snoozedView?: boolean;
  /** Filter to messages whose classifier category is one of these.
   *  Comma-joined on the wire; on null/empty the filter is omitted. */
  categories?: string[] | null;
}


// Page size for the messages list. Picked to balance "first paint is
// fast" with "you don't have to scroll-load five times to find a thread
// from this morning". The backend caps at 200 per call.
const MESSAGES_PAGE_SIZE = 100;

// Defensive normaliser: dedupe by id (latest wins) and re-sort by
// (date_received DESC, id DESC) — mirrors the server's ORDER BY. Used
// as a safety net after every state mutation in the paged hook so no
// merge/append/race can leave the rendered list out of order. Server
// already sorts each page, but the client may *combine* pages whose
// dates overlap when polling refreshes the top while a load-more is
// in flight; without this sort, the combined array can interleave
// older mails between newer ones.
function dedupeSortRows(rows: EmailMessageRow[]): EmailMessageRow[] {
  const byId = new Map<number, EmailMessageRow>();
  for (const r of rows) byId.set(r.id, r);
  const out = Array.from(byId.values());
  out.sort((a, b) => {
    const da = a.date_received || "";
    const db = b.date_received || "";
    if (da > db) return -1;
    if (da < db) return 1;
    // Ties broken by id DESC, mirroring the server.
    return b.id - a.id;
  });
  return out;
}

// Paginated message list with append-on-loadMore + poll-the-top-only
// refresh. Replaces a single fixed-limit useApi which silently capped
// the inbox at 100. `accountIdForBackfill` is passed to /load-older so
// only the currently-viewed mailbox is backfilled from IMAP when the
// DB is exhausted ("all" view → null → backfill every account).
function usePagedMessages(
  basePath: string | null,
  accountIdForBackfill: number | null,
  pollMs: number,
) {
  const [messages, setMessages] = useState<EmailMessageRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasMoreInDb, setHasMoreInDb] = useState(true);
  const [backfilling, setBackfilling] = useState(false);

  const reqIdRef = useRef(0);
  const messagesRef = useRef<EmailMessageRow[]>([]);
  messagesRef.current = messages;
  // Track the server-side offset we'd request next on append. We can't
  // derive this from messages.length because dedupe may strip overlap
  // rows — if we recomputed offset from length after a no-op dedup, the
  // sentinel observer would re-fire the same fetch endlessly and the
  // UI would jiggle "Scroll for more" ↔ "Loading…" without progress.
  const nextOffsetRef = useRef(0);

  async function fetchPage(offset: number, mode: "replace" | "merge_top" | "append") {
    if (!basePath) return;
    const myReq = ++reqIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const sep = basePath.includes("?") ? "&" : "?";
      const url = `${basePath}${sep}offset=${offset}&limit=${MESSAGES_PAGE_SIZE}`;
      const data = await api.get<EmailMessageRow[]>(url);
      if (myReq !== reqIdRef.current) return;  // stale, discard
      if (mode === "replace") {
        setMessages(dedupeSortRows(data));
        nextOffsetRef.current = data.length;
      } else if (mode === "merge_top") {
        // Refresh the first page in place. Don't touch nextOffsetRef —
        // merge_top doesn't advance the append cursor, it just refreshes
        // the head of the list.
        setMessages(dedupeSortRows([...data, ...messagesRef.current]));
      } else {
        setMessages(dedupeSortRows([...messagesRef.current, ...data]));
        nextOffsetRef.current = offset + data.length;
      }
      // hasMoreInDb is driven by what the SERVER returned, not by what
      // dedupe kept. A full page back from the server means "more
      // exists at the next offset" regardless of whether we already had
      // some of these rows from a recent merge_top.
      const more = data.length >= MESSAGES_PAGE_SIZE;
      setHasMoreInDb(more);
    } catch (e: any) {
      if (myReq === reqIdRef.current) setError(e?.message || "fetch failed");
    } finally {
      if (myReq === reqIdRef.current) setLoading(false);
    }
  }

  // Reset + initial load when basePath changes.
  useEffect(() => {
    if (!basePath) {
      setMessages([]);
      setHasMoreInDb(true);
      nextOffsetRef.current = 0;
      return;
    }
    nextOffsetRef.current = 0;
    fetchPage(0, "replace");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [basePath]);

  // Polling for new mail — only refreshes the top page.
  useEffect(() => {
    if (!basePath || !pollMs) return;
    const id = setInterval(() => fetchPage(0, "merge_top"), pollMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [basePath, pollMs]);

  async function loadMore() {
    if (loading) return;
    await fetchPage(nextOffsetRef.current, "append");
  }

  async function loadOlderFromImap() {
    if (backfilling) return;
    setBackfilling(true);
    try {
      await api.post("/api/email/load-older", {
        count: 200,
        account_id: accountIdForBackfill,
      });
      // After IMAP backfill the DB has more rows below our current
      // tail — keep advancing from the same server-side offset we'd
      // hit next on a regular loadMore.
      await fetchPage(nextOffsetRef.current, "append");
    } catch (e: any) {
      setError(e?.message || "load-older failed");
    } finally {
      setBackfilling(false);
    }
  }

  return {
    messages,
    loading,
    error,
    hasMoreInDb,
    backfilling,
    loadMore,
    loadOlderFromImap,
    refetch: () => { nextOffsetRef.current = 0; return fetchPage(0, "replace"); },
  };
}


export function EmailApp() {
  const [showWizard, setShowWizard] = useState(false);
  const [showCleanup, setShowCleanup] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [selectedAccount, setSelectedAccount] = useState<number | "all">("all");
  const [folderSel, setFolderSel] = useState<FolderSelection>({
    folderId: null, semantic: "inbox", unreadOnly: false,
  });
  // Initial selection comes from ?msg=X (deep-link from the home
  // briefing card's bill rows). One-shot read on mount — after that
  // user clicks drive selection normally.
  const [selectedId, setSelectedId] = useState<number | null>(() => {
    try {
      const params = new URLSearchParams(window.location.search);
      const v = params.get("msg");
      return v ? Number(v) : null;
    } catch { return null; }
  });
  // Open the composer pre-filled when navigated with ?to=<addr> (or
   // ?to=<addr>&subject=<...>) — used by the "Email" icon in
   // /r/contacts and any other contact-aware caller.
   const [composer, setComposer] = useState<ComposeDraft | null>(() => {
     // Two ways to deep-link the composer on mount:
     //  (1) ?to=…&subject=… — used by the contacts page "Email" icon.
     //  (2) sessionStorage "yorik_pending_email" — used by the chat
     //      photo handoff and the documents "Send via email" button.
     //      Carries a list of server-side asset URLs the Composer
     //      mount effect fetches and attaches.
     try {
       const params = new URLSearchParams(window.location.search);
       const to = params.get("to") || "";
       const subject = params.get("subject") || "";
       let pendingAttachments: ComposeDraft["pendingAttachments"];
       const raw = sessionStorage.getItem("yorik_pending_email");
       if (raw) {
         try {
           const parsed = JSON.parse(raw);
           if (Array.isArray(parsed?.attachments)) {
             pendingAttachments = parsed.attachments;
           }
         } catch {}
         // One-shot: clear so a refresh doesn't re-attach.
         sessionStorage.removeItem("yorik_pending_email");
       }
       if (!to && !pendingAttachments) return null;
       return { to, subject, body: "", pendingAttachments };
     } catch { return null; }
   });

  const accountsApi = useApi<EmailAccount[]>("/api/email/accounts", [], 10_000);
  // Aggregate unread count across all the user's accounts, semantic
  // inbox only (is_sent=0). Drives the badge on "All inboxes" /
  // "Unread" so the sidebar shows a number without the user having to
  // click in first. Poll cadence matches the message-list poll so the
  // badge stays in sync with what the user would see.
  const inboxSummary = useApi<{ unread: number; total: number }>(
    "/api/email/inbox-summary", [], 8000,
  );
  const accounts = accountsApi.data || [];

  // Open wizard automatically on FIRST load if no accounts yet (first-
  // run nudge). The ref makes this fire exactly once per page mount;
  // without it, the accounts api polls every 10s, flipping loading
  // true→false on every cycle and re-firing this effect — which would
  // re-pop the wizard ~6×/min after the user dismissed it. Set the
  // ref to true once loading completes the first time so reopening
  // is only ever user-initiated after that.
  const autoOpenedRef = useRef(false);
  useEffect(() => {
    if (autoOpenedRef.current) return;
    if (accountsApi.loading) return;
    autoOpenedRef.current = true;  // mark "we've decided once"
    if (accounts.length === 0) setShowWizard(true);
  }, [accountsApi.loading, accounts.length]);

  // Message list — paginated. The base path is everything except the
  // offset/limit query params; usePagedMessages appends those per page.
  // Polled every 8s for the FIRST page only — IMAP IDLE pushes new mail
  // into the DB, polling just keeps the top of the list fresh without
  // refetching everything the user has scrolled past.
  const listBase = useMemo(() => {
    const params = new URLSearchParams();
    if (folderSel.folderId !== null) {
      params.set("folder_id", String(folderSel.folderId));
    } else {
      params.set("folder", folderSel.semantic);
      if (selectedAccount !== "all") params.set("account_id", String(selectedAccount));
    }
    if (folderSel.unreadOnly)  params.set("unread_only", "true");
    if (folderSel.starredOnly) params.set("starred_only", "true");
    if (folderSel.snoozedView) params.set("snoozed_view", "true");
    if (folderSel.categories?.length) {
      params.set("category", folderSel.categories.join(","));
    }
    const qs = params.toString();
    return qs ? `/api/email/messages?${qs}` : `/api/email/messages`;
  }, [selectedAccount, folderSel]);
  const listApi = usePagedMessages(
    accounts.length > 0 ? listBase : null,
    selectedAccount === "all" ? null : Number(selectedAccount),
    8000,
  );
  const messages = listApi.messages;

  // Mobile drill-down state. On md+ this is ignored; on mobile the
  // sidebar is a drawer (off by default) and the message list / reader
  // form a 2-step push: list visible when no message selected, reader
  // visible (full-screen) when one is.
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);
  // Close the drawer whenever the user picks an account / folder so
  // the next action lands on the now-filtered list, not on the menu.
  const closeDrawer = () => setMobileDrawerOpen(false);

  // ── Resizable message list (desktop only) ───────────────────────
  // The list used to be a fixed `md:w-[400px]`. Users on wide screens
  // wanted more room for previews; users on narrow screens wanted to
  // hand more space to the reader. The handle between the list and
  // the reader drags between LIST_MIN and LIST_MAX, persists to
  // localStorage, and double-click resets to the default.
  const LIST_DEFAULT = 400;
  const LIST_MIN = 280;
  const LIST_MAX = 720;
  const [isDesktop, setIsDesktop] = useState(() =>
    typeof window !== "undefined"
      && window.matchMedia("(min-width: 768px)").matches);
  const [listWidth, setListWidth] = useState<number>(() => {
    if (typeof window === "undefined") return LIST_DEFAULT;
    try {
      const raw = localStorage.getItem("yorik_email_list_width");
      const n = raw ? parseInt(raw, 10) : NaN;
      if (Number.isFinite(n)) return Math.max(LIST_MIN, Math.min(LIST_MAX, n));
    } catch {}
    return LIST_DEFAULT;
  });
  const [resizing, setResizing] = useState(false);
  const listSectionRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia("(min-width: 768px)");
    const handler = (e: MediaQueryListEvent) => setIsDesktop(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  // Persist on every change — local, fast, and avoids the stale-
  // closure trap of writing inside the mouseup handler.
  useEffect(() => {
    try { localStorage.setItem("yorik_email_list_width", String(listWidth)); } catch {}
  }, [listWidth]);

  // Drag listeners attached only while a drag is active. Disables text
  // selection + sets the body cursor so the col-resize cursor follows
  // the pointer even when it leaves the 6px handle hit area.
  useEffect(() => {
    if (!resizing) return;
    const onMove = (e: MouseEvent) => {
      const el = listSectionRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const w = e.clientX - rect.left;
      setListWidth(Math.max(LIST_MIN, Math.min(LIST_MAX, w)));
    };
    const onUp = () => setResizing(false);
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    const prevUserSelect = document.body.style.userSelect;
    const prevCursor = document.body.style.cursor;
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = prevUserSelect;
      document.body.style.cursor = prevCursor;
    };
  }, [resizing]);

  // Search state — when non-empty, replaces the folder-filtered list.
  const [search, setSearch] = useState("");
  const searchApi = useApi<EmailMessageRow[]>(
    search.trim().length >= 2 ? `/api/email/search?q=${encodeURIComponent(search)}` : null,
    [],
  );
  const visibleMessages = search.trim().length >= 2 ? (searchApi.data || []) : messages;

  // Deep-link fallback — when the URL had ?msg=<id> (timeline click
  // from a contact, briefing-bill link, etc.) the user expects to
  // see THAT message even when it's outside the current folder/
  // account filter. Fetch the message directly when the list view
  // doesn't have it; the Reader's inner fetch fills the body
  // either way, but we need at least {id, account_id} on the
  // synthetic row so it can mount.
  const fromList = useMemo(
    () => visibleMessages.find(m => m.id === selectedId),
    [visibleMessages, selectedId]
  );
  const needDeepLink = selectedId !== null && !fromList;
  const deepLinkApi = useApi<any>(
    needDeepLink ? `/api/email/messages/${selectedId}` : null,
    [selectedId, needDeepLink],
  );
  const selected: EmailMessageRow | undefined = fromList ?? (
    deepLinkApi.data
      ? {
          id:           deepLinkApi.data.id,
          account_id:   deepLinkApi.data.account_id,
          account_email: deepLinkApi.data.account_email,
          message_id:   deepLinkApi.data.message_id,
          thread_id:    deepLinkApi.data.thread_id,
          from_email:   deepLinkApi.data.from_email,
          from_name:    deepLinkApi.data.from_name,
          to_addrs:     deepLinkApi.data.to_addrs || [],
          subject:      deepLinkApi.data.subject || "",
          snippet:      deepLinkApi.data.snippet || "",
          date_received: deepLinkApi.data.date_received,
          is_unread:    !!deepLinkApi.data.is_unread,
          is_starred:   !!deepLinkApi.data.is_starred,
          is_sent:      !!deepLinkApi.data.is_sent,
          has_attachments: (deepLinkApi.data.attachments || []).length > 0,
        } as EmailMessageRow
      : undefined
  );

  // Keyboard shortcuts — j/k navigate, e archive, s star, r reply,
  // c compose, / search, gi go to inbox. Skip when typing in an
  // input/textarea/contentEditable to avoid hijacking the composer.
  const navigate = useNavigate();
  useEffect(() => {
    let lastKey = "";
    let lastKeyAt = 0;
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t) {
        const tag = t.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || t.isContentEditable) return;
      }
      // Two-key "g i" → go to inbox.
      const now = Date.now();
      if (lastKey === "g" && now - lastKeyAt < 800 && e.key === "i") {
        e.preventDefault();
        setSelectedAccount("all");
        setFolderSel({ folderId: null, semantic: "inbox", unreadOnly: false });
        setSelectedId(null);
        lastKey = ""; return;
      }
      lastKey = e.key; lastKeyAt = now;

      if (e.key === "/") {
        e.preventDefault();
        const el = document.querySelector<HTMLInputElement>("input[placeholder='Search across all email']");
        el?.focus();
      } else if (e.key === "c") {
        e.preventDefault();
        if (accounts.length > 0) setComposer({ to: "", subject: "", body: "" });
      } else if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        const idx = visibleMessages.findIndex(m => m.id === selectedId);
        const next = visibleMessages[Math.min(idx + 1, visibleMessages.length - 1)];
        if (next) setSelectedId(next.id);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        const idx = visibleMessages.findIndex(m => m.id === selectedId);
        const prev = visibleMessages[Math.max(idx - 1, 0)];
        if (prev) setSelectedId(prev.id);
      } else if (e.key === "e" && selected) {
        e.preventDefault();
        api.post(`/api/email/messages/${selected.id}/archive`, {})
          .then(() => { setSelectedId(null); listApi.refetch(); })
          .catch(() => {});
      } else if (e.key === "s" && selected) {
        e.preventDefault();
        api.patch(`/api/email/messages/${selected.id}`, { is_starred: !selected.is_starred })
          .then(() => listApi.refetch())
          .catch(() => {});
      } else if (e.key === "u" && selected) {
        // Unsnooze / mark unread shortcut — toggle read state.
        e.preventDefault();
        api.patch(`/api/email/messages/${selected.id}`, { is_unread: !selected.is_unread })
          .then(() => listApi.refetch())
          .catch(() => {});
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [accounts.length, visibleMessages, selectedId, selected, listApi, navigate]);

  // Refetch the list when accounts change (a freshly-added account
  // might have messages already in transit).
  useEffect(() => { listApi.refetch(); }, [accounts.length]);

  return (
    <div className="flex h-screen bg-background text-foreground overflow-hidden">
      {/* Mobile backdrop — closes the drawer when tapped. */}
      {mobileDrawerOpen && (
        <div
          className="md:hidden fixed inset-0 z-40 bg-black/40"
          onClick={closeDrawer}
          aria-hidden="true"
        />
      )}
      {/* ── Sidebar ─────────────────────────────────────────────── */}
      {/* Desktop: persistent 240px column. Mobile: full-height drawer
          fixed to the left, slides in from -translate-x-full. The
          mobileDrawerOpen state controls only the mobile transform —
          desktop ignores it. */}
      <aside className={cn(
        "border-r border-border flex flex-col bg-sidebar",
        "md:static md:translate-x-0 md:w-60 md:shrink-0",
        "fixed inset-y-0 left-0 z-50 w-72 transform transition-transform",
        "pb-[env(safe-area-inset-bottom)]",
        mobileDrawerOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0",
      )}>
        <div className="h-14 px-4 flex items-center justify-between border-b border-border">
          <div>
            <div className="font-bold text-lg tracking-tight leading-none">yorik</div>
            <span className="text-[10px] text-muted-foreground uppercase tracking-wider">email</span>
          </div>
          <button
            onClick={() => setShowSettings(true)}
            className="text-xs text-muted-foreground hover:text-foreground p-2 -mr-2 md:p-0 md:mr-0 rounded-md hover:bg-muted md:hover:bg-transparent inline-flex items-center justify-center"
            title="Email settings — manage connected accounts"
            aria-label="Email settings"
          >
            <SettingsIcon className="w-5 h-5 md:w-4 md:h-4" />
          </button>
        </div>

        <button
          onClick={() => setComposer({ to: "", subject: "", body: "" })}
          disabled={accounts.length === 0}
          className="m-3 flex items-center justify-center gap-2 h-10 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 disabled:opacity-50 transition"
        >
          <Pencil className="w-4 h-4" />
          Compose
        </button>

        {/* Unified semantic shortcuts at the top — work across all accounts */}
        <nav className="px-2 pb-2 space-y-0.5">
          <SidebarItem icon={Inbox} label="All inboxes"
            badge={inboxSummary.data?.unread}
            active={folderSel.folderId === null && folderSel.semantic === "inbox"
                    && !folderSel.unreadOnly && !folderSel.starredOnly && !folderSel.snoozedView
                    && selectedAccount === "all"}
            onClick={() => {
              setSelectedAccount("all");
              setFolderSel({ folderId: null, semantic: "inbox", unreadOnly: false });
            }} />
          <SidebarItem icon={AlertCircle} label="Unread"
            badge={inboxSummary.data?.unread}
            active={folderSel.folderId === null && !!folderSel.unreadOnly && !folderSel.snoozedView}
            onClick={() => {
              setSelectedAccount("all");
              setFolderSel({ folderId: null, semantic: "inbox", unreadOnly: true });
            }} />
          <SidebarItem icon={Star} label="Starred"
            active={folderSel.folderId === null && !!folderSel.starredOnly}
            onClick={() => {
              setSelectedAccount("all");
              setFolderSel({ folderId: null, semantic: "all", unreadOnly: false, starredOnly: true });
            }} />
          <SidebarItem icon={Clock} label="Snoozed"
            active={folderSel.folderId === null && !!folderSel.snoozedView}
            onClick={() => {
              setSelectedAccount("all");
              setFolderSel({ folderId: null, semantic: "all", unreadOnly: false, snoozedView: true });
            }} />
        </nav>

        {/* Categories — filter the unified inbox by the classifier's
            tag. Quick way to scan "all newsletters" or "all bills"
            without searching. Same FolderSelection state model, just
            with a categories array set. */}
        <div className="border-t border-border mt-2 pt-3 px-2">
          <div className="px-2 pb-2 text-[10px] uppercase tracking-wider text-muted-foreground font-medium">
            Categories
          </div>
          <nav className="space-y-0.5">
            {CATEGORY_NAV.map(c => {
              const navKey = [...c.categories].sort().join(",");
              const selKey = folderSel.categories
                ? [...folderSel.categories].sort().join(",")
                : "";
              const active = folderSel.folderId === null
                && selKey === navKey
                && !folderSel.starredOnly && !folderSel.snoozedView && !folderSel.unreadOnly;
              return (
                <SidebarItem
                  key={c.id}
                  icon={c.icon}
                  label={c.label}
                  active={active}
                  onClick={() => {
                    setSelectedAccount("all");
                    setFolderSel({
                      folderId: null,
                      semantic: "inbox",
                      unreadOnly: false,
                      categories: c.categories,
                    });
                  }}
                />
              );
            })}
          </nav>
        </div>

        {/* Per-account expanded folder lists */}
        <div className="border-t border-border mt-2 pt-3 px-2 flex-1 overflow-y-auto">
          <div className="px-2 pb-2 flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-medium">
              Accounts
            </span>
            <button
              onClick={() => setShowWizard(true)}
              className="text-muted-foreground hover:text-foreground"
              title="Add account"
            >
              <Plus className="w-3.5 h-3.5" />
            </button>
          </div>
          {accounts.map(a => (
            <AccountSection
              key={a.id}
              account={a}
              activeFolderId={selectedAccount === a.id ? folderSel.folderId : null}
              // Treat the per-account INBOX row as active when the user
              // has the semantic-inbox view AND has scoped to this
              // account. The semantic view's folderId is null, so
              // without this hint AccountSection has no way to know
              // INBOX is the currently-displayed folder.
              activeSemanticInbox={
                selectedAccount === a.id
                && folderSel.folderId === null
                && folderSel.semantic === "inbox"
                && !folderSel.unreadOnly
                && !folderSel.starredOnly
                && !folderSel.snoozedView
              }
              onSelectFolder={(folder) => {
                setSelectedAccount(a.id);
                // INBOX click uses the semantic filter (is_sent=0) so
                // Gmail-style accounts where the fetcher files inbound
                // mail in "All Mail" still surface here. Without this
                // the strict folder_id query returns 0 messages even
                // when the inbox semantically has new mail. Non-inbox
                // folders (Sent / Drafts / Trash / etc.) use strict
                // folder_id as before.
                if (folder.category === "inbox") {
                  setFolderSel({ folderId: null, semantic: "inbox", unreadOnly: false });
                } else {
                  setFolderSel({ folderId: folder.id, semantic: "inbox", unreadOnly: false });
                }
                setSelectedId(null);
              }}
            />
          ))}
        </div>

        <div className="mt-auto border-t border-border p-3 text-[11px] text-muted-foreground">
          {accountsApi.loading ? "Loading…"
            : accounts.length === 0 ? "Add your first account to get started."
            : `${accounts.length} account${accounts.length === 1 ? "" : "s"} · auto-syncs via IMAP IDLE`}
        </div>
      </aside>

      {/* ── Message list ────────────────────────────────────────── */}
      {/* Mobile: full-width when no message selected, hidden when a
          message is open (reader takes over). Desktop: starts at 400px,
          user-resizable via the handle below (next sibling) within
          [280, 720]. Width comes from inline style instead of a
          tailwind class so it can be dynamic; the className stops
          declaring a width on md+ to avoid fighting the style. */}
      <section
        ref={listSectionRef}
        style={isDesktop ? { width: listWidth } : undefined}
        className={cn(
          "flex-col bg-background min-w-0",
          // md:flex-none (== flex: 0 0 auto) is the key bit — it
          // overrides the mobile `flex-1` so flex-basis is `auto`
          // instead of `0%`, which lets the inline width style
          // actually win at desktop. Without this the section keeps
          // growing to fill space and the drag visibly does nothing.
          "md:flex-none md:flex",
          selectedId ? "hidden" : "flex flex-1",
        )}
      >
        {/* Mobile top bar: hamburger + folder/account label. Above the
            search bar so the user can always escape to the drawer. */}
        <div className="md:hidden h-12 px-3 flex items-center gap-2 border-b border-border">
          <button
            type="button"
            onClick={() => setMobileDrawerOpen(true)}
            className="w-10 h-10 -ml-1 rounded-md hover:bg-muted flex items-center justify-center text-muted-foreground"
            aria-label="Open folder menu"
          >
            <Menu className="w-5 h-5" />
          </button>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold truncate">
              {folderSel.starredOnly ? "Starred"
                : folderSel.snoozedView ? "Snoozed"
                : folderSel.unreadOnly ? "Unread"
                : folderSel.categories?.length
                  ? (findCategoryNav(folderSel.categories)?.label || "Filtered")
                : folderSel.semantic === "inbox" ? "Inbox"
                : folderSel.semantic === "sent" ? "Sent"
                : folderSel.semantic === "all" ? "All mail"
                : "Mail"}
            </div>
          </div>
        </div>
        <div className="h-14 px-4 flex items-center gap-3 border-b border-border">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              // Short placeholder on mobile so it fits at 375px viewport
              // width; desktop shows the more descriptive one.
              placeholder="Search…"
              aria-label="Search across all email"
              className="w-full h-11 md:h-9 pl-9 pr-3 rounded-md bg-muted text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring/40"
            />
            {search && (
              <button onClick={() => setSearch("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                aria-label="Clear search">
                ✕
              </button>
            )}
          </div>
          {folderSel.categories?.length ? (
            <button
              onClick={() => setShowCleanup(true)}
              className="hidden sm:inline-flex items-center gap-1.5 px-2.5 h-9 rounded-md border border-border text-xs text-muted-foreground hover:text-foreground hover:bg-muted shrink-0"
              title="Bulk unsubscribe / block / delete by sender"
            >
              <Sparkles className="w-3.5 h-3.5" /> Cleanup
            </button>
          ) : null}
          <button
            onClick={() => (search ? searchApi.refetch() : listApi.refetch())}
            disabled={listApi.loading || searchApi.loading}
            className="p-2 rounded-md hover:bg-muted text-muted-foreground"
            title="Refresh"
          >
            <RefreshCw className={cn("w-4 h-4", (listApi.loading || searchApi.loading) && "animate-spin")} />
          </button>
        </div>
        <MessageList
          messages={visibleMessages}
          selectedId={selectedId}
          onSelect={setSelectedId}
          loading={search ? searchApi.loading : listApi.loading}
          error={search ? searchApi.error : listApi.error}
          empty={accounts.length === 0
            ? "Add an email account to get started."
            : search ? `No results for "${search}"`
            : folderSel.snoozedView ? "Nothing snoozed right now."
            : folderSel.starredOnly ? "No starred messages yet."
            : "No messages in this view yet."}
          hasMoreInDb={listApi.hasMoreInDb}
          backfilling={listApi.backfilling}
          onLoadMore={listApi.loadMore}
          onLoadOlderFromImap={listApi.loadOlderFromImap}
          onQuickAction={async (m, action) => {
            try {
              if (action === "star" || action === "unstar") {
                await api.patch(`/api/email/messages/${m.id}`,
                  { is_starred: action === "star" });
              } else if (action === "needs_reply_on" || action === "needs_reply_off") {
                await api.patch(`/api/email/messages/${m.id}`,
                  { needs_reply: action === "needs_reply_on" });
              } else if (action === "archive") {
                await api.post(`/api/email/messages/${m.id}/archive`, {});
              } else {
                const until = snoozePresetToIso(action);
                await api.post(`/api/email/messages/${m.id}/snooze`, { until });
              }
              listApi.refetch();
            } catch (e: any) {
              alert(`Action failed: ${e?.message || e}`);
            }
          }}
        />
      </section>

      {/* ── Resize handle ──────────────────────────────────────── */}
      {/* Desktop-only thin gutter that doubles as the visual border
          between list and reader. Drag to resize; double-click to
          reset to LIST_DEFAULT. The visible 1px line stays muted at
          rest and tints primary while hovering / dragging so the
          affordance is obvious without being loud. */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize message list"
        title="Drag to resize · double-click to reset"
        onMouseDown={(e) => { e.preventDefault(); setResizing(true); }}
        onDoubleClick={() => setListWidth(LIST_DEFAULT)}
        className={cn(
          "hidden md:flex shrink-0 w-1.5 cursor-col-resize group items-stretch",
          "bg-border hover:bg-primary/50 active:bg-primary/60 transition-colors",
          resizing && "bg-primary/60",
        )}
      />

      {/* ── Reader ──────────────────────────────────────────────── */}
      {/* Mobile: hidden when no message selected (list shows instead),
          full-screen when one IS selected. Desktop: always visible
          alongside the list. */}
      <section className={cn(
        "flex-col bg-background min-w-0",
        "md:flex-1 md:flex",
        selectedId ? "flex flex-1" : "hidden md:flex md:flex-1",
      )}>
        {selected ? (
          <Reader
            messageRow={selected}
            accounts={accounts}
            onReply={(draft) => setComposer(draft)}
            onRefresh={() => { listApi.refetch(); accountsApi.refetch(); }}
            onActionDone={(closeMessage) => {
              if (closeMessage) setSelectedId(null);
              listApi.refetch();
            }}
            onBack={() => setSelectedId(null)}
          />
        ) : (
          <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
            {accounts.length === 0 ? "" : "Select a message to read it."}
          </div>
        )}
      </section>

      {showWizard && (
        <AccountWizard
          onClose={() => setShowWizard(false)}
          onSaved={() => { setShowWizard(false); accountsApi.refetch(); listApi.refetch(); }}
        />
      )}
      {showCleanup && (
        <CleanupModal
          categories={folderSel.categories || []}
          onClose={() => setShowCleanup(false)}
          onApplied={() => { setShowCleanup(false); listApi.refetch(); }}
        />
      )}
      {showSettings && (
        <EmailSettingsModal
          accounts={accounts}
          onClose={() => setShowSettings(false)}
          onAddAccount={() => { setShowSettings(false); setShowWizard(true); }}
          onDisconnected={() => {
            accountsApi.refetch();
            listApi.refetch();
          }}
        />
      )}
      {composer && accounts.length > 0 && (
        <Composer
          accounts={accounts}
          initial={composer}
          onClose={() => setComposer(null)}
          onSent={() => { listApi.refetch(); setComposer(null); }}
        />
      )}
      {/* Mobile FAB — Compose is in the sidebar drawer on desktop, but
          the drawer is hidden by default on mobile, making compose
          undiscoverable. FAB sits BOTTOM-LEFT (same convention as the
          calendar's + event FAB) so it doesn't collide with the
          globally-fixed VoiceFab at right-4. Hidden if no email
          accounts are set up — there'd be nowhere to send from. */}
      {accounts.length > 0 && !composer && !selectedId && (
        <button
          type="button"
          onClick={() => setComposer({ to: "", subject: "", body: "" })}
          className="md:hidden fixed left-4 bottom-[max(5.5rem,calc(env(safe-area-inset-bottom)+4.5rem))] z-30 w-14 h-14 rounded-full bg-primary text-primary-foreground shadow-lg flex items-center justify-center hover:opacity-90 active:scale-95 transition"
          aria-label="Compose new email"
          title="Compose"
        >
          <Pencil className="w-5 h-5" strokeWidth={2.5} />
        </button>
      )}
      <Dock activeAppId="email" />
    </div>
  );
}

// ───────────────────────── sub-components ──────────────────────────

// Message-category badge styling. Heuristic classifier in
// backend/email_classifier.py tags each incoming message. We DON'T
// show a badge for "personal" or "other" — the absence of a badge IS
// the signal that it's a normal human email needing attention.
const CATEGORY_BADGE: Record<string, { label: string; cls: string }> = {
  bill:         { label: "Bill",         cls: "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30" },
  appointment:  { label: "Appointment",  cls: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30" },
  newsletter:   { label: "Newsletter",   cls: "bg-blue-500/15 text-blue-700 dark:text-blue-300 border-blue-500/30" },
  notification: { label: "Notification", cls: "bg-slate-500/15 text-slate-600 dark:text-slate-300 border-slate-500/30" },
};

// Sidebar category quick-filters. Each entry can map to ONE OR MORE
// classifier categories — "People" bundles the two catch-all tags
// (personal + other) into the "actual humans writing to me, not
// broadcasts" view. The id is the URL-stable identifier; categories
// is what we send to the backend's ?category= filter.
const CATEGORY_NAV: Array<{ id: string; label: string; icon: any; categories: string[] }> = [
  { id: "people",       label: "People",        icon: Users,     categories: ["personal", "other"] },
  { id: "newsletter",   label: "Newsletters",   icon: Newspaper, categories: ["newsletter"] },
  { id: "bill",         label: "Bills",         icon: Receipt,   categories: ["bill"] },
  { id: "appointment",  label: "Appointments",  icon: Calendar,  categories: ["appointment"] },
  { id: "notification", label: "Notifications", icon: Bell,      categories: ["notification"] },
];

// Look up the nav entry whose categories match an active filter — used
// by the header strip and the cleanup modal to show the friendly name.
function findCategoryNav(active: string[] | null | undefined) {
  if (!active?.length) return null;
  const key = [...active].sort().join(",");
  return CATEGORY_NAV.find(c => [...c.categories].sort().join(",") === key) || null;
}

// Folder category → icon. Keeps the sidebar visually parseable at
// a glance (envelope for Inbox, paper-plane for Sent, shield for Spam, etc.).
const FOLDER_ICONS: Record<string, any> = {
  inbox:    Inbox,
  sent:     Send,
  drafts:   FileEdit,
  trash:    Trash2,
  spam:     ShieldAlert,
  archive:  Archive,
  all:      Mail,
  starred:  Star,
  custom:   Folder,
};

function AccountSection({
  account, activeFolderId, activeSemanticInbox, onSelectFolder,
}: {
  account: EmailAccount;
  activeFolderId: number | null;
  activeSemanticInbox?: boolean;
  onSelectFolder: (folder: EmailFolder) => void;
}) {
  const foldersApi = useApi<EmailFolder[]>(`/api/email/accounts/${account.id}/folders`, [], 30_000);
  const folders = foldersApi.data || [];

  // Hide Gmail-style "All Mail" — it's a virtual folder that mirrors
  // every other folder, so it just confuses the sidebar (clicking it
  // shows the same set as Inbox + Sent + Archive combined). Messages
  // remain searchable; the user doesn't lose access to anything.
  const visible = folders.filter(f => f.category !== "all");

  // Show the standard categories first in a fixed order, then custom
  // folders alphabetically. Keeps the sidebar consistent across accounts.
  const CATEGORY_ORDER: EmailFolder["category"][] = [
    "inbox", "drafts", "sent", "starred", "archive", "spam", "trash",
  ];
  const sorted = [
    ...CATEGORY_ORDER.flatMap(cat => visible.filter(f => f.category === cat)),
    ...visible.filter(f => f.category === "custom")
              .sort((a, b) => a.display_name.localeCompare(b.display_name)),
  ];

  return (
    <div className="mb-3">
      <div className={cn(
        "px-3 py-1.5 text-xs font-medium truncate flex items-center gap-2",
        account.last_error && "text-yellow-500",
      )}>
        {account.last_error && <AlertTriangle className="w-3 h-3 shrink-0" />}
        <span className="truncate">{account.display_name || account.email}</span>
      </div>
      {foldersApi.loading && folders.length === 0 && (
        <div className="px-3 py-1 text-[11px] text-muted-foreground italic">syncing folders…</div>
      )}
      {sorted.map(f => {
        const Icon = FOLDER_ICONS[f.category] || Folder;
        // INBOX is active either when the strict folder_id matches OR
        // when the parent told us the semantic-inbox view is active —
        // because INBOX click switches to semantic, the strict match
        // never fires for it.
        const isActive = f.category === "inbox"
          ? (!!activeSemanticInbox || activeFolderId === f.id)
          : activeFolderId === f.id;
        return (
          <button
            key={f.id}
            onClick={() => onSelectFolder(f)}
            className={cn(
              "w-full flex items-center gap-2 pl-5 pr-2 py-1 rounded-md text-xs text-left transition",
              isActive
                ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
                : "text-sidebar-foreground/75 hover:bg-sidebar-accent/60"
            )}
          >
            <Icon className="w-3.5 h-3.5 shrink-0" />
            <span className="flex-1 truncate">{f.display_name}</span>
            {f.unread > 0 && (
              <span className={cn(
                "text-[10px] tabular-nums",
                isActive ? "text-sidebar-accent-foreground" : "text-primary font-semibold"
              )}>
                {f.unread}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

function SidebarItem({
  icon: Icon, label, sub, active, warn, badge, onClick,
}: {
  icon: any; label: string; sub?: string; active?: boolean; warn?: boolean;
  /** Number rendered as a right-aligned badge. Hidden when undefined
   *  or 0 — keeps the sidebar quiet when nothing's unread. */
  badge?: number;
  onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full flex items-center gap-2 px-3 py-1.5 rounded-md text-sm transition text-left",
        active
          ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
          : "text-sidebar-foreground/80 hover:bg-sidebar-accent/60",
        warn && "text-yellow-500"
      )}
    >
      <Icon className="w-4 h-4 shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="truncate">{label}</div>
        {sub && <div className="text-[10px] text-muted-foreground truncate">{sub}</div>}
      </div>
      {!!badge && badge > 0 && (
        <span className={cn(
          "text-[11px] tabular-nums shrink-0",
          active ? "text-sidebar-accent-foreground" : "text-primary font-semibold",
        )}>
          {badge}
        </span>
      )}
    </button>
  );
}

function MessageList({
  messages, selectedId, onSelect, loading, error, empty,
  onQuickAction, hasMoreInDb, onLoadMore, onLoadOlderFromImap, backfilling,
}: {
  messages: EmailMessageRow[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  loading: boolean;
  error: string | null;
  empty: string;
  /** Hover quick-actions — star / archive / snooze without opening
   *  the reader. The parent applies the PATCH + refetches. */
  onQuickAction?: (msg: EmailMessageRow,
                   action: "star" | "unstar" | "archive" | "needs_reply_on" | "needs_reply_off" | "snooze-1d" | "snooze-tomorrow" | "snooze-nextweek") => void;
  /** True when more rows exist in the local DB for the current
   *  query (we returned a full page on the last fetch). */
  hasMoreInDb?: boolean;
  /** Append the next page from the DB. Invoked by the sentinel's
   *  IntersectionObserver when the user nears the bottom. */
  onLoadMore?: () => Promise<void> | void;
  /** Fall-through when the DB has nothing more: ask IMAP for an
   *  older slice and try again. */
  onLoadOlderFromImap?: () => Promise<void> | void;
  /** True while an IMAP backfill is in flight — drives the spinner
   *  next to "Loading older messages…". */
  backfilling?: boolean;
}) {
  if (error) {
    return <div className="p-4 text-sm text-destructive">{error}</div>;
  }
  if (loading && messages.length === 0) {
    return (
      <div className="p-4 space-y-3">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="flex gap-3 animate-pulse">
            <div className="w-9 h-9 rounded-full bg-muted" />
            <div className="flex-1 space-y-1.5">
              <div className="h-3 bg-muted rounded w-1/3" />
              <div className="h-3 bg-muted rounded w-2/3" />
              <div className="h-3 bg-muted rounded w-5/6" />
            </div>
          </div>
        ))}
      </div>
    );
  }
  if (!messages.length) {
    return <div className="p-8 text-center text-sm text-muted-foreground">{empty}</div>;
  }
  return (
    // pb-24: the global Dock floats fixed at bottom-center (~75px tall
    // including its safe-area offset); without bottom padding the
    // last few messages sit visually underneath it. 96px clears the
    // dock zone with a small breathing margin.
    <div className="flex-1 overflow-y-auto pb-24">
      {messages.map((m, i) => {
        // Year divider — inserted just before the first message of a
        // new (older) year. Apple-Mail-style: small muted heading +
        // hairline. Helps the user visually parse the long backward
        // scroll from 2026 → 2025 → 2024 etc.
        const thisYear = yearOf(m.date_received);
        const prevYear = i > 0 ? yearOf(messages[i - 1].date_received) : null;
        const showDivider = thisYear && thisYear !== prevYear;
        return (
        <Fragment key={m.id}>
        {showDivider && (
          <div className="sticky top-0 z-10 px-4 py-1.5 bg-muted/80 backdrop-blur-sm border-y border-border text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">
            {thisYear}
          </div>
        )}
        <div
          onClick={() => onSelect(m.id)}
          className={cn(
            "group w-full text-left px-4 py-3 border-b border-border/60 transition cursor-pointer relative",
            // Three visual states, layered:
            //  1) selected      → accent bg (wins over everything)
            //  2) unread        → faint primary tint + slightly stronger
            //                     on hover, plus a 3px leading bar below
            //  3) read (default)→ plain background
            selectedId === m.id
              ? "bg-accent"
              : m.is_unread
                ? "bg-primary/[0.04] hover:bg-primary/[0.08]"
                : "bg-background hover:bg-muted/40",
          )}
        >
          {/* Leading accent bar: only on unread + non-selected rows.
              Sits flush against the row's left edge so it acts as a
              "this is new" tick mark you can scan in a long list. */}
          {m.is_unread && selectedId !== m.id && (
            <div className="absolute left-0 top-0 bottom-0 w-[3px] bg-primary" aria-hidden />
          )}
          {/* Hover quick-actions — appear in the top-right of the row.
              Wired through onQuickAction so the parent owns the PATCH
              + list refetch. stopPropagation keeps the row from also
              firing onSelect when the user clicks an icon. */}
          {/* Quick actions — hover-revealed on desktop, always
              visible on mobile (touch devices have no hover state,
              so without this snooze + archive + star were unreachable
              from the list view). */}
          {onQuickAction && (
            <div className="absolute top-2 right-2 flex md:hidden md:group-hover:flex items-center gap-0.5 bg-background border border-border rounded-md shadow-sm">
              <QuickActionBtn
                icon={Star} label={m.is_starred ? "Unstar" : "Star"}
                active={m.is_starred}
                onClick={(e) => { e.stopPropagation(); onQuickAction(m, m.is_starred ? "unstar" : "star"); }}
              />
              <QuickActionBtn
                icon={Reply} label={m.needs_reply ? "Reply-needed off" : "Mark needs reply"}
                active={!!m.needs_reply}
                onClick={(e) => { e.stopPropagation(); onQuickAction(m, m.needs_reply ? "needs_reply_off" : "needs_reply_on"); }}
              />
              <QuickActionBtn
                icon={Archive} label="Archive"
                onClick={(e) => { e.stopPropagation(); onQuickAction(m, "archive"); }}
              />
              <SnoozeMenuBtn
                onPick={(label) => onQuickAction(m, label)}
              />
            </div>
          )}
          <div className="flex items-start gap-3">
            <PersonHover identifier={m.is_sent ? (m.to_addrs?.[0]?.email || m.from_email) : m.from_email}>
              <Avatar name={m.from_name || m.from_email} />
            </PersonHover>
            <div className="flex-1 min-w-0">
              <div className="flex items-baseline justify-between gap-2">
                <span className={cn(
                  "text-sm truncate",
                  m.is_unread ? "font-semibold text-foreground" : "text-muted-foreground",
                )}>
                  {m.is_sent ? (
                    <span className="text-muted-foreground">to {addrLabel(m.to_addrs)}</span>
                  ) : (
                    <PersonHover identifier={m.from_email}>
                      <span className="cursor-default">{m.from_name || m.from_email}</span>
                    </PersonHover>
                  )}
                </span>
                <span className="text-[11px] text-muted-foreground tabular-nums shrink-0">
                  {formatWhen(m.date_received)}
                </span>
              </div>
              <div className={cn(
                "text-sm truncate mt-0.5 flex items-center gap-1.5",
                m.is_unread ? "font-medium" : "text-muted-foreground"
              )}>
                {m.has_my_reply && (
                  <CornerDownRight
                    className="w-3 h-3 text-emerald-600 dark:text-emerald-500 shrink-0"
                    aria-label="You replied to this"
                  />
                )}
                <span className="truncate">{m.subject || "(no subject)"}</span>
                {typeof m.thread_count === "number" && m.thread_count > 1 && (
                  <span
                    className="text-[10px] tabular-nums px-1.5 rounded bg-muted text-muted-foreground shrink-0"
                    title={`${m.thread_count} messages in thread`}
                  >
                    {m.thread_count}
                  </span>
                )}
                {m.is_starred && <Star className="w-3 h-3 text-amber-500 fill-amber-500 shrink-0" />}
              </div>
              <div className="text-xs text-muted-foreground line-clamp-2 mt-0.5">
                {m.snippet}
              </div>
              <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                {m.needs_reply && (
                  <span
                    className="px-1.5 py-0.5 rounded border border-amber-500/40 bg-amber-500/[0.08] text-amber-700 dark:text-amber-400 text-[10px] font-medium leading-none inline-flex items-center gap-1"
                    title="You marked this as needing a reply"
                  >
                    <Reply className="w-2.5 h-2.5" /> reply
                  </span>
                )}
                {m.category && CATEGORY_BADGE[m.category] && (
                  <span className={cn(
                    "px-1.5 py-0.5 rounded border text-[10px] font-medium leading-none",
                    CATEGORY_BADGE[m.category].cls,
                  )}>
                    {CATEGORY_BADGE[m.category].label}
                  </span>
                )}
                {m.has_attachments && (
                  <span className="flex items-center gap-1">
                    <Paperclip className="w-3 h-3" /> attachment
                  </span>
                )}
                {(m.account_display_name || m.account_email) && (
                  <span className="opacity-60">· {m.account_display_name || m.account_email}</span>
                )}
                {m.snoozed_until && (
                  <span className="inline-flex items-center gap-1 text-violet-500"
                        title={`Snoozed until ${m.snoozed_until}`}>
                    <Clock className="w-3 h-3" /> {formatWhen(m.snoozed_until)}
                  </span>
                )}
              </div>
            </div>
          </div>
        </div>
        </Fragment>
        );
      })}
      {onLoadMore && (
        <ListBottomSentinel
          loading={!!loading}
          hasMoreInDb={!!hasMoreInDb}
          backfilling={!!backfilling}
          onLoadMore={onLoadMore}
          onLoadOlderFromImap={onLoadOlderFromImap}
        />
      )}
    </div>
  );
}


// Sentinel at the bottom of the list — drives infinite scroll. Two
// stages: while the local DB still has rows for the current query,
// onLoadMore fetches the next page. When the DB is exhausted, the
// sentinel calls onLoadOlderFromImap exactly once per "session at the
// bottom" to backfill from IMAP. Re-firing protection: an internal
// flag resets only when the sentinel leaves the viewport, so the user
// scrolling back and then to the bottom doesn't hammer IMAP.
function ListBottomSentinel({
  loading, hasMoreInDb, backfilling, onLoadMore, onLoadOlderFromImap,
}: {
  loading: boolean;
  hasMoreInDb: boolean;
  backfilling: boolean;
  onLoadMore?: () => Promise<void> | void;
  onLoadOlderFromImap?: () => Promise<void> | void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const triedBackfillRef = useRef(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const obs = new IntersectionObserver(async (entries) => {
      const e = entries[0];
      if (!e.isIntersecting) {
        triedBackfillRef.current = false;  // user scrolled away, re-arm
        return;
      }
      if (loading || backfilling) return;
      if (hasMoreInDb && onLoadMore) {
        await onLoadMore();
      } else if (!triedBackfillRef.current && onLoadOlderFromImap) {
        triedBackfillRef.current = true;
        await onLoadOlderFromImap();
      }
    }, { rootMargin: "300px" });  // pre-fetch before fully reaching bottom
    obs.observe(el);
    return () => obs.disconnect();
  }, [loading, backfilling, hasMoreInDb, onLoadMore, onLoadOlderFromImap]);

  return (
    <div ref={ref} className="px-4 py-4 flex items-center justify-center gap-2 text-xs text-muted-foreground">
      {(loading || backfilling) && <Loader2 className="w-3 h-3 animate-spin" />}
      {backfilling
        ? "Fetching older messages from server…"
        : loading
          ? "Loading…"
          : hasMoreInDb
            ? "Scroll for more"
            : "End of inbox"}
    </div>
  );
}


// Tiny hover-toolbar button for the message-list row.
function QuickActionBtn({ icon: Icon, label, active, onClick }: {
  icon: any;
  label: string;
  active?: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      className={cn(
        "p-1.5 hover:bg-muted rounded transition",
        active ? "text-amber-500" : "text-muted-foreground hover:text-foreground",
      )}
    >
      <Icon className={cn("w-3.5 h-3.5", active && "fill-current")} />
    </button>
  );
}


// Snooze menu on hover — fixed 3 presets + the option to defer to
// the reader for a custom time. Keeps the row-hover area tight.
function SnoozeMenuBtn({ onPick }: {
  onPick: (action: "snooze-1d" | "snooze-tomorrow" | "snooze-nextweek") => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen(o => !o); }}
        title="Snooze"
        className="p-1.5 hover:bg-muted rounded text-muted-foreground hover:text-foreground transition"
      >
        <Clock className="w-3.5 h-3.5" />
      </button>
      {open && (
        <div
          className="absolute top-full right-0 mt-1 min-w-[140px] rounded-md border border-border bg-popover shadow-lg z-30 overflow-hidden"
          onMouseLeave={() => setOpen(false)}
        >
          <SnoozeMenuItem label="Tomorrow morning" onClick={() => { setOpen(false); onPick("snooze-tomorrow"); }} />
          <SnoozeMenuItem label="+1 day"           onClick={() => { setOpen(false); onPick("snooze-1d"); }} />
          <SnoozeMenuItem label="Next week"        onClick={() => { setOpen(false); onPick("snooze-nextweek"); }} />
        </div>
      )}
    </div>
  );
}
function SnoozeMenuItem({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      className="w-full text-left px-3 py-1.5 text-xs hover:bg-muted transition flex items-center gap-1.5"
    >
      <Clock className="w-3 h-3 text-violet-500" />
      {label}
    </button>
  );
}

// Bulk-cleanup modal for a category-filtered inbox view. Lists every
// sender that contributed mail in this category, sorted by count
// (loudest first), so the user can blast through a year of newsletter
// build-up in one screen. Each sender has up to three independent
// toggles:
//   * Unsubscribe  — runs the existing per-message unsubscribe on the
//                    most recent message from that sender. The route
//                    already adds the sender to the blocklist on
//                    success, so toggling block is only useful when
//                    unsubscribe fails or is unavailable.
//   * Block        — adds the address to email_blocklist immediately.
//   * Delete all   — moves every existing message from this sender to
//                    Trash across ALL of the user's accounts.
// Works the same regardless of how many mailboxes are connected:
// senders are grouped by address (lower-cased), and per-sender actions
// fan out server-side over every account that has mail from them.
type CleanupSender = {
  sender_email: string;
  sender_name?: string | null;
  msg_count: number;
  last_received?: string | null;
  has_unsubscribe: boolean;
  sample_subject?: string | null;
  account_emails: string[];
};

type CleanupChoice = {
  unsubscribe: boolean;
  block: boolean;
  block_domain: boolean;
  delete_existing: boolean;
};

function CleanupModal({
  categories, onClose, onApplied,
}: {
  categories: string[];
  onClose: () => void;
  onApplied: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [senders, setSenders] = useState<CleanupSender[]>([]);
  const [choices, setChoices] = useState<Record<string, CleanupChoice>>({});
  const [applying, setApplying] = useState(false);
  const [results, setResults] = useState<Array<Record<string, any>> | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number; current?: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const qs = categories.length ? `?category=${encodeURIComponent(categories.join(","))}` : "";
    api.get<{ senders: CleanupSender[] }>(`/api/email/cleanup/senders${qs}`)
      .then(r => { if (alive) setSenders(r.senders); })
      .catch((e: any) => { if (alive) setErr(e?.message || "load failed"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [categories.join(",")]);

  function toggle(sender: string, key: keyof CleanupChoice) {
    setChoices(prev => {
      const cur = prev[sender] || { unsubscribe: false, block: false, block_domain: false, delete_existing: false };
      return { ...prev, [sender]: { ...cur, [key]: !cur[key] } };
    });
  }

  async function apply() {
    const actions = Object.entries(choices)
      .map(([sender_email, c]) => ({ sender_email, ...c }))
      .filter(a => a.unsubscribe || a.block || a.block_domain || a.delete_existing);
    if (actions.length === 0) return;
    setApplying(true); setErr(null);
    setProgress({ done: 0, total: actions.length });
    // Loop one sender at a time so the progress bar reflects real-time
    // state. Delete-all in particular can take many seconds per sender
    // (one IMAP STORE+EXPUNGE per message); batching them all into a
    // single request leaves the user staring at a frozen modal for
    // minutes. Per-sender calls are slightly slower in total (extra
    // HTTP overhead × N) but the UX win is worth it.
    const collected: Array<Record<string, any>> = [];
    for (let i = 0; i < actions.length; i++) {
      const a = actions[i];
      setProgress({ done: i, total: actions.length, current: a.sender_email });
      try {
        const r = await api.post<{ results: Array<Record<string, any>> }>(
          "/api/email/cleanup/apply", { actions: [a] });
        collected.push(...(r.results || []));
      } catch (e: any) {
        collected.push({ sender_email: a.sender_email, error: e?.message || "failed" });
      }
    }
    setProgress({ done: actions.length, total: actions.length });
    setResults(collected);
    setApplying(false);
  }

  // Selection bookkeeping for the per-column "select all" toggles.
  const selectAll = (key: keyof CleanupChoice, onlyEligible?: (s: CleanupSender) => boolean) => {
    setChoices(prev => {
      const next: Record<string, CleanupChoice> = { ...prev };
      // If everyone (eligible) already has it on, turn off; otherwise turn on.
      const eligible = senders.filter(s => !onlyEligible || onlyEligible(s));
      const allOn = eligible.every(s => next[s.sender_email]?.[key]);
      eligible.forEach(s => {
        const cur = next[s.sender_email] || { unsubscribe: false, block: false, block_domain: false, delete_existing: false };
        next[s.sender_email] = { ...cur, [key]: !allOn };
      });
      return next;
    });
  };

  const actionCount = Object.values(choices).filter(c =>
    c.unsubscribe || c.block || c.block_domain || c.delete_existing
  ).length;

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-card border border-border rounded-xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between p-5 border-b border-border">
          <div>
            <h2 className="font-semibold text-base">
              Inbox cleanup — {findCategoryNav(categories)?.label || categories.join(", ") || "filtered"}
            </h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              {senders.length} sender{senders.length === 1 ? "" : "s"} found across all connected mailboxes.
              Pick what to do with each, then Apply.
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>

        {results ? (
          <div className="flex-1 min-h-0 overflow-y-auto p-5 space-y-2">
            <div className="text-sm font-medium">Cleanup applied — results</div>
            {results.map((r, i) => (
              <div key={i} className="p-3 border border-border rounded-md text-xs space-y-1">
                <div className="font-medium">{r.sender_email}</div>
                {r.unsubscribe && (
                  <div className={cn(r.unsubscribe.ok === false ? "text-destructive" : "text-emerald-600 dark:text-emerald-500")}>
                    unsubscribe: {JSON.stringify(r.unsubscribe)}
                  </div>
                )}
                {r.blocked && (
                  <div className="text-emerald-600 dark:text-emerald-500">
                    blocked: sender={String(r.blocked.sender ?? false)}, domain={String(r.blocked.domain ?? false)}
                  </div>
                )}
                {r.deleted && (
                  <div className="text-muted-foreground">
                    deleted {r.deleted.deleted}/{r.deleted.matched} message(s)
                    {r.deleted.errors?.length ? ` · errors: ${r.deleted.errors.join("; ")}` : ""}
                  </div>
                )}
              </div>
            ))}
            <div className="pt-2">
              <button
                onClick={onApplied}
                className="px-4 h-9 rounded-md bg-primary text-primary-foreground hover:opacity-90 text-sm"
              >
                Done
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="px-5 py-2 border-b border-border flex items-center gap-3 text-xs text-muted-foreground">
              <span className="flex-1">{senders.length} senders</span>
              <button onClick={() => selectAll("unsubscribe", s => s.has_unsubscribe)} className="hover:text-foreground">
                Toggle all unsubscribe (eligible)
              </button>
              <button onClick={() => selectAll("block")} className="hover:text-foreground">
                Toggle all block
              </button>
              <button onClick={() => selectAll("delete_existing")} className="hover:text-foreground">
                Toggle all delete
              </button>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto">
              {loading && (
                <div className="p-8 text-center text-sm text-muted-foreground">
                  <Loader2 className="w-4 h-4 animate-spin inline mr-2" /> Loading senders…
                </div>
              )}
              {!loading && senders.length === 0 && (
                <div className="p-8 text-center text-sm text-muted-foreground">No senders found in this category.</div>
              )}
              {!loading && senders.map(s => {
                const c = choices[s.sender_email] || { unsubscribe: false, block: false, block_domain: false, delete_existing: false };
                return (
                  <div key={s.sender_email} className="px-5 py-3 border-b border-border/60 flex items-start gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium truncate">{s.sender_name || s.sender_email}</div>
                      <div className="text-xs text-muted-foreground truncate">
                        {s.sender_email} · {s.msg_count} mail{s.msg_count === 1 ? "" : "s"}
                        {s.account_emails.length > 1 && ` · across ${s.account_emails.length} mailboxes`}
                      </div>
                      {s.sample_subject && (
                        <div className="text-xs text-muted-foreground/80 italic truncate mt-0.5">
                          "{s.sample_subject}"
                        </div>
                      )}
                    </div>
                    <div className="flex flex-col gap-1 text-xs shrink-0 w-44">
                      <label className={cn(
                        "flex items-center gap-2",
                        !s.has_unsubscribe && "opacity-50 cursor-not-allowed",
                      )} title={s.has_unsubscribe ? "RFC 8058 / 2369 unsubscribe" : "No List-Unsubscribe header on any message from this sender"}>
                        <input
                          type="checkbox"
                          checked={c.unsubscribe}
                          disabled={!s.has_unsubscribe}
                          onChange={() => toggle(s.sender_email, "unsubscribe")}
                          className="h-3.5 w-3.5"
                        />
                        Unsubscribe
                      </label>
                      <label className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={c.block}
                          onChange={() => toggle(s.sender_email, "block")}
                          className="h-3.5 w-3.5"
                        />
                        Block sender
                      </label>
                      <label className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={c.block_domain}
                          onChange={() => toggle(s.sender_email, "block_domain")}
                          className="h-3.5 w-3.5"
                        />
                        Block domain
                      </label>
                      <label className="flex items-center gap-2 text-destructive/90">
                        <input
                          type="checkbox"
                          checked={c.delete_existing}
                          onChange={() => toggle(s.sender_email, "delete_existing")}
                          className="h-3.5 w-3.5"
                        />
                        Delete all ({s.msg_count})
                      </label>
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="p-5 border-t border-border space-y-3">
              {applying && progress && (
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>
                      Processing {progress.done} of {progress.total}
                      {progress.current && <> · <span className="text-foreground/80">{progress.current}</span></>}
                    </span>
                    <span>{Math.round((progress.done / progress.total) * 100)}%</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                    <div
                      className="h-full bg-primary transition-all duration-300"
                      style={{ width: `${(progress.done / Math.max(progress.total, 1)) * 100}%` }}
                    />
                  </div>
                </div>
              )}
              <div className="flex items-center gap-3">
                {err && <span className="text-xs text-destructive">{err}</span>}
                <div className="flex-1 text-xs text-muted-foreground">
                  {actionCount} sender{actionCount === 1 ? "" : "s"} selected
                </div>
                <button
                  onClick={onClose}
                  disabled={applying}
                  className="px-4 h-9 rounded-md hover:bg-muted text-sm disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={apply}
                  disabled={applying || actionCount === 0}
                  className="px-4 h-9 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 text-sm inline-flex items-center gap-2"
                >
                  {applying && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                  {applying ? "Applying…" : "Apply"}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}


// Banner shown above the body when the classifier tagged the message
// as 'appointment'. Single-click "Add to Calendar" — the server-side
// endpoint re-extracts the date/time and runs the same skill the bell
// notification's Accept handler would. After a successful add we show
// a soft "Added · open" line so the user has a confirmation without a
// modal toast system in play.
function AppointmentBanner({ messageId }: { messageId: number }) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ starts_at: string; event_id?: number | string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function add() {
    setBusy(true); setErr(null);
    try {
      const r = await api.post<{
        starts_at: string;
        skill_result?: { event_id?: number | string };
      }>(`/api/email/messages/${messageId}/calendar-event`);
      setDone({ starts_at: r.starts_at, event_id: r.skill_result?.event_id });
    } catch (e: any) {
      setErr(e.message || "could not add to calendar");
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="mt-3 p-2.5 rounded-md bg-emerald-500/[0.08] border border-emerald-500/30 text-xs flex items-center gap-2">
        <Clock className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-500" />
        <span className="text-emerald-700 dark:text-emerald-400">
          Added to calendar — {done.starts_at.replace("T", " ").slice(0, 16)}
        </span>
      </div>
    );
  }

  return (
    <div className="mt-3 p-2.5 rounded-md bg-sky-500/[0.08] border border-sky-500/30 text-xs flex items-center gap-2">
      <Clock className="w-3.5 h-3.5 text-sky-600 dark:text-sky-500" />
      <span className="flex-1 text-foreground/85">
        This email looks like an appointment.
      </span>
      <button
        onClick={add}
        disabled={busy}
        className="px-3 h-7 rounded bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50"
      >
        Add to calendar
      </button>
      {err && <span className="text-destructive ml-2">{err}</span>}
    </div>
  );
}


// One row in the Attachments list. Clicking the row opens the preview
// modal where the user can see the file AND decide whether to file it
// to Paperless. Inline state hint stays in the row so the user can
// tell the status of each attachment at a glance without opening the
// modal.
function AttachmentRow({
  att, onActionDone,
}: {
  att: EmailAttachment;
  onActionDone: () => void;
}) {
  const [open, setOpen] = useState(false);
  const state = att.paperless_state ?? null;

  return (
    <>
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(true); } }}
        className="p-3 border border-border rounded-lg bg-card hover:bg-muted/40 transition cursor-pointer focus:outline-none focus:ring-2 focus:ring-ring/40"
      >
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded bg-primary/10 text-primary flex items-center justify-center text-xs font-bold shrink-0">
            {ext(att.filename)}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium truncate">{att.filename || "attachment"}</div>
            <div className="text-xs text-muted-foreground truncate">
              {humanSize(att.size_bytes)} · {att.mimetype || "?"}
              {state === "auto_filed" && (
                <span className="ml-2 text-emerald-600 dark:text-emerald-500">✓ Filed (auto)</span>
              )}
              {state === "filed" && (
                <span className="ml-2 text-emerald-600 dark:text-emerald-500">✓ Filed</span>
              )}
              {state === "suggested" && (
                <span className="ml-2 text-sky-600 dark:text-sky-500">📎 Suggested</span>
              )}
              {state === "failed" && (
                <span className="ml-2 text-destructive">Upload failed</span>
              )}
              {state === "discarded" && (
                <span className="ml-2 text-muted-foreground">Not filed</span>
              )}
            </div>
          </div>
          <a
            href={`/api/email/attachments/${att.id}/download`}
            onClick={(e) => e.stopPropagation()}
            className="text-xs text-muted-foreground hover:text-foreground no-underline shrink-0"
            download
            title="Save to disk"
          >
            ↓ download
          </a>
        </div>
      </div>
      {open && (
        <AttachmentPreviewModal
          att={att}
          onClose={() => setOpen(false)}
          onActionDone={onActionDone}
        />
      )}
    </>
  );
}


// Full-screen preview modal. PDFs / images / text render inline; other
// types fall back to a Download CTA (no browser has a native viewer
// for DOCX/XLSX/etc., so we don't pretend). All Paperless actions for
// this attachment live in the footer — File / Discard / Undo / Retry
// pick themselves based on current state.
function AttachmentPreviewModal({
  att, onClose, onActionDone,
}: {
  att: EmailAttachment;
  onClose: () => void;
  onActionDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Optimistic local state so the footer updates immediately after a
  // click without waiting on the parent's refetch. parent still refetches
  // (via onActionDone) so the source-of-truth converges.
  const [localState, setLocalState] = useState<typeof att.paperless_state>(att.paperless_state ?? null);

  const inlineUrl = `/api/email/attachments/${att.id}/inline`;
  const downloadUrl = `/api/email/attachments/${att.id}/download`;
  const mt = (att.mimetype || "").toLowerCase();
  const fnLower = (att.filename || "").toLowerCase();
  const isPdf = mt === "application/pdf" || fnLower.endsWith(".pdf");
  const isImage = mt.startsWith("image/");
  const isText = mt.startsWith("text/") || fnLower.endsWith(".txt") || fnLower.endsWith(".csv");
  const canPreview = isPdf || isImage || isText;

  async function call(action: "file" | "discard" | "undo") {
    setBusy(true); setErr(null);
    try {
      if (action === "file") {
        await api.post(`/api/email/attachments/${att.id}/paperless`);
        setLocalState("filed");
      } else if (action === "discard") {
        await api.delete(`/api/email/attachments/${att.id}/paperless-suggestion`);
        setLocalState("discarded");
      } else if (action === "undo") {
        await api.post(`/api/email/attachments/${att.id}/paperless/undo`);
        setLocalState("discarded");
      }
      onActionDone();
    } catch (e: any) {
      setErr(e.message || `${action} failed`);
    } finally {
      setBusy(false);
    }
  }

  // Close on backdrop click — but NOT on Escape during action (would
  // tear the user away mid-Undo confirm). Escape is fine because the
  // user is the one initiating; backdrop click is fine too because the
  // expensive thing (preview load) is already done.
  return (
    <div
      className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-card border border-border rounded-xl shadow-2xl w-full max-w-5xl h-[90vh] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between p-4 border-b border-border shrink-0">
          <div className="min-w-0">
            <div className="font-semibold truncate">{att.filename || "attachment"}</div>
            <div className="text-xs text-muted-foreground">
              {humanSize(att.size_bytes)} · {att.mimetype || "?"}
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 hover:bg-muted rounded-md text-muted-foreground shrink-0 ml-2"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 min-h-0 bg-muted/40">
          {isPdf || isText ? (
            <>
              {/* Desktop / tablet: render inline via iframe. Mobile
                  browsers handle PDF-in-iframe poorly (iOS Safari shows
                  only the first page with no controls, Android Chrome
                  often forces a download). On narrow viewports we
                  surface a "Open PDF" CTA that hands off to the OS
                  native viewer — strictly better quality than any
                  embed we could produce, and the Add-to-Paperless
                  buttons stay reachable in the same modal once they
                  return. */}
              <iframe
                src={inlineUrl}
                className="hidden md:block w-full h-full bg-white"
                title={att.filename || "attachment"}
              />
              <div className="md:hidden h-full flex flex-col items-center justify-center gap-3 p-6 text-center text-sm text-muted-foreground">
                <div className="w-12 h-12 rounded-full bg-primary/10 text-primary flex items-center justify-center text-base font-bold">
                  {ext(att.filename)}
                </div>
                <div>Tap below to view this file in your browser or system PDF viewer.</div>
                <a
                  href={inlineUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="px-4 h-10 rounded-md bg-primary text-primary-foreground hover:opacity-90 inline-flex items-center"
                >
                  Open {isPdf ? "PDF" : "file"}
                </a>
              </div>
            </>
          ) : isImage ? (
            <div className="w-full h-full flex items-center justify-center p-4 overflow-auto">
              <img src={inlineUrl} alt={att.filename || ""} className="max-w-full max-h-full object-contain" />
            </div>
          ) : (
            <div className="h-full flex flex-col items-center justify-center text-sm text-muted-foreground gap-3 px-6 text-center">
              <div>Preview not available for {att.mimetype || "this file type"}.</div>
              <a
                href={downloadUrl}
                download
                className="px-3 h-9 rounded-md bg-primary text-primary-foreground hover:opacity-90 inline-flex items-center"
              >
                ↓ Download to view
              </a>
            </div>
          )}
        </div>

        <div className="p-4 border-t border-border flex items-center gap-2 flex-wrap shrink-0">
          {/* Left: status text. */}
          <div className="text-xs flex-1 min-w-0">
            {localState === "auto_filed" && (
              <span className="text-emerald-600 dark:text-emerald-500">✓ Filed to Paperless · auto (trusted sender)</span>
            )}
            {localState === "filed" && (
              <span className="text-emerald-600 dark:text-emerald-500">✓ Filed to Paperless</span>
            )}
            {localState === "suggested" && (
              <span className="text-sky-600 dark:text-sky-500">📎 This looks like a document worth keeping. File it to Paperless?</span>
            )}
            {localState === "discarded" && (
              <span className="text-muted-foreground">Not filed</span>
            )}
            {localState === "failed" && (
              <span className="text-destructive">Last Paperless upload failed</span>
            )}
            {err && <span className="block text-destructive mt-1">{err}</span>}
          </div>

          {/* Right: action buttons (vary by state). */}
          {(localState === "auto_filed" || localState === "filed") ? (
            <button
              onClick={() => call("undo")}
              disabled={busy}
              className="px-3 h-9 rounded-md border border-border hover:bg-muted text-sm disabled:opacity-50"
              title="Delete from Paperless"
            >
              Undo
            </button>
          ) : (
            <>
              {localState === "suggested" && (
                <button
                  onClick={() => call("discard")}
                  disabled={busy}
                  className="px-3 h-9 rounded-md border border-border hover:bg-muted text-sm disabled:opacity-50"
                >
                  Discard
                </button>
              )}
              <button
                onClick={() => call("file")}
                disabled={busy}
                className="px-3 h-9 rounded-md bg-primary text-primary-foreground hover:opacity-90 text-sm disabled:opacity-50"
              >
                {localState === "failed" ? "Retry upload" : "Add to Paperless"}
              </button>
            </>
          )}

          {canPreview && (
            <a
              href={inlineUrl}
              target="_blank"
              rel="noreferrer"
              className="px-3 h-9 rounded-md border border-border hover:bg-muted text-sm no-underline text-foreground inline-flex items-center"
              title="Open in a new browser tab"
            >
              Open in tab
            </a>
          )}
          <a
            href={downloadUrl}
            download
            className="px-3 h-9 rounded-md border border-border hover:bg-muted text-sm no-underline text-foreground inline-flex items-center"
          >
            Download
          </a>
        </div>
      </div>
    </div>
  );
}


function Reader({
  messageRow, accounts, onReply, onRefresh, onActionDone, onBack,
}: {
  messageRow: EmailMessageRow;
  accounts: EmailAccount[];
  onReply: (draft: ComposeDraft) => void;
  onRefresh: () => void;
  onActionDone: (closeMessage: boolean) => void;
  /** Mobile back-to-list. md:hidden in the toolbar; on desktop the
   *  list pane is always visible so this isn't rendered. */
  onBack?: () => void;
}) {
  // Refetch the full message detail when the selected id changes.
  const detail = useApi<EmailMessageDetail>(`/api/email/messages/${messageRow.id}`, []);
  const m = detail.data;

  // Remote-image gate. Off by default (privacy) — flipped per-message
  // when the user clicks "Show images". When the sender is on the
  // user's image-trust list (server returns images_auto_allowed=true)
  // we init to true so the banner never appears. Reset on message
  // change so a previous email's per-message "show" doesn't carry over.
  const [showImages, setShowImages] = useState(false);
  const [senderTrusted, setSenderTrusted] = useState(false);
  useEffect(() => {
    const auto = !!m?.images_auto_allowed;
    setShowImages(auto);
    setSenderTrusted(auto);
  }, [messageRow.id, m?.images_auto_allowed]);

  // cid:foo@bar -> attachment id, used by HtmlBody to resolve <img>
  // tags that reference inline attachments. Strips the angle brackets
  // some emails wrap the content-id in (RFC 2392).
  const cidMap = useMemo(() => {
    const map: Record<string, number> = {};
    (m?.attachments || []).forEach((a) => {
      if (a.content_id) {
        const cleaned = a.content_id.replace(/^<|>$/g, "");
        if (cleaned) map[cleaned] = a.id;
      }
    });
    return map;
  }, [m?.attachments]);

  const hasRemoteImages = useMemo(() => {
    if (!m?.body_html) return false;
    return /<img\b[^>]*\bsrc\s*=\s*["'][^"']*https?:/i.test(m.body_html);
  }, [m?.body_html]);
  if (detail.loading || !m) {
    return (
      <div className="p-8 space-y-3">
        <div className="h-6 bg-muted rounded w-2/3 animate-pulse" />
        <div className="h-4 bg-muted rounded w-1/3 animate-pulse" />
        <div className="h-32 bg-muted rounded animate-pulse" />
      </div>
    );
  }

  function buildReply(replyAll = false): ComposeDraft {
    const replySubject = m!.subject.toLowerCase().startsWith("re:")
      ? m!.subject : `Re: ${m!.subject}`;
    const recipients = m!.is_sent
      ? m!.to_addrs.map(a => a.email)
      : [m!.from_email];
    const ccList = replyAll
      ? [...m!.cc_addrs.map(a => a.email),
         ...(m!.is_sent ? [] : m!.to_addrs.map(a => a.email).filter(e => e !== m!.account_email))]
      : [];
    return {
      accountId: messageRow.account_id,
      to: recipients.join(", "),
      cc: ccList.length ? ccList.join(", ") : undefined,
      subject: replySubject,
      body: `\n\nOn ${m!.date_received || ""}, ${m!.from_name || m!.from_email} wrote:\n> ${(m!.body_text || "").split("\n").join("\n> ")}`,
      inReplyTo: m!.message_id || undefined,
      references: m!.message_id ? [...m!.references_ids, m!.message_id] : m!.references_ids,
    };
  }

  return (
    <>
      <div className="h-14 px-3 md:px-6 flex items-center justify-between border-b border-border gap-2">
        {/* Mobile-only back button — return to the list. The list pane
            is hidden on mobile while a message is open, so without
            this the user has no way back. */}
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="md:hidden w-10 h-10 -ml-1 rounded-md hover:bg-muted flex items-center justify-center text-muted-foreground shrink-0"
            aria-label="Back to list"
          >
            <ArrowLeft className="w-5 h-5" />
          </button>
        )}
        {/* Toolbar contents — horizontally scrollable on mobile if they
            overflow. Desktop fits everything; mobile gets a small
            scrollbar on overflow which is acceptable for a touch row. */}
        <div className="flex items-center gap-1 flex-1 min-w-0 overflow-x-auto md:overflow-visible">
          <ToolbarBtn icon={Reply} label="Reply" onClick={() => onReply(buildReply(false))} />
          <ToolbarBtn icon={ReplyAll} label="Reply all" onClick={() => onReply(buildReply(true))} />
          <ToolbarBtn icon={Forward} label="Forward"
            onClick={() => onReply({
              accountId: messageRow.account_id,
              to: "", subject: `Fwd: ${m.subject}`,
              body: `\n\n--- Forwarded message ---\nFrom: ${m.from_name || m.from_email}\nSubject: ${m.subject}\n\n${m.body_text || ""}`,
            })} />
          <div className="w-px h-5 bg-border mx-1" />
          <ToolbarBtn
            icon={Star}
            label={messageRow.is_starred ? "Unstar" : "Star"}
            active={messageRow.is_starred}
            onClick={async () => {
              try {
                await api.patch(`/api/email/messages/${messageRow.id}`, { is_starred: !messageRow.is_starred });
                onActionDone(false);
              } catch (e: any) { alert("Star failed: " + e.message); }
            }} />
          <ToolbarBtn
            icon={AlertCircle}
            label={messageRow.is_unread ? "Mark read" : "Mark unread"}
            onClick={async () => {
              try {
                await api.patch(`/api/email/messages/${messageRow.id}`, { is_unread: !messageRow.is_unread });
                onActionDone(false);
              } catch (e: any) { alert("Failed: " + e.message); }
            }} />
          <ToolbarBtn
            icon={Reply}
            label={m.needs_reply ? "Reply-needed off" : "Mark needs reply"}
            active={!!m.needs_reply}
            onClick={async () => {
              try {
                await api.patch(`/api/email/messages/${messageRow.id}`,
                  { needs_reply: !m.needs_reply });
                onActionDone(false);
              } catch (e: any) { alert("Failed: " + e.message); }
            }} />
          <ToolbarBtn
            icon={Archive}
            label="Archive"
            onClick={async () => {
              try {
                await api.post(`/api/email/messages/${messageRow.id}/archive`);
                onActionDone(true);
              } catch (e: any) { alert("Archive failed: " + e.message); }
            }} />
          <ToolbarBtn
            icon={Trash2}
            label="Delete"
            onClick={async () => {
              try {
                await api.delete(`/api/email/messages/${messageRow.id}`);
                onActionDone(true);
              } catch (e: any) { alert("Delete failed: " + e.message); }
            }} />
          {/* Unsubscribe — appears only when the message carries a
              List-Unsubscribe header. Tiered behaviour:
                one_click → backend POSTs RFC 8058 (silent, fast)
                mailto    → backend sends empty mail via SMTP
                http      → open URL in new tab (CAPTCHA etc.)
              All three ALSO add the sender to the blocklist so any
              future mail from them stops generating notifications. */}
          {m.unsubscribe && m.unsubscribe.method !== "none" && (
            <ToolbarBtn
              icon={MailX}
              label={m.unsubscribe.method === "http"
                ? "Unsubscribe (open)"
                : "Unsubscribe"}
              onClick={async () => {
                if (!confirm(
                  m.unsubscribe!.method === "http"
                    ? "Open the sender's unsubscribe page?\n\nThe sender will also be added to your block list so future emails stop generating notifications."
                    : "Unsubscribe from the newsletter and block the sender?",
                )) return;
                try {
                  const r = await api.post<{
                    ok: boolean; method: string; target: string | null;
                    blocked?: boolean;
                  }>(`/api/email/messages/${messageRow.id}/unsubscribe`);
                  if (r.method === "http" && r.target) {
                    window.open(r.target, "_blank", "noopener,noreferrer");
                  }
                  // Soft confirmation — no toast system here yet.
                  console.log("[unsubscribe]", r);
                  onActionDone(true);
                } catch (e: any) {
                  alert("Abmeldung fehlgeschlagen: " + e.message);
                }
              }} />
          )}
          <div className="w-px h-5 bg-border mx-1" />
          {/* Ask-Yorik handoff — open the chat with the email content
              pre-seeded so the LLM can summarise it, draft a reply,
              extract a task/event, etc. Uses sessionStorage seed +
              the existing chat-seed-and-send event the chat composer
              listens for. */}
          <AskYorikButton message={messageRow} detail={m} onReply={onReply} />
        </div>
        <ToolbarBtn icon={MoreVertical} label="More" onClick={() => {}} />
      </div>

      {/* Header strip: sender + subject, shrink-0 so it doesn't get squeezed.
          Body region below uses flex-1 so the iframe fills the rest of the
          available column — short emails no longer leave a tall blank gap
          between the body and the AI panel. */}
      <div className="px-6 pt-6 pb-4 border-b border-border shrink-0">
        <div className="flex items-start gap-4 mb-3">
          <PersonHover identifier={m.from_email}>
            <Avatar name={m.from_name || m.from_email} size="lg" />
          </PersonHover>
          <div className="flex-1 min-w-0">
            <PersonHover identifier={m.from_email}>
              <div className="font-semibold text-base cursor-default inline-block">{m.from_name || m.from_email}</div>
            </PersonHover>
            <div className="text-sm text-muted-foreground truncate">
              {m.from_email}
              {m.to_addrs?.length > 0 && (
                <span> · to {m.to_addrs.map(a => a.name || a.email).join(", ")}</span>
              )}
            </div>
            <div className="text-xs text-muted-foreground mt-1">
              {formatFull(m.date_received)} · via {m.account_email}
            </div>
          </div>
        </div>
        <h1 className="text-xl font-semibold break-words">{m.subject || "(no subject)"}</h1>
        {m.category === "appointment" && (
          <AppointmentBanner messageId={m.id} />
        )}
      </div>

      {/* Body region: flex-1 + min-h-0 so it can shrink AND grow inside
          the flex column. Internal scroll for long emails. */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        <div className="px-6 py-4 h-full flex flex-col">
          {hasRemoteImages && !showImages && (
            <div className="mb-3 p-2.5 rounded-md bg-amber-500/[0.08] border border-amber-500/30 text-xs flex items-center gap-2 flex-wrap shrink-0">
              <ShieldAlert className="w-3.5 h-3.5 text-amber-600 dark:text-amber-500 shrink-0" />
              <span className="flex-1 min-w-[12rem] text-foreground/85">
                Remote images blocked. Senders use these to track when you open emails.
              </span>
              <button
                onClick={() => setShowImages(true)}
                className="px-3 h-7 rounded border border-border hover:bg-muted text-xs shrink-0"
              >
                Show once
              </button>
              <button
                onClick={async () => {
                  try {
                    await api.post(`/api/email/messages/${m.id}/trust-sender-images`);
                    setSenderTrusted(true);
                    setShowImages(true);
                  } catch (e: any) {
                    // Fall back to one-time show so the user isn't stuck
                    // staring at a blocked email if the trust call fails.
                    setShowImages(true);
                    console.warn("[email] trust-sender-images failed:", e);
                  }
                }}
                className="px-3 h-7 rounded bg-primary text-primary-foreground hover:opacity-90 text-xs shrink-0"
                title={`Always show images from ${m.from_email}`}
              >
                Always from {shortAddress(m.from_email)}
              </button>
            </div>
          )}
          {hasRemoteImages && showImages && !senderTrusted && (
            <div className="mb-3 p-2 rounded-md bg-muted/40 border border-border text-xs flex items-center gap-2 shrink-0">
              <span className="flex-1 text-muted-foreground">
                Want to skip the prompt for future mail from {m.from_email}?
              </span>
              <button
                onClick={async () => {
                  try {
                    await api.post(`/api/email/messages/${m.id}/trust-sender-images`);
                    setSenderTrusted(true);
                  } catch (e: any) {
                    console.warn("[email] trust-sender-images failed:", e);
                  }
                }}
                className="px-3 h-7 rounded border border-border hover:bg-muted text-xs shrink-0"
              >
                Always show from this sender
              </button>
            </div>
          )}
          {senderTrusted && hasRemoteImages && (
            <div className="mb-3 text-[10px] text-muted-foreground uppercase tracking-wider flex items-center gap-2 shrink-0">
              <span>Images auto-shown — sender is trusted.</span>
              <button
                onClick={async () => {
                  try {
                    await api.delete(`/api/email/messages/${m.id}/trust-sender-images`);
                    setSenderTrusted(false);
                  } catch (e: any) {
                    console.warn("[email] untrust-sender-images failed:", e);
                  }
                }}
                className="text-foreground/70 hover:text-foreground underline"
              >
                Revoke
              </button>
            </div>
          )}
          <div className="flex-1 min-h-0">
            {m.body_html ? (
              <HtmlBody
                html={m.body_html}
                fill
                messageId={m.id}
                allowImages={showImages}
                cidMap={cidMap}
              />
            ) : (
              <div className="text-sm leading-relaxed whitespace-pre-wrap break-words h-full">
                {m.body_text || "(empty body)"}
              </div>
            )}
          </div>
          {m.attachments?.length > 0 && (
            <div className="mt-6 space-y-2 shrink-0">
              <div className="text-xs uppercase tracking-wider text-muted-foreground font-medium">
                Attachments
              </div>
              {m.attachments.map(att => (
                <AttachmentRow
                  key={att.id}
                  att={att}
                  onActionDone={() => detail.refetch()}
                />
              ))}
            </div>
          )}
        </div>
      </div>
      <SuggestionPanel sourceKind="email" sourceId={messageRow.id} />
      <AIDraftPanel
        messageId={messageRow.id}
        accountId={messageRow.account_id}
        message={m}
        onUseDraft={onReply}
      />
    </>
  );
}

// Per-tone tint — mirrors WhatsAppApp's TONE_TINTS so the email +
// WhatsApp draft panels feel like siblings. Subtle 10/20% so the
// buttons read as quiet UI, just easier to tell apart at a glance.
// Tailwind v4 JIT can't expand dynamic class names — full strings.
const EMAIL_TONE_TINTS: Record<string, { idle: string; active: string }> = {
  friendly: {
    idle:   "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/15",
    active: "bg-emerald-500/20 text-emerald-700 dark:text-emerald-200 ring-1 ring-emerald-500/40",
  },
  formal: {
    idle:   "bg-slate-500/10 text-slate-700 dark:text-slate-300 hover:bg-slate-500/15",
    active: "bg-slate-500/20 text-slate-700 dark:text-slate-200 ring-1 ring-slate-500/40",
  },
  quick: {
    idle:   "bg-sky-500/10 text-sky-700 dark:text-sky-300 hover:bg-sky-500/15",
    active: "bg-sky-500/20 text-sky-700 dark:text-sky-200 ring-1 ring-sky-500/40",
  },
  warm: {
    idle:   "bg-rose-500/10 text-rose-700 dark:text-rose-300 hover:bg-rose-500/15",
    active: "bg-rose-500/20 text-rose-700 dark:text-rose-200 ring-1 ring-rose-500/40",
  },
  firm: {
    idle:   "bg-amber-500/10 text-amber-700 dark:text-amber-300 hover:bg-amber-500/15",
    active: "bg-amber-500/20 text-amber-700 dark:text-amber-200 ring-1 ring-amber-500/40",
  },
};
const EMAIL_TONE_TINT_DEFAULT = {
  idle:   "bg-muted/60 text-foreground/85 hover:bg-muted",
  active: "bg-primary/15 text-primary ring-1 ring-primary/40",
};

function AIDraftPanel({
  messageId, accountId, message, onUseDraft,
}: {
  messageId: number; accountId: number; message: EmailMessageDetail;
  onUseDraft: (d: ComposeDraft) => void;
}) {
  const draftsApi = useApi<{
    group_id: string | null;
    variants: Array<{ id: number; label: string; text: string }>;
    sources: Array<{ snippet: string }>;
  }>(`/api/email/messages/${messageId}/drafts`, [], 8000);
  // Tone states served by the backend so adding/removing one is a
  // backend-only change. Same shape + keys as WhatsApp's DraftPanel.
  const statesApi = useApi<Array<{
    key: string; label_en: string; label_de: string; tone: string;
  }>>("/api/email/draft-states", []);
  // Defensive: api.get falls back to res.text() when the response
  // isn't JSON (eg the static-file handler swallowed the URL because
  // the route hadn't loaded yet on a stale uvicorn). That returns a
  // string into .data, and `string || []` keeps the string — which
  // would then crash on `.map`. Array.isArray narrows correctly.
  const states = Array.isArray(statesApi.data) ? statesApi.data : [];
  const [activeState, setActiveState] = useState<string | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const [instructions, setInstructions] = useState("");

  // Reset the picked tone when the user switches messages — past
  // tone choice on a different thread is a meaningless default here.
  useEffect(() => { setActiveState(null); }, [messageId]);

  // Inline dictation: mic toggles MediaRecorder, on stop POSTs to
  // /api/voice/transcribe and appends the transcript to the
  // instructions textarea. Same Whisper-only pipeline ChatApp's mic
  // button uses — no LLM call, just speech → text.
  const [voiceState, setVoiceState] = useState<"idle" | "recording" | "transcribing">("idle");
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const voiceStreamRef = useRef<MediaStream | null>(null);
  const voiceChunksRef = useRef<Blob[]>([]);

  const stopVoiceRecorder = () => {
    const rec = recorderRef.current;
    if (rec && rec.state !== "inactive") rec.stop();
  };
  const handleVoiceStopped = async (mimeType: string | undefined) => {
    const stream = voiceStreamRef.current;
    if (stream) stream.getTracks().forEach(t => t.stop());
    voiceStreamRef.current = null;

    const blob = new Blob(voiceChunksRef.current, { type: mimeType || "audio/webm" });
    voiceChunksRef.current = [];
    if (blob.size < 1000) {
      setVoiceError("Too short — record again.");
      setVoiceState("idle");
      return;
    }
    setVoiceState("transcribing");
    setVoiceError(null);
    try {
      const fd = new FormData();
      fd.append("audio", blob, "voice.webm");
      const r = await fetch("/api/voice/transcribe", { method: "POST", body: fd, credentials: "include" });
      if (!r.ok) {
        const j = await r.json().catch(() => ({} as any));
        throw new Error(j.detail || j.error || `HTTP ${r.status}`);
      }
      const data = await r.json() as { text: string };
      const transcript = (data.text || "").trim();
      if (!transcript) {
        setVoiceError("Empty transcript — record again.");
      } else {
        setInstructions(prev => prev.trim() ? `${prev.trim()} ${transcript}` : transcript);
      }
    } catch (e: any) {
      setVoiceError(e?.message || "Transcription failed.");
    } finally {
      setVoiceState("idle");
    }
  };
  const startVoice = async () => {
    if (voiceState !== "idle") return;
    setVoiceError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      voiceStreamRef.current = stream;
      const rec = new MediaRecorder(stream);
      recorderRef.current = rec;
      voiceChunksRef.current = [];
      rec.ondataavailable = e => { if (e.data && e.data.size) voiceChunksRef.current.push(e.data); };
      rec.onstop = () => { void handleVoiceStopped(rec.mimeType); };
      rec.start();
      setVoiceState("recording");
    } catch (e: any) {
      setVoiceError(e?.message || "Mic access denied.");
    }
  };
  // Stop the recorder on unmount so a half-open stream doesn't leak.
  useEffect(() => () => {
    const stream = voiceStreamRef.current;
    if (stream) stream.getTracks().forEach(t => t.stop());
    const rec = recorderRef.current;
    if (rec && rec.state !== "inactive") { try { rec.stop(); } catch {} }
  }, []);

  if (draftsApi.loading && !draftsApi.data) return null;
  const data = draftsApi.data;
  const variants = data?.variants || [];

  const handleUse = (v: { id: number; label: string; text: string }) => {
    const to = message.is_sent
      ? message.to_addrs.map(a => a.email).join(", ")
      : message.from_email;
    const subj = message.subject.toLowerCase().startsWith("re:")
      ? message.subject : `Re: ${message.subject}`;
    onUseDraft({
      accountId: accountId,
      to,
      subject: subj,
      body: v.text,
      inReplyTo: message.message_id || undefined,
      references: message.message_id
        ? [...message.references_ids, message.message_id]
        : message.references_ids,
    });
  };

  // Regenerate with an explicit tone state if provided, or fall back
  // to the currently-active one. `null` = no tone (original
  // brief/warm/detailed angle split). Picking a tone button is the
  // common path; the plain "regenerate" link re-runs with the
  // currently-active tone.
  const regenerate = async (overrideState?: string | null) => {
    const effectiveState =
      overrideState === undefined ? activeState : overrideState;
    if (overrideState !== undefined) setActiveState(overrideState);
    setRegenerating(true);
    try {
      await api.post(`/api/email/messages/${messageId}/drafts/regenerate`, {
        instructions: instructions.trim() || undefined,
        state: effectiveState || undefined,
      });
      await draftsApi.refetch();
    } catch {} finally { setRegenerating(false); }
  };

  const discard = async () => {
    try {
      await api.post(`/api/email/messages/${messageId}/drafts/discard`);
      await draftsApi.refetch();
    } catch {}
  };

  return (
    // mb-24: the global Dock floats fixed at bottom-center (~75px tall);
    // without this gap below the panel the regenerate row + variant
    // cards sit underneath it and the user can't read/click them.
    <div className="border-t border-border bg-muted/30 p-4 mb-24">
      <div className="flex items-center gap-2 mb-3 text-xs uppercase tracking-wider text-muted-foreground font-medium">
        <Loader2 className={cn("w-3.5 h-3.5", !regenerating && "hidden", "animate-spin")} />
        {!regenerating && <span>✨</span>}
        AI drafts
        <div className="ml-auto flex items-center gap-2">
          {variants.length > 0 && (
            <button onClick={discard} className="text-[10px] hover:text-foreground">discard</button>
          )}
          <button onClick={() => regenerate()}
            disabled={regenerating}
            className="text-[10px] hover:text-foreground flex items-center gap-1">
            <RefreshCw className={cn("w-3 h-3", regenerating && "animate-spin")} />
            {regenerating ? "thinking" : "regenerate"}
          </button>
        </div>
      </div>

      {/* Tone chips — click one to immediately regenerate 3 variants
          that all share that tone but differ in angle. Mirrors the
          WhatsApp DraftPanel pattern (same 5 keys, same tints). */}
      {states.length > 0 && (
        <div className="mb-3 grid grid-cols-5 gap-1.5">
          {states.map(s => {
            const isActive = activeState === s.key;
            const tint = EMAIL_TONE_TINTS[s.key] || EMAIL_TONE_TINT_DEFAULT;
            return (
              <button
                key={s.key}
                onClick={() => regenerate(s.key)}
                disabled={regenerating}
                title={s.tone}
                className={cn(
                  "px-2 py-1.5 rounded-md text-[11px] font-medium transition",
                  "flex flex-col items-center gap-0 leading-tight",
                  regenerating && "opacity-50 cursor-not-allowed",
                  isActive ? tint.active : tint.idle,
                )}
              >
                <span>{s.label_en}</span>
                <span className="text-[9px] opacity-70 font-normal">{s.label_de}</span>
              </button>
            );
          })}
        </div>
      )}

      {/* Intent input — optional nudge on top of the tone. User types
          (or dictates) what they actually want to say; passes to the
          email_draft skill as extra_instructions on the next
          regenerate. */}
      <div className="mb-3">
        <div className="relative">
          <textarea
            value={instructions}
            onChange={e => setInstructions(e.target.value)}
            placeholder="Tell Yorik what to say (optional) — e.g. 'decline politely, I'm on holiday until next Tuesday'"
            rows={2}
            className="w-full text-xs px-3 py-2 pr-10 rounded-md bg-background border border-border focus:outline-none focus:ring-2 focus:ring-ring/30 transition resize-none leading-relaxed"
          />
          <button
            type="button"
            onClick={voiceState === "recording" ? stopVoiceRecorder : startVoice}
            disabled={voiceState === "transcribing"}
            className={cn(
              "absolute top-1.5 right-1.5 w-7 h-7 rounded-md inline-flex items-center justify-center transition",
              voiceState === "recording"
                ? "bg-red-500/15 text-red-500 hover:bg-red-500/25"
                : voiceState === "transcribing"
                ? "bg-muted text-muted-foreground cursor-wait"
                : "text-muted-foreground hover:text-foreground hover:bg-muted",
            )}
            title={
              voiceState === "recording" ? "Stop recording" :
              voiceState === "transcribing" ? "Transcribing…" :
              "Dictate"
            }
            aria-label={
              voiceState === "recording" ? "Stop recording" :
              voiceState === "transcribing" ? "Transcribing" :
              "Dictate"
            }
          >
            {voiceState === "recording" ? (
              <Square className="w-3.5 h-3.5 fill-current" />
            ) : voiceState === "transcribing" ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Mic className="w-3.5 h-3.5" />
            )}
          </button>
        </div>
        {voiceError && (
          <div className="text-[10px] text-red-500 mt-1 flex items-center gap-1">
            <AlertCircle className="w-2.5 h-2.5" /> {voiceError}
          </div>
        )}
      </div>

      {variants.length === 0 ? (
        <div className="text-xs text-muted-foreground italic">
          No draft yet — pick a <span className="font-medium">tone</span> above
          {instructions.trim() ? " (your nudge is included)" : ""} to get 3 reply options.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
          {variants.map(v => (
            <button key={v.id}
              onClick={() => handleUse(v)}
              className="text-left p-3 rounded-md bg-card border border-border hover:border-primary/60 hover:bg-accent transition group"
            >
              <div className="text-[10px] uppercase tracking-wider font-semibold text-primary mb-1.5">
                {v.label}
              </div>
              <div className="text-xs text-foreground/90 line-clamp-4 group-hover:text-foreground whitespace-pre-line">
                {v.text}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ToolbarBtn({ icon: Icon, label, onClick, active = false }:
  { icon: any; label: string; onClick: () => void; active?: boolean }) {
  return (
    <button
      onClick={onClick}
      title={label}
      aria-label={label}
      className={cn(
        "p-2 rounded-md hover:bg-muted transition",
        active ? "text-yellow-500" : "text-muted-foreground"
      )}
    >
      <Icon className={cn("w-4 h-4", active && "fill-current")} />
    </button>
  );
}

function Avatar({ name, size = "md" }: { name: string; size?: "md" | "lg" }) {
  const initials = (name || "?")
    .split(/\s+|@/)
    .filter(Boolean)
    .slice(0, 2)
    .map(s => s[0])
    .join("")
    .toUpperCase();
  const hue = Math.abs(hash(name)) % 360;
  const dim = size === "lg" ? "w-11 h-11 text-sm" : "w-9 h-9 text-xs";
  return (
    <div
      className={cn("rounded-full flex items-center justify-center font-semibold shrink-0", dim)}
      style={{ background: `hsl(${hue} 60% 45% / 0.18)`, color: `hsl(${hue} 50% 50%)` }}
    >
      {initials}
    </div>
  );
}

// ───────────────────────── helpers ──────────────────────────────────

function hash(s: string) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h) + s.charCodeAt(i);
  return h;
}

/** Dropdown that hands the email body to /r/chat with a pre-seeded
 *  prompt. Three sub-actions cover ~all the "what now?" verbs:
 *    - Summarise → 1-line "what does this want from me?"
 *    - Draft reply → the chat creates a reply via the LLM
 *    - Create task → the chat extracts a follow-up task
 *  All three reuse the existing chat skills (compose_draft / add_task)
 *  — no new backend surface needed. */
function AskYorikButton({ message, detail, onReply }: {
  message: EmailMessageRow;
  detail: EmailMessageDetail;
  onReply: (draft: ComposeDraft) => void;
}) {
  const [open, setOpen] = useState(false);
  const [replyBusy, setReplyBusy] = useState(false);
  const navigate = useNavigate();

  function seedAndGo(prompt: string) {
    setOpen(false);
    try { sessionStorage.setItem("yorik_chat_seed", prompt); } catch {}
    navigate("/chat");
    // The chat picks up the seed via sessionStorage on mount, then
    // auto-sends. See ChatApp.tsx — same path as the onboarding wizard.
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send",
                                             { detail: { seed: prompt } }));
    }, 200);
  }

  // Cap the body so we don't blow the LLM context on a huge thread.
  const body = (detail.body_text || "").slice(0, 4000);
  const ctx = `[Email context]\n`
            + `From: ${detail.from_name || ""} <${detail.from_email}>\n`
            + `Subject: ${detail.subject}\n`
            + `Date: ${detail.date_received || ""}\n\n`
            + body;

  const summary = `${ctx}\n\n---\nSummarize this email in one sentence — what does the sender want from me?`;
  const task    = `${ctx}\n\n---\nCreate a follow-up task from this for me (title + due date if derivable from the text).`;

  // Draft a reply INLINE — trigger the existing autodraft pipeline,
  // pick the first variant, open the Composer with that text. No
  // chat detour. The AIDraftPanel under the message body is still
  // there if the user wants to compare all three variants.
  async function draftReplyInline() {
    setOpen(false);
    setReplyBusy(true);
    try {
      await api.post(`/api/email/messages/${message.id}/drafts/regenerate`);
      const r = await api.get<{
        variants: Array<{ id: number; label: string; text: string }>;
      }>(`/api/email/messages/${message.id}/drafts`);
      const first = r.variants?.[0];
      if (!first) {
        alert("Yorik konnte keinen Entwurf erstellen — schau dir den AI-Entwurfs-Bereich unter der Mail an.");
        return;
      }
      const to = detail.is_sent
        ? detail.to_addrs.map(a => a.email).join(", ")
        : detail.from_email;
      const subj = detail.subject.toLowerCase().startsWith("re:")
        ? detail.subject : `Re: ${detail.subject}`;
      onReply({
        accountId:   message.account_id,
        to,
        subject:     subj,
        body:        first.text,
        inReplyTo:   detail.message_id || undefined,
        references:  detail.message_id
          ? [...detail.references_ids, detail.message_id]
          : detail.references_ids,
      });
    } catch (e: any) {
      alert("Antwort-Entwurf fehlgeschlagen: " + (e?.message || e));
    } finally {
      setReplyBusy(false);
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        disabled={replyBusy}
        className="h-8 px-2.5 rounded-md text-xs inline-flex items-center gap-1.5 bg-violet-500/10 hover:bg-violet-500/20 text-violet-600 dark:text-violet-400 transition disabled:opacity-60"
        title="Ask Yorik about this email"
      >
        {replyBusy
          ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
          : <Sparkles className="w-3.5 h-3.5" />}
        Ask Yorik
      </button>
      {open && (
        <div
          className="absolute top-full left-0 mt-1 min-w-[220px] rounded-md border border-border bg-popover shadow-lg z-30 overflow-hidden"
          onMouseLeave={() => setOpen(false)}
        >
          <AskYorikMenuItem icon={MessageSquare} label="Summarise this email"
                            onClick={() => seedAndGo(summary)} />
          <AskYorikMenuItem icon={Reply} label="Draft a reply"
                            onClick={draftReplyInline} />
          <AskYorikMenuItem icon={AlertCircle} label="Create a follow-up task"
                            onClick={() => seedAndGo(task)} />
        </div>
      )}
    </div>
  );
}

function AskYorikMenuItem({ icon: Icon, label, onClick }: {
  icon: any; label: string; onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full text-left px-3 py-2 text-xs hover:bg-muted transition flex items-center gap-2"
    >
      <Icon className="w-3.5 h-3.5 text-violet-500" />
      {label}
    </button>
  );
}


/** Resolve a snooze preset label into an ISO datetime in the user's
 *  local timezone. "Tomorrow morning" = 08:00 next day. */
function snoozePresetToIso(preset: "snooze-1d" | "snooze-tomorrow" | "snooze-nextweek"): string {
  const d = new Date();
  if (preset === "snooze-1d") {
    d.setDate(d.getDate() + 1);
  } else if (preset === "snooze-tomorrow") {
    d.setDate(d.getDate() + 1);
    d.setHours(8, 0, 0, 0);
  } else {
    d.setDate(d.getDate() + 7);
    d.setHours(8, 0, 0, 0);
  }
  // Local-time ISO (no Z) — the backend's `datetime('now')` is local too.
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T`
       + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// Year of an ISO-ish timestamp. Returns null on missing/unparseable —
// the row simply gets no divider rather than rendering "NaN".
function yearOf(iso?: string | null): number | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  return d.getFullYear();
}

function formatWhen(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  const y = new Date(now); y.setDate(y.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  if (now.getFullYear() === d.getFullYear()) {
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  return d.toLocaleDateString([], { year: "2-digit", month: "short", day: "numeric" });
}

function formatFull(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function addrLabel(addrs: Array<{ name?: string; email: string }> | undefined): string {
  if (!addrs?.length) return "(no recipient)";
  return addrs[0].name || addrs[0].email + (addrs.length > 1 ? ` +${addrs.length - 1}` : "");
}

function ext(filename?: string): string {
  if (!filename) return "FILE";
  const m = filename.match(/\.([a-z0-9]{1,5})$/i);
  return (m ? m[1] : "FILE").toUpperCase().slice(0, 4);
}

function humanSize(n?: number): string {
  if (!n) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

// Compact a "user@host" address for tight button labels. Keeps the
// local part if it fits, otherwise shows the host so the user knows
// who they'd be trusting.
function shortAddress(email?: string | null): string {
  if (!email) return "sender";
  const at = email.indexOf("@");
  if (at < 0) return email.length > 20 ? email.slice(0, 18) + "…" : email;
  const local = email.slice(0, at);
  const host = email.slice(at + 1);
  if (local.length <= 14) return email;
  return `…@${host}`;
}



// Inline panel inside the email settings modal that exposes the
// classifier preference + the backfill control. Kept separate from
// the modal so it can be reused on a future "AI settings" page
// without dragging the account-disconnect logic with it.
function ClassifierSettingsPanel() {
  const [mode, setMode] = useState<"heuristic" | "llm" | null>(null);
  const [saving, setSaving] = useState(false);
  const [job, setJob] = useState<{ status: string; total: number; done: number; last_error?: string | null } | null>(null);
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<{ mode: "heuristic" | "llm" }>("/api/email/classifier/settings")
      .then(r => setMode(r.mode))
      .catch(() => setMode("heuristic"));
    refreshStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Poll status while a backfill is running so the progress bar
  // advances live. Stop polling once it transitions out of 'running'.
  useEffect(() => {
    if (job?.status !== "running") return;
    const id = setInterval(refreshStatus, 1500);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.status]);

  async function refreshStatus() {
    try {
      const r = await api.get<{ status: string; total: number; done: number; last_error?: string | null }>(
        "/api/email/classifier/backfill/status");
      setJob(r);
    } catch {
      // Silent — the panel still shows the toggle.
    }
  }

  async function save(next: "heuristic" | "llm") {
    setSaving(true); setErr(null);
    try {
      await api.post("/api/email/classifier/settings", { mode: next });
      setMode(next);
    } catch (e: any) {
      setErr(e?.message || "save failed");
    } finally {
      setSaving(false);
    }
  }

  async function startBackfill() {
    setStarting(true); setErr(null);
    try {
      await api.post("/api/email/classifier/backfill", {});
      // Give the server a moment to flip status to running before we
      // poll, so the bar doesn't show idle for one tick.
      setTimeout(refreshStatus, 250);
    } catch (e: any) {
      setErr(e?.message || "start failed");
    } finally {
      setStarting(false);
    }
  }

  if (mode === null) return <div className="text-xs text-muted-foreground">Loading classifier…</div>;

  const pct = job && job.total > 0 ? Math.round((job.done / job.total) * 100) : 0;
  const running = job?.status === "running";

  return (
    <div className="space-y-3">
      <div>
        <div className="text-sm font-medium">Email classifier</div>
        <div className="text-xs text-muted-foreground">
          Picks the category badge ("Newsletter", "Bill", ...) on each incoming mail.
          Heuristic is instant and offline; LLM uses your local Yorik LLM for sharper tagging.
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <button
          onClick={() => save("heuristic")}
          disabled={saving || running}
          className={cn(
            "p-3 rounded-md border text-left transition disabled:opacity-50",
            mode === "heuristic"
              ? "border-primary bg-primary/[0.06]"
              : "border-border hover:bg-muted",
          )}
        >
          <div className="text-sm font-medium">Heuristic</div>
          <div className="text-[11px] text-muted-foreground mt-0.5">
            Regex rules. ~1 ms per mail. Reliable but coarse.
          </div>
        </button>
        <button
          onClick={() => save("llm")}
          disabled={saving || running}
          className={cn(
            "p-3 rounded-md border text-left transition disabled:opacity-50",
            mode === "llm"
              ? "border-primary bg-primary/[0.06]"
              : "border-border hover:bg-muted",
          )}
        >
          <div className="text-sm font-medium">LLM (Yorik)</div>
          <div className="text-[11px] text-muted-foreground mt-0.5">
            Local Qwen via HOMEOS_LLM_BASE_URL. ~1–2 s per mail. Sharper categories.
          </div>
        </button>
      </div>

      <div className="border border-border rounded-md p-3 space-y-2 bg-muted/20">
        <div className="flex items-center gap-2">
          <div className="text-xs flex-1">
            {running ? (
              <>Reclassifying inbox — {job!.done}/{job!.total} ({pct}%)</>
            ) : job?.status === "done" ? (
              <>Last backfill complete — {job.done}/{job.total} classified.</>
            ) : job?.status === "error" ? (
              <span className="text-destructive">Last backfill errored: {job.last_error || "unknown"}</span>
            ) : (
              <>
                Reclassify existing mail with the current setting.
                {job && job.total > 0 ? <> {job.done}/{job.total} already at this version.</> : null}
              </>
            )}
          </div>
          <button
            onClick={startBackfill}
            disabled={starting || running}
            className="px-3 h-8 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 text-xs inline-flex items-center gap-1.5"
          >
            {(starting || running) && <Loader2 className="w-3 h-3 animate-spin" />}
            {running ? "Running…" : "Reclassify all"}
          </button>
        </div>
        {running && (
          <div className="h-1.5 rounded-full bg-muted overflow-hidden">
            <div
              className="h-full bg-primary transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
        )}
      </div>
      {err && <div className="text-xs text-destructive">{err}</div>}
    </div>
  );
}


// ─── EmailSettingsModal ───────────────────────────────────────────────
// Opened from the gear icon in the email-app header. Until this
// landed, the gear icon was actually a <a href="/"> labeled "Back to
// Yorik" — misleading enough that users thought they were going to
// settings (which is what gear icons mean everywhere else). Now the
// gear opens this modal, which lists every connected email account
// with the IMAP host they're using, a destructive Disconnect button
// per row (calls DELETE /api/email/accounts/{id}, the endpoint
// already exists in backend/email_routes.py), and an "Add another
// account" button that hands off to the existing AccountWizard.
//
// Disconnect is owner-only on the backend; the UI confirms before
// firing because removing an account also wipes its credential-store
// row, which can't be undone without re-entering the IMAP password.

function EmailSettingsModal({
  accounts, onClose, onAddAccount, onDisconnected,
}: {
  accounts:        EmailAccount[];
  onClose:         () => void;
  onAddAccount:    () => void;
  onDisconnected:  () => void;
}) {
  const [busyId, setBusyId]   = useState<number | null>(null);
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [error, setError]     = useState<string | null>(null);

  // Esc closes — but never while a disconnect is mid-flight (network
  // call to the IMAP credential cleanup).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && busyId === null) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busyId, onClose]);

  async function disconnect(account_id: number) {
    setBusyId(account_id);
    setError(null);
    try {
      await api.delete(`/api/email/accounts/${account_id}`);
      onDisconnected();
      setConfirmId(null);
    } catch (e: any) {
      const msg = typeof e === "string"
        ? e
        : (e?.message || e?.toString?.() || JSON.stringify(e));
      setError(`Couldn't disconnect: ${msg}`);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
      onClick={() => { if (busyId === null) onClose(); }}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl max-w-lg w-full p-6"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="flex items-start gap-3 mb-4">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center shrink-0">
            <SettingsIcon className="w-5 h-5 text-violet-500" />
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-lg font-semibold leading-tight">Email settings</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              {accounts.length === 0
                ? "No accounts connected yet."
                : `${accounts.length} account${accounts.length === 1 ? "" : "s"} connected.`}
            </p>
          </div>
          <button
            onClick={() => { if (busyId === null) onClose(); }}
            disabled={busyId !== null}
            className="text-muted-foreground hover:text-foreground transition disabled:opacity-40"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="space-y-2">
          {accounts.map(a => (
            <div
              key={a.id}
              className="border border-border rounded-lg p-3 bg-muted/20"
            >
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-sm truncate">{a.email}</div>
                  <div className="text-[11px] text-muted-foreground truncate font-mono">
                    {a.imap_host}:{a.imap_port}
                    {a.imap_ssl ? " · SSL" : ""}
                    {a.is_default ? " · default" : ""}
                  </div>
                  {a.last_error && (
                    <div className="text-[11px] text-amber-600 dark:text-amber-400 mt-1 inline-flex items-center gap-1">
                      <AlertTriangle className="w-3 h-3" />
                      <span className="truncate">{a.last_error}</span>
                    </div>
                  )}
                </div>
                {confirmId === a.id ? (
                  <div className="flex gap-1.5 shrink-0">
                    <button
                      onClick={() => disconnect(a.id)}
                      disabled={busyId !== null}
                      className="text-xs h-8 px-3 rounded-md bg-red-500 hover:bg-red-600 text-white transition disabled:opacity-50 inline-flex items-center gap-1"
                    >
                      {busyId === a.id ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3 h-3" />}
                      Confirm
                    </button>
                    <button
                      onClick={() => setConfirmId(null)}
                      disabled={busyId !== null}
                      className="text-xs h-8 px-3 rounded-md border border-border hover:bg-muted transition disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => { setConfirmId(a.id); setError(null); }}
                    disabled={busyId !== null}
                    className="text-xs h-8 px-3 rounded-md border border-red-500/30 text-red-600 dark:text-red-400 hover:bg-red-500/10 transition disabled:opacity-50 inline-flex items-center gap-1.5 shrink-0"
                    title="Remove this account from Yorik (the mailbox itself stays untouched on the IMAP server)"
                  >
                    <Trash2 className="w-3 h-3" />
                    Disconnect
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>

        {error && (
          <div className="mt-4 text-xs text-red-600 dark:text-red-400 bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
            <AlertTriangle className="w-3.5 h-3.5 inline mr-1.5 -mt-0.5" />
            {error}
          </div>
        )}

        <div className="mt-6 pt-4 border-t border-border">
          <ClassifierSettingsPanel />
        </div>

        <div className="flex justify-between items-center mt-6 pt-4 border-t border-border">
          <button
            onClick={onAddAccount}
            disabled={busyId !== null}
            className="text-sm h-9 px-3 rounded-md border border-border hover:bg-muted transition disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            <Plus className="w-3.5 h-3.5" />
            Add another account
          </button>
          <button
            onClick={() => { if (busyId === null) onClose(); }}
            disabled={busyId !== null}
            className="text-sm h-9 px-4 rounded-md bg-primary text-primary-foreground hover:opacity-90 transition disabled:opacity-50"
          >
            Done
          </button>
        </div>

        <p className="text-[11px] text-muted-foreground mt-4 leading-relaxed">
          Disconnecting removes the account from Yorik. Yorik forgets the
          IMAP/SMTP credentials and stops fetching new messages. The
          mailbox on your provider is untouched — nothing is deleted on
          their side. To restore, just add the account again.
        </p>
      </div>
    </div>
  );
}
