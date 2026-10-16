/**
 * Yorik WhatsApp — polished three-pane shell.
 *
 * Visual upgrades over the previous version:
 *  - Date separators between days
 *  - Grouped consecutive same-sender messages (one avatar per group)
 *  - Soft radial-gradient thread background
 *  - Pill-shaped composer with a circular send button
 *  - "Replying to" context above the AI drafts panel
 *  - Hover-only timestamps on non-last messages
 *  - Connection-status dot in the sidebar footer
 *  - Polished empty/loading states
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import i18n, { locale } from "@/i18n";
import {
  Loader2, Search, RefreshCw, Plus, Send, Sparkles,
  MessageSquare, Newspaper, Trash2, Download, X, Mic, FileText,
  Image as ImageIcon, CircleDot, CheckCheck, UsersRound, ShieldAlert, Check,
  Power, Paperclip, Film,
} from "lucide-react";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatTime } from "@/i18n/format";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Dock } from "@/components/Dock";
import { PersonHover } from "@/components/PersonCard";
import { SharedPhotoBanner } from "@/components/SharedPhotoBanner";
import { SuggestionPanel } from "@/apps/email/SuggestionPanel";
import { toast } from "@/components/Toast";
import { WA_PREFILL_KEY } from "@/apps/chat/WhatsAppDraftCard";
import {
  useTriPane, MobileTopBar, MobileBackdrop,
  mobileAsideLeft, mobileAsideRight,
} from "@/components/MobileShell";
import type {
  WaStatus, WaChat, WaMessage, WaPendingDrafts,
} from "./types";

export function WhatsAppApp() {
  const { t } = useTranslation();
  // Deep-link: `/r/whatsapp?chat=<jid>` selects that chat on mount.
  // Used by the briefing's "Jump to chat" actions. Read synchronously
  // so the first render already has the right activeJid — saves a
  // flash of "no chat selected" empty state.
  const initialJid = (() => {
    if (typeof window === "undefined") return null;
    const p = new URLSearchParams(window.location.search);
    return p.get("chat");
  })();
  const [activeJid, setActiveJidState] = useState<string | null>(initialJid);
  // On a phone the chat list is the screen and a chat opens on top of it
  // (like WhatsApp itself, Dirk 2026-10-01). Opening pushes ?chat= so
  // the phone's Back returns to the list; the list is the state "no
  // chat". Desktop keeps all three panes side by side.
  const phone = () => typeof window !== "undefined" && window.matchMedia("(max-width: 767px)").matches;
  const setActiveJid = useCallback((jid: string | null) => {
    setActiveJidState(jid);
    if (!phone()) return;
    const url = new URL(window.location.href);
    if (jid) {
      url.searchParams.set("chat", jid);
      window.history.pushState({ chat: jid }, "", url.toString());
    } else {
      url.searchParams.delete("chat");
      window.history.replaceState({}, "", url.toString());
    }
  }, []);
  useEffect(() => {
    const onPop = () => setActiveJidState(new URLSearchParams(window.location.search).get("chat"));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const backToList = useCallback(() => {
    if (window.history.state && window.history.state.chat) window.history.back();
    else setActiveJid(null);
  }, [setActiveJid]);
  const [draftCounts, setDraftCounts] = useState<Record<string, number>>({});
  const [showImport, setShowImport] = useState(false);
  const [showBriefing, setShowBriefing] = useState(false);
  const [showDisconnect, setShowDisconnect] = useState(false);

  const statusApi = useApi<WaStatus>("/api/whatsapp/status", []);
  const chatsApi = useApi<WaChat[]>("/api/whatsapp/chats", []);

  // Stash the live refetcher in a ref so refreshAll itself can have
  // ZERO reactive deps. Without this, the dep chain was:
  //   useEffect → refreshAll → chatsApi.refetch (changes when useApi's
  //   internal state changes) → refreshAll re-derives → useEffect re-fires
  //   → calls refreshAll → setState → re-render → … → React #300.
  // The refetch reference *should* be stable per useApi's useCallback
  // memoization, but in production something was still tripping the
  // "too many re-renders" guard intermittently on dock-click navigation.
  // Routing it through a ref severs the dep chain entirely.
  const chatsRefetchRef = useRef(chatsApi.refetch);
  chatsRefetchRef.current = chatsApi.refetch;
  const statusRefetchRef = useRef(statusApi.refetch);
  statusRefetchRef.current = statusApi.refetch;

  const refreshAll = useCallback(async () => {
    await chatsRefetchRef.current();
    // Draft-count polling removed — auto-drafts are gone (users now
    // trigger drafts per-state from the right panel), so the chat-row
    // sparkles badge has nothing to count. Stays-at-0 setState keeps
    // the existing ChatRow draftCount prop happy without a network call.
    setDraftCounts({});
  }, []);

  // Fire once on mount only — refreshAll has no deps so the effect won't
  // re-fire and there's no loop window for React's safeguard to trip on.
  useEffect(() => { refreshAll(); }, [refreshAll]);

  // Auto-sync on first connect after pairing. The bridge holds the
  // history burst it received while we weren't subscribed yet — without
  // this, the chat list stays empty until the user clicks "Sync from
  // phone" manually. We detect the actual disconnected→connected
  // transition (not the initial-mount load of an already-paired status)
  // so a normal page reload doesn't re-trigger the heavy sync.
  const wasConnectedRef = useRef<boolean | null>(null);
  const connectedHint = statusApi.data?.connected;
  useEffect(() => {
    if (connectedHint == null) return;
    if (wasConnectedRef.current === false && connectedHint === true) {
      api.post("/api/whatsapp/sync").then(() => refreshAll()).catch(() => {});
    }
    wasConnectedRef.current = connectedHint;
  }, [connectedHint, refreshAll]);

  // Coalesce WS-driven refreshes. Without this, a backlog of N message
  // events on (re)connect kicks off N parallel refreshAll calls. 400ms
  // window is fine for human-perceptible "new message arrived" UX.
  const refreshTimerRef = useRef<number | null>(null);
  const scheduleRefresh = useCallback(() => {
    if (refreshTimerRef.current != null) return;
    refreshTimerRef.current = window.setTimeout(() => {
      refreshTimerRef.current = null;
      refreshAll();
    }, 400);
  }, [refreshAll]);
  // Same idea for status events. During pairing, Baileys emits a
  // burst of "qr" / "ready" / "hello" / "disconnected" events — and
  // every QR rotation (~20s) fires another "qr". Without a debouncer
  // each event triggered a /api/whatsapp/status fetch, and a wedged
  // bridge in reconnect-storm mode would burn the 120/min general
  // rate-limit bucket within seconds, jamming every other endpoint.
  // 800ms window is short enough that humans see status update
  // promptly, long enough to collapse the reconnect bursts to one
  // fetch.
  const statusRefreshTimerRef = useRef<number | null>(null);
  const scheduleStatusRefresh = useCallback(() => {
    if (statusRefreshTimerRef.current != null) return;
    statusRefreshTimerRef.current = window.setTimeout(() => {
      statusRefreshTimerRef.current = null;
      statusRefetchRef.current();
    }, 800);
  }, []);
  useEffect(() => () => {
    if (refreshTimerRef.current != null) clearTimeout(refreshTimerRef.current);
    if (statusRefreshTimerRef.current != null) clearTimeout(statusRefreshTimerRef.current);
  }, []);

  useWaSocket(useCallback((event) => {
    if (event.type === "message" || event.type === "chat" || event.type === "drafts_updated") {
      scheduleRefresh();
    } else if (["ready", "qr", "hello", "disconnected"].includes(event.type)) {
      scheduleStatusRefresh();
    }
  }, [scheduleRefresh, scheduleStatusRefresh]));

  const chats = chatsApi.data || [];
  const status = statusApi.data;
  const needsPairing = !status?.connected;
  const activeChat = useMemo(
    () => chats.find(c => c.jid === activeJid || !!c.aliases?.includes(activeJid || "")),
    [chats, activeJid],
  );

  const tri = useTriPane();
  // A chat is "open" on a phone when it exists (or the list is still
  // loading); a stale ?chat= for a chat that is gone shows the list.
  const chatOpen = !!activeJid && (!!activeChat || !chatsApi.data);

  return (
    <div className="flex h-screen bg-background text-foreground relative">
      <MobileBackdrop show={tri.leftOpen || tri.rightOpen} onClick={tri.closeAll} />
      {/* ── Chat list ───────────────────────────────────────── */}
      <aside className={cn(
        "md:w-[330px] md:border-r border-border flex flex-col bg-sidebar shrink-0",
        // Phone: the list IS the screen while no chat is open; with a
        // chat open it is the drawer behind the ☰ (unchanged).
        chatOpen ? mobileAsideLeft(tri.leftOpen) : "max-md:w-full max-md:flex-1 max-md:min-w-0",
      )}>
        {!chatOpen && (
          <MobileTopBar
            title="WhatsApp"
            rightAction={
              <button
                onClick={refreshAll}
                className="w-11 h-11 rounded-md flex items-center justify-center text-muted-foreground"
                aria-label={t("whatsapp.header.reload")}
              >
                <RefreshCw className={cn("w-5 h-5", chatsApi.loading && "animate-spin")} />
              </button>
            }
          />
        )}
        <header className="hidden md:flex h-16 px-5 items-center justify-between border-b border-border">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-emerald-500/15 flex items-center justify-center">
              <MessageSquare className="w-4 h-4 text-emerald-500" />
            </div>
            <div>
              <div className="font-semibold leading-none">WhatsApp</div>
              <div className="text-2xs text-muted-foreground mt-0.5">
                {t("whatsapp.header.chatCount", { count: chats.length })}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-0.5">
            <SilkBtn icon={Newspaper} title={t("whatsapp.header.briefing")} onClick={() => setShowBriefing(true)} />
            <SilkBtn icon={Plus} title={t("whatsapp.header.importHistory")} onClick={() => setShowImport(true)} />
            <SilkBtn icon={Download} title={t("whatsapp.header.syncFromPhone")} onClick={async () => {
              try {
                const r = await api.post<{ chats: number; messages_ingested: number }>("/api/whatsapp/sync");
                refreshAll();
                if (r.messages_ingested) toast(t("whatsapp.header.synced", { count: r.messages_ingested }));
              } catch (e: any) { toast(t("whatsapp.header.syncFailed", { error: e.message })); }
            }} />
            <SilkBtn icon={RefreshCw} title={t("whatsapp.header.reload")} loading={chatsApi.loading} onClick={refreshAll} />
          </div>
        </header>

        <ChatListPane
          chats={chats}
          activeJid={activeJid}
          onSelect={setActiveJid}
          draftCounts={draftCounts}
          loading={chatsApi.loading}
          needsPairing={needsPairing}
          bridgeUnreachable={!!status?.bridge_unreachable}
        />

        <footer className="border-t border-border px-4 py-3 flex items-center gap-2 text-xs">
          <ConnectionDot status={status} />
          <span className="text-muted-foreground truncate flex-1">
            {status?.connected
              ? <>{t("whatsapp.footer.connectedAs")} <span className="text-foreground font-medium">{status.me?.name || status.me?.id?.split(":")[0]}</span></>
              : status?.bridge_unreachable
                ? <span className="text-yellow-500">{t("whatsapp.footer.bridgeOffline")}</span>
                : t("whatsapp.footer.notPaired")}
          </span>
          {status?.connected && (
            <button
              onClick={() => setShowDisconnect(true)}
              className="text-muted-foreground hover:text-destructive underline-offset-2 hover:underline shrink-0"
              title={t("whatsapp.footer.disconnectTitle")}
            >
              {t("whatsapp.footer.disconnect")}
            </button>
          )}
        </footer>
      </aside>

      {/* ── Thread ──────────────────────────────────────────── */}
      <section className={cn("flex-1 flex-col bg-background min-w-0 thread-bg md:flex", chatOpen ? "flex" : "hidden")}>
        <MobileTopBar
          title={activeChat?.name || (activeJid ? activeJid.split("@")[0] : "WhatsApp")}
          onBack={backToList}
          backLabel={t("whatsapp.thread.backToChats")}
          onContextClick={activeJid ? () => tri.setRightOpen(true) : undefined}
          contextLabel={t("whatsapp.thread.drafts")}
        />
        {activeJid && activeChat ? (
          <Thread
            jid={activeJid}
            chat={activeChat}
            onSent={refreshAll}
          />
        ) : (
          <EmptyThread chatsCount={chats.length} onOpenList={() => tri.setLeftOpen(true)} />
        )}
      </section>

      {/* ── AI drafts ──────────────────────────────────────── */}
      <aside className={cn(
        "w-[340px] border-l border-border flex flex-col bg-card shrink-0",
        mobileAsideRight(tri.rightOpen),
      )}>
        {activeJid ? (
          <>
            <WaSuggestions jid={activeJid} />
            <DraftPanel
              jid={activeJid}
              chat={activeChat}
              onPicked={(text) => {
                window.dispatchEvent(new CustomEvent("wa-load-draft", { detail: text }));
              }}
            />
          </>
        ) : (
          <EmptyDrafts />
        )}
      </aside>

      {needsPairing && <QrModal status={status} onRefreshStatus={statusApi.refetch} />}
      {showImport && <ImportDialog onClose={() => setShowImport(false)} onImported={refreshAll} />}
      {showBriefing && <BriefingDialog onClose={() => setShowBriefing(false)} />}
      {showDisconnect && (
        <DisconnectDialog
          currentName={status?.me?.name || status?.me?.id?.split(":")[0] || t("whatsapp.thread.unknown")}
          onClose={() => setShowDisconnect(false)}
          onDisconnected={async () => {
            setShowDisconnect(false);
            // After disconnect the bridge restarts the session fresh
            // and emits a `qr` event. Refresh status so `needsPairing`
            // flips true and the QR modal appears for the new pair.
            await statusApi.refetch();
            await refreshAll();
          }}
        />
      )}

      <SharedPhotoBanner appLabel="WhatsApp" />

      <Dock activeAppId="whatsapp" />

      <style>{`
        /* Soft conversation-area gradient — barely there, gives the
           thread a sense of depth without distracting from content. */
        .thread-bg {
          background-image:
            radial-gradient(circle at 30% 15%, hsl(160 40% 50% / 0.045), transparent 50%),
            radial-gradient(circle at 70% 85%, hsl(263 50% 60% / 0.04), transparent 50%);
        }
      `}</style>
    </div>
  );
}

// ───────────────────────── chat list ────────────────────────────────

function ChatListPane({
  chats, activeJid, onSelect, draftCounts, loading, needsPairing, bridgeUnreachable,
}: {
  chats: WaChat[];
  activeJid: string | null;
  onSelect: (jid: string) => void;
  draftCounts: Record<string, number>;
  loading: boolean;
  needsPairing: boolean;
  bridgeUnreachable: boolean;
}) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState("");

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return chats;
    return chats.filter(c =>
      (c.name || "").toLowerCase().includes(q) ||
      (c.last_message_text || "").toLowerCase().includes(q) ||
      c.jid.toLowerCase().includes(q)
    );
  }, [chats, filter]);

  // Pre-compute which display names collide across chats so the row can
  // append " · …<last 4 of jid>" to make duplicates distinguishable.
  // This is the UI half of the "messages to Tom went to brother" fix —
  // without disambiguation in the list, the user can't tell which Tom
  // they're clicking on. (Backend fix: per-jid channels in contacts hub.)
  const duplicateNames = useMemo(() => {
    const counts = new Map<string, number>();
    for (const c of chats) {
      const name = (c.name || "").trim().toLowerCase();
      if (!name) continue;
      counts.set(name, (counts.get(name) || 0) + 1);
    }
    return new Set([...counts.entries()].filter(([, n]) => n > 1).map(([k]) => k));
  }, [chats]);

  return (
    <>
      <div className="px-4 pt-3 pb-2">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <input
            value={filter}
            onChange={e => setFilter(e.target.value)}
            placeholder={t("whatsapp.list.filter")}
            className="w-full h-9 pl-9 pr-3 rounded-full bg-muted/70 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {loading && chats.length === 0 && (
          <div className="px-2 space-y-3 pt-2">
            {[1,2,3,4,5].map(i => (
              <div key={i} className="flex gap-3 p-2 animate-pulse">
                <div className="w-11 h-11 rounded-full bg-muted/60 shrink-0" />
                <div className="flex-1 space-y-2 pt-1">
                  <div className="h-3 bg-muted/60 rounded w-1/3" />
                  <div className="h-3 bg-muted/40 rounded w-5/6" />
                </div>
              </div>
            ))}
          </div>
        )}
        {!loading && filtered.length === 0 && (
          <div className="px-4 py-12 text-center text-xs text-muted-foreground">
            {bridgeUnreachable
              ? <><MessageSquare className="w-8 h-8 mx-auto mb-3 opacity-30" />{t("whatsapp.list.notConnected")}<br/>{t("whatsapp.list.startFromPairing")}</>
              : needsPairing
                ? <><MessageSquare className="w-8 h-8 mx-auto mb-3 opacity-30" />{t("whatsapp.list.scanToSync")}</>
                : chats.length === 0
                  ? <><MessageSquare className="w-8 h-8 mx-auto mb-3 opacity-30" />{t("whatsapp.list.noConversations")}<br/>{t("whatsapp.list.showUpHere")}</>
                  : t("whatsapp.list.noMatches")}
          </div>
        )}
        {filtered.map(c => (
          <ChatRow key={c.jid} chat={c}
            active={activeJid === c.jid || !!c.aliases?.includes(activeJid || "")}
            draftCount={draftCounts[c.jid] || 0}
            ambiguousName={duplicateNames.has((c.name || "").trim().toLowerCase())}
            onClick={() => onSelect(c.jid)} />
        ))}
      </div>
    </>
  );
}

/** WhatsApp writes a "~" in front of a name the other person chose for
 *  themselves, to say it is not from your address book. Yorik does the
 *  same, so a chat titled "~Ela" reads as what it is. Display only —
 *  the avatar's initials and the filter keep the plain name. */
function withPushMark(chat: WaChat, text: string) {
  return chat.name && chat.name_source === "push" ? `~${text}` : text;
}

function ChatRow({ chat, active, draftCount, ambiguousName, onClick }:
  { chat: WaChat; active: boolean; draftCount: number; ambiguousName: boolean; onClick: () => void }) {
  const { t } = useTranslation();
  const rawName = chat.name || chat.jid.split("@")[0];
  // When two chats still share a name ("Tom" / "Tom" — two people),
  // append the number's last 4 digits so the user can tell them apart.
  // A LID is no number and means nothing to a person: no suffix. The
  // same person's two addresses are one chat already (backend merge).
  const localPart = chat.jid.split("@")[0];
  const digits = localPart.replace(/\D/g, "");
  const isLid = chat.jid.endsWith("@lid");
  const suffix = ambiguousName && !isLid && digits.length >= 4
    ? ` · …${digits.slice(-4)}`
    : "";
  const name = rawName + suffix;
  const shown = withPushMark(chat, name);
  const ts = chat.last_message_ts ? new Date(chat.last_message_ts * 1000) : null;
  const tsLabel = ts ? formatChatListTime(ts) : "";
  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full text-left px-3 py-2.5 mb-0.5 rounded-lg flex items-start gap-3 transition group",
        active
          ? "bg-sidebar-accent shadow-sm"
          : "hover:bg-sidebar-accent/50"
      )}
    >
      <PersonHover identifier={chat.jid}>
        <WaAvatar jid={chat.jid} name={name} />
      </PersonHover>
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline justify-between gap-2">
          <span className={cn("text-sm truncate", chat.unread_count > 0 && "font-semibold")}
                title={shown !== name ? t("whatsapp.list.pushNameHint") : undefined}>
            {shown}
          </span>
          <span className={cn(
            "text-xs tabular-nums shrink-0",
            chat.unread_count > 0 ? "text-emerald-500 font-medium" : "text-muted-foreground"
          )}>
            {tsLabel}
          </span>
        </div>
        <div className="flex items-center gap-1.5 mt-0.5">
          <div className={cn(
            "text-xs truncate flex-1",
            chat.unread_count > 0 ? "text-foreground" : "text-muted-foreground"
          )}>
            {chat.last_message_text || <span className="italic opacity-60">—</span>}
          </div>
          {draftCount > 0 && (
            <span className="inline-flex items-center gap-0.5 text-2xs text-primary font-semibold shrink-0">
              <Sparkles className="w-2.5 h-2.5" />{draftCount}
            </span>
          )}
          {chat.unread_count > 0 && (
            <span className="inline-flex items-center justify-center text-2xs bg-emerald-500 text-white rounded-full px-1.5 min-w-[18px] h-[18px] font-semibold shrink-0">
              {chat.unread_count}
            </span>
          )}
        </div>
      </div>
    </button>
  );
}

// ───────────────────────── thread + composer ─────────────────────────

function Thread({ jid, chat, onSent }:
  { jid: string; chat: WaChat; onSent: () => void }) {
  const { t } = useTranslation();
  const msgsApi = useApi<WaMessage[]>(`/api/whatsapp/chats/${encodeURIComponent(jid)}/messages?limit=200`, [jid]);
  const messages = msgsApi.data || [];
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  // A photo, a video or a PDF waiting in the composer; the text becomes
  // its caption. One at a time, like the WhatsApp app's picker.
  const [attachment, setAttachment] = useState<File | null>(null);
  const attachmentUrl = useMemo(() => attachment ? URL.createObjectURL(attachment) : null, [attachment]);
  useEffect(() => () => { if (attachmentUrl) URL.revokeObjectURL(attachmentUrl); }, [attachmentUrl]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll behavior matching WhatsApp Web's:
  //   - Opening a chat: ALWAYS land at the bottom (latest messages first).
  //     Double rAF lets React paint the new messages before we measure
  //     scrollHeight; otherwise we'd scroll to a stale (too-small) value.
  //   - Mid-chat new messages: only auto-scroll when the user is already
  //     near the bottom (don't yank them away if they're scrolling up
  //     through history).
  const prevJidRef = useRef<string>("");
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const jidChanged = prevJidRef.current !== jid;
    prevJidRef.current = jid;
    if (jidChanged) {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          el.scrollTop = el.scrollHeight;
        });
      });
      return;
    }
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (atBottom) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
  }, [messages.length, jid]);

  useEffect(() => {
    const handler = (e: Event) => {
      const evt = e as CustomEvent;
      setText(evt.detail || "");
      composerRef.current?.focus();
    };
    window.addEventListener("wa-load-draft", handler);
    return () => window.removeEventListener("wa-load-draft", handler);
  }, []);

  // Reset composer when switching chats — unless the chat's WhatsApp
  // draft card handed over a text for this thread ("Im WhatsApp-Chat
  // öffnen"); then it lands in the composer, ready to edit and send.
  useEffect(() => {
    let prefill = "";
    try {
      prefill = sessionStorage.getItem(WA_PREFILL_KEY(jid)) || "";
      sessionStorage.removeItem(WA_PREFILL_KEY(jid));
    } catch { /* storage blocked — plain reset */ }
    setText(prefill);
    setAttachment(null);
    if (prefill) composerRef.current?.focus();
  }, [jid]);

  function pickFile(f: File | null | undefined) {
    if (!f) return;
    const ok = f.type.startsWith("image/") || f.type.startsWith("video/") || f.type === "application/pdf";
    if (!ok) { toast(t("whatsapp.thread.attachOnly")); return; }
    setAttachment(f);
    composerRef.current?.focus();
  }

  async function send() {
    const body = text.trim();
    if (!body && !attachment) return;
    setSending(true);
    try {
      if (attachment) {
        const fd = new FormData();
        fd.append("file", attachment, attachment.name);
        fd.append("caption", body);
        await api.postForm(`/api/whatsapp/chats/${encodeURIComponent(jid)}/send-media`, fd);
        setAttachment(null);
      } else {
        await api.post(`/api/whatsapp/chats/${encodeURIComponent(jid)}/send`, { text: body });
      }
      setText("");
      await msgsApi.refetch();
      onSent();
    } catch (e: any) {
      toast(t("whatsapp.thread.sendFailed", { error: e.message }));
    } finally {
      setSending(false);
    }
  }

  const name = chat.name || jid.split("@")[0];
  const shown = withPushMark(chat, name);

  return (
    <>
      {/* Thread header */}
      <header className="hidden md:flex h-16 px-6 items-center gap-3 border-b border-border bg-card/40 backdrop-blur-sm">
        <PersonHover identifier={jid}>
          <WaAvatar jid={jid} name={name} size="lg" />
        </PersonHover>
        <div className="flex-1 min-w-0">
          <PersonHover identifier={jid}>
            <div className="font-semibold leading-none cursor-default inline-block"
                 title={shown !== name ? t("whatsapp.list.pushNameHint") : undefined}>{shown}</div>
          </PersonHover>
          <div className="text-xs text-muted-foreground mt-1 truncate">
            {chat.is_group ? t("whatsapp.thread.groupChat") : jid.replace(/@.+/, "")}
            <span className="ml-2 opacity-60">· {t("whatsapp.thread.messageCount", { count: messages.length })}</span>
          </div>
        </div>
      </header>

      {/* Contacts hub banner — 1:1 chats only. Pending → triage CTAs.
          Unknown → "save to contacts?". Active/spam → nothing. */}
      {!chat.is_group && (
        <ContactBanner jid={jid} fallbackName={chat.name || ""} />
      )}

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        <div className="px-6 py-6 max-w-3xl mx-auto">
          {msgsApi.loading && messages.length === 0 && (
            <div className="text-center text-xs text-muted-foreground py-12">{t("whatsapp.thread.loading")}</div>
          )}
          <MessageStream messages={messages} contactName={name} isGroup={!!chat.is_group} />
        </div>
      </div>

      {/* Composer — pb-20 reserves space for the floating bottom dock
          (~70px of dock + padding); without it the dock sits on top of
          the textarea + send button. */}
      <form
        onSubmit={(e) => { e.preventDefault(); send(); }}
        className="border-t border-border bg-card/40 backdrop-blur-sm px-6 pt-3 pb-20"
      >
        {attachment && (
          <div className="mb-2 flex items-center gap-3 rounded-xl bg-muted/70 p-2 pr-3 max-w-md">
            {attachment.type.startsWith("image/") && attachmentUrl ? (
              <img src={attachmentUrl} alt="" className="w-16 h-16 rounded-lg object-cover shrink-0" />
            ) : attachment.type.startsWith("video/") && attachmentUrl ? (
              <video src={attachmentUrl} muted playsInline className="w-16 h-16 rounded-lg object-cover bg-black/40 shrink-0" />
            ) : (
              <div className="w-16 h-16 rounded-lg bg-black/10 dark:bg-white/10 flex items-center justify-center shrink-0">
                {attachment.type.startsWith("video/") ? <Film className="w-6 h-6" /> : <FileText className="w-6 h-6" />}
              </div>
            )}
            <div className="min-w-0 flex-1">
              <div className="text-sm truncate">{attachment.name}</div>
              <div className="text-xs text-muted-foreground">
                {attachment.type.startsWith("image/") ? t("whatsapp.thread.attachment.photo") : attachment.type.startsWith("video/") ? t("whatsapp.thread.attachment.video") : t("whatsapp.thread.attachment.pdf")}
                {" · "}{t("whatsapp.thread.attachment.size", { size: (attachment.size / (1024 * 1024)).toFixed(attachment.size > 10 * 1024 * 1024 ? 0 : 1) })}
              </div>
            </div>
            <button type="button" aria-label={t("whatsapp.thread.attachment.remove")} onClick={() => setAttachment(null)}
                    className="p-1.5 rounded-full hover:bg-black/10 dark:hover:bg-white/10 shrink-0">
              <X className="w-4 h-4" />
            </button>
          </div>
        )}
        <div className="flex items-end gap-3">
        <input
          ref={fileInputRef}
          type="file"
          accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime,video/3gpp,video/webm,application/pdf"
          className="hidden"
          onChange={(e) => { pickFile(e.target.files?.[0]); e.target.value = ""; }}
        />
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={sending}
          aria-label={t("whatsapp.thread.attachment.attachAria")}
          title={t("whatsapp.thread.attachment.attachTitle")}
          className="w-11 h-11 rounded-full flex items-center justify-center shrink-0 text-muted-foreground hover:bg-muted hover:text-foreground transition"
        >
          <Paperclip className="w-5 h-5" />
        </button>
        <div className="flex-1 relative">
          <textarea
            ref={composerRef}
            value={text}
            onChange={e => setText(e.target.value)}
            onPaste={(e) => {
              const f = Array.from(e.clipboardData?.files || []).find(x => x.type.startsWith("image/") || x.type.startsWith("video/") || x.type === "application/pdf");
              if (f) { e.preventDefault(); pickFile(f); }
            }}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault(); send();
              } else if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault(); send();
              }
            }}
            placeholder={attachment ? t("whatsapp.thread.captionPlaceholder") : t("whatsapp.thread.messagePlaceholder")}
            rows={1}
            className="w-full px-4 py-2.5 rounded-2xl bg-muted/70 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 resize-none min-h-[42px] max-h-32 leading-relaxed"
            style={{ height: "auto" }}
            onInput={(e) => {
              const el = e.currentTarget;
              el.style.height = "auto";
              el.style.height = Math.min(el.scrollHeight, 128) + "px";
            }}
          />
        </div>
        <button
          type="submit"
          disabled={(!text.trim() && !attachment) || sending}
          aria-label={t("whatsapp.thread.send")}
          className={cn(
            "w-11 h-11 rounded-full flex items-center justify-center transition shrink-0",
            (text.trim() || attachment) && !sending
              ? "bg-emerald-500 text-white hover:bg-emerald-600 shadow-md hover:shadow-lg"
              : "bg-muted text-muted-foreground"
          )}
        >
          {sending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4 -translate-x-px translate-y-px" />}
        </button>
        </div>
      </form>
    </>
  );
}

/**
 * Renders messages with date separators between days and grouping of
 * consecutive same-sender messages (one avatar per group, tighter
 * spacing within a group). This is the single biggest visual win
 * over the previous "every message is its own card" rendering.
 */
function MessageStream({ messages, contactName, isGroup = false }:
  { messages: WaMessage[]; contactName: string; isGroup?: boolean }) {
  const grouped: React.ReactNode[] = [];
  let lastDateKey = "";
  let lastSender: string | null = null;
  let lastFromMe: number | null = null;

  messages.forEach((m, i) => {
    const d = new Date(m.timestamp * 1000);
    const dateKey = d.toDateString();
    const senderKey = (m.from_me ? "_me" : (m.push_name || contactName || "_them"));

    // Date separator on day change.
    if (dateKey !== lastDateKey) {
      grouped.push(<DateSeparator key={`d-${i}`} date={d} />);
      lastDateKey = dateKey;
      lastSender = null;
    }

    // Start of a new group when sender flips OR > 5 minutes since prev.
    const prevMsg = messages[i - 1];
    const gapMinutes = prevMsg ? Math.abs(m.timestamp - prevMsg.timestamp) / 60 : Infinity;
    const isNewGroup = senderKey !== lastSender || lastFromMe !== m.from_me || gapMinutes > 5;

    // Last in group?
    const next = messages[i + 1];
    const nextSenderKey = next ? (next.from_me ? "_me" : (next.push_name || contactName || "_them")) : null;
    const nextGap = next ? Math.abs(next.timestamp - m.timestamp) / 60 : Infinity;
    const isLastInGroup = !next || nextSenderKey !== senderKey || nextGap > 5
                           || new Date(next.timestamp * 1000).toDateString() !== dateKey;

    grouped.push(
      <Bubble
        key={`m-${m.msg_id}-${i}`}
        m={m}
        contactName={contactName}
        isGroup={isGroup}
        isFirstInGroup={isNewGroup}
        isLastInGroup={isLastInGroup}
      />,
    );

    lastSender = senderKey;
    lastFromMe = m.from_me;
  });

  return <div className="space-y-0.5">{grouped}</div>;
}

function DateSeparator({ date }: { date: Date }) {
  const now = new Date();
  const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
  let label: string;
  if (date.toDateString() === now.toDateString()) label = i18n.t("common.today");
  else if (date.toDateString() === yesterday.toDateString()) label = i18n.t("common.yesterday");
  else if (date.getFullYear() === now.getFullYear()) {
    label = date.toLocaleDateString(locale(), { weekday: "long", day: "numeric", month: "long" });
  } else {
    label = date.toLocaleDateString(locale(), { day: "numeric", month: "long", year: "numeric" });
  }
  // A centred pill, the way messengers mark a day — not a rule across
  // the screen.
  return (
    <div className="flex justify-center my-4">
      <span className="px-3 py-1 rounded-full bg-muted/80 text-2xs font-medium text-muted-foreground shadow-sm">
        {label}
      </span>
    </div>
  );
}

/**
 * Inline image renderer for WhatsApp media messages. Bytes come from
 * the bridge via /api/whatsapp/media/{msg_id} (we proxy the decrypted
 * stream). Lazy-loaded so chats with hundreds of photos don't fetch
 * everything at once; click-to-open opens the full-size image in a
 * new tab. Falls back to the legacy placeholder if the media has
 * expired on WhatsApp's servers (404) so the bubble doesn't render
 * as a broken-image icon.
 */
function ImageBubble({ msgId }: { msgId: string }) {
  const { t } = useTranslation();
  const [errored, setErrored] = useState(false);
  if (errored) {
    return (
      <span className="opacity-80 italic flex items-center gap-1.5 text-sm px-2 py-1">
        <ImageIcon className="w-3.5 h-3.5" /> {t("whatsapp.bubble.photoUnavailable")}
      </span>
    );
  }
  const url = `/api/whatsapp/media/${encodeURIComponent(msgId)}`;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="block"
      onClick={(e) => e.stopPropagation()}
    >
      <img
        src={url}
        alt={t("whatsapp.bubble.photo")}
        loading="lazy"
        className="rounded-xl max-w-full max-h-[420px] object-cover cursor-zoom-in"
        onError={() => setErrored(true)}
      />
    </a>
  );
}


function Bubble({ m, contactName, isGroup, isFirstInGroup, isLastInGroup }:
  { m: WaMessage; contactName: string; isGroup: boolean; isFirstInGroup: boolean; isLastInGroup: boolean }) {
  const { t } = useTranslation();
  const out = !!m.from_me;
  const mediaUrl = `/api/whatsapp/media/${encodeURIComponent(m.msg_id)}`;
  // Media first, the caption (m.text) under it — a photo with a caption
  // used to show the caption only.
  let media: React.ReactNode = null;
  if (m.media_kind === "image" || m.media_kind === "sticker") {
    media = <ImageBubble msgId={m.msg_id} />;
  } else if (m.media_kind === "video") {
    media = <VideoBubble url={mediaUrl} />;
  } else if (m.media_kind === "document") {
    media = (
      <a href={mediaUrl} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}
         className="flex items-center gap-2 rounded-lg bg-black/10 dark:bg-white/10 px-2.5 py-2 hover:bg-black/15 dark:hover:bg-white/15">
        <FileText className="w-5 h-5 shrink-0" />
        <span className="truncate text-sm">{m.filename || t("whatsapp.bubble.document")}</span>
      </a>
    );
  } else if (m.media_kind === "audio") {
    media = <AudioBubble url={mediaUrl} transcript={m.transcript} out={out} />;
  } else if (m.media_kind && !m.text) {
    media = <span className="opacity-70 italic">[{m.media_kind}]</span>;
  }
  const text = m.text ? <span className="whitespace-pre-wrap break-words">{m.text}</span> : null;
  const ts = formatTime(new Date(m.timestamp * 1000));

  // Group-aware corners for the tail effect.
  const cornerClass = out
    ? cn("rounded-2xl", !isLastInGroup && "rounded-br-md", !isFirstInGroup && "rounded-tr-md")
    : cn("rounded-2xl", !isLastInGroup && "rounded-bl-md", !isFirstInGroup && "rounded-tl-md");
  const mediaOnly = !!media && !text;

  return (
    <div className={cn(
      "flex",
      out ? "justify-end" : "justify-start",
      isFirstInGroup ? "mt-2.5" : "mt-0.5",
    )}>
      <div className={cn(
        "max-w-[82%] md:max-w-[70%] text-[15px] md:text-sm leading-snug shadow-sm",
        mediaOnly ? "p-1" : "px-3 pt-1.5 pb-1",
        cornerClass,
        // Flat, like a messenger: a quiet green for mine, the surface
        // colour for theirs, no borders.
        out
          ? "bg-emerald-200 text-emerald-950 dark:bg-emerald-800/80 dark:text-emerald-50"
          : "bg-muted text-foreground",
      )}>
        {/* Who wrote it: only in groups, only at the top of a run — in a
            1:1 chat the name is the chat's title. */}
        {!out && isGroup && isFirstInGroup && m.push_name && (
          <div className={cn("text-xs font-semibold text-emerald-600 dark:text-emerald-400 mb-0.5", mediaOnly && "px-2 pt-1")}>
            {m.push_name}
          </div>
        )}
        {media}
        {text && <div className={cn(media && "mt-1.5")}>{text}</div>}
        {/* Time on every bubble (phones have no hover), small, at the end. */}
        <div className={cn(
          "text-[11px] tabular-nums flex items-center gap-1 justify-end leading-none",
          mediaOnly ? "px-1.5 pt-1 pb-0.5" : "mt-0.5 -mb-0.5",
          out ? "text-emerald-900/60 dark:text-emerald-100/60" : "text-muted-foreground",
        )}>
          {m.media_paperless_id != null && <FileText className="w-3 h-3 opacity-70" aria-label={t("whatsapp.bubble.filedInDocuments")} />}
          {m.media_immich_id && <ImageIcon className="w-3 h-3 opacity-70" aria-label={t("whatsapp.bubble.inPhotos")} />}
          {ts}
          {out && <CheckCheck className="w-3.5 h-3.5 opacity-80" />}
        </div>
      </div>
    </div>
  );
}

/** A video from the chat — the browser streams it from the media route
 *  (bridge or the photo library); a faded label when it is gone. */
function VideoBubble({ url }: { url: string }) {
  const { t } = useTranslation();
  const [errored, setErrored] = useState(false);
  if (errored) {
    return <span className="opacity-80 italic text-sm px-2 py-1 inline-block">{t("whatsapp.bubble.videoUnavailable")}</span>;
  }
  return (
    <video
      src={url}
      controls
      preload="metadata"
      playsInline
      className="rounded-xl max-w-full max-h-[420px] bg-black/40"
      onError={() => setErrored(true)}
      onClick={(e) => e.stopPropagation()}
    />
  );
}

/** A voice note: a player, and under it what was said (the transcript). */
function AudioBubble({ url, transcript, out }: { url: string; transcript?: string | null; out: boolean }) {
  const { t } = useTranslation();
  const [errored, setErrored] = useState(false);
  return (
    <div className="min-w-[200px]">
      {errored ? (
        <div className="opacity-80 italic flex items-center gap-1.5 text-sm"><Mic className="w-3.5 h-3.5" /> {t("whatsapp.bubble.voiceUnavailable")}</div>
      ) : (
        <audio src={url} controls preload="none" className="w-full h-9" onError={() => setErrored(true)}
               onClick={(e) => e.stopPropagation()} />
      )}
      {transcript && (
        <div className={cn(
          "mt-1.5 pl-2 border-l-2 text-xs italic leading-relaxed",
          out ? "border-emerald-900/30 dark:border-emerald-100/30" : "border-emerald-500/40 text-foreground/80",
        )}>{transcript}</div>
      )}
    </div>
  );
}

// ───────────────────────── AI drafts panel ───────────────────────────

interface DraftState {
  key: string;
  label_en: string;
  label_de: string;
  tone: string;
}

// Per-tone tint. Subtle (10% idle, 20% active) so the buttons still
// read as quiet UI, just easier to tell apart at a glance. Tailwind v4
// JIT can't expand dynamic class names — full strings here. Unknown
// state keys fall through to the neutral default below.
const TONE_TINTS: Record<string, { idle: string; active: string }> = {
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
const TONE_TINT_DEFAULT = {
  idle:   "bg-muted/60 text-foreground/85 hover:bg-muted",
  active: "bg-primary/15 text-primary ring-1 ring-primary/40",
};

// WaSuggestions — bridge between this chat's latest inbound message
// and the shared SuggestionPanel. Polls the chat's messages list at
// the same cadence DraftPanel does (8s) and feeds the most recent
// from_me=0 message id to SuggestionPanel. When the engine fires on
// a brand-new inbound message we want the card to surface here
// without the user having to switch chats and switch back.
function WaSuggestions({ jid }: { jid: string }) {
  const msgsApi = useApi<WaMessage[]>(
    `/api/whatsapp/chats/${encodeURIComponent(jid)}/messages?limit=20`,
    [jid],
    8000,
  );
  const messages = msgsApi.data || [];
  // Most recent INBOUND (not fromMe) message — the engine only fires
  // on inbound, so that's where suggestions could live.
  const latestInbound = [...messages].reverse().find(m => !m.from_me && m.id != null);
  if (!latestInbound?.id) return null;
  return (
    <div className="px-4 pt-4">
      <SuggestionPanel sourceKind="wa" sourceId={latestInbound.id} />
    </div>
  );
}


function DraftPanel({ jid, chat, onPicked }:
  { jid: string; chat: WaChat | undefined; onPicked: (text: string) => void }) {
  const { t } = useTranslation();
  // States loaded once from /api/whatsapp/draft-states so adding /
  // removing a state is a pure backend change.
  const statesApi = useApi<DraftState[]>("/api/whatsapp/draft-states", []);
  const states = statesApi.data || [];
  const [activeState, setActiveState] = useState<string | null>(null);
  const [custom, setCustom] = useState("");
  const [generating, setGenerating] = useState(false);
  const [drafts, setDrafts] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Reset when switching chats — drafts are per-conversation by nature.
  useEffect(() => {
    setActiveState(null);
    setCustom("");
    setDrafts([]);
    setError(null);
  }, [jid]);

  async function generate(state: string) {
    setActiveState(state);
    setGenerating(true);
    setError(null);
    setDrafts([]);
    try {
      const r = await api.post<{ state: string; drafts: string[] }>(
        `/api/whatsapp/chats/${encodeURIComponent(jid)}/draft-options`,
        { state, custom: custom.trim() || null },
      );
      setDrafts(r.drafts || []);
    } catch (e: any) {
      setError(e?.message || t("whatsapp.drafts.generationFailed"));
    } finally {
      setGenerating(false);
    }
  }

  return (
    <>
      <header className="h-16 px-5 flex items-center justify-between border-b border-border">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-full bg-primary/15 flex items-center justify-center">
            <Sparkles className="w-4 h-4 text-primary" />
          </div>
          <div>
            <div className="text-sm font-semibold leading-none">{t("whatsapp.drafts.title")}</div>
            <div className="text-2xs text-muted-foreground mt-0.5">
              {generating ? t("whatsapp.drafts.thinking") : t("whatsapp.drafts.pickTone")}
            </div>
          </div>
        </div>
      </header>

      {chat?.last_message_text && (
        <div className="px-5 pt-4 pb-3 border-b border-border bg-muted/30">
          <div className="text-2xs text-muted-foreground font-semibold mb-1.5">
            {t("whatsapp.drafts.replyingTo")}
          </div>
          <div className="text-xs italic text-muted-foreground line-clamp-3 pl-2 border-l-2 border-primary/40">
            {t("whatsapp.drafts.quoted", { text: chat.last_message_text })}
          </div>
        </div>
      )}

      {/* State buttons + custom prompt */}
      <div className="px-4 pt-4 pb-3 border-b border-border space-y-3">
        <div className="grid grid-cols-2 gap-1.5">
          {states.map(s => {
            const isActive = activeState === s.key;
            const tint = TONE_TINTS[s.key] || TONE_TINT_DEFAULT;
            return (
              <button
                key={s.key}
                onClick={() => generate(s.key)}
                disabled={generating}
                title={s.tone}
                className={cn(
                  "px-2.5 py-2 rounded-lg text-xs font-medium transition",
                  "flex flex-col items-start gap-0.5 leading-tight",
                  generating && "opacity-50 cursor-not-allowed",
                  isActive ? tint.active : tint.idle,
                )}
              >
                <span>{s.label_en}</span>
                <span className="text-2xs opacity-70 font-normal">{s.label_de}</span>
              </button>
            );
          })}
        </div>
        <textarea
          value={custom}
          onChange={e => setCustom(e.target.value)}
          placeholder={t("whatsapp.drafts.customPlaceholder")}
          rows={2}
          className="w-full px-3 py-2 rounded-lg bg-muted/60 text-xs focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 resize-none leading-relaxed placeholder:text-muted-foreground/70"
        />
      </div>

      {/* Drafts area */}
      <div className="flex-1 overflow-y-auto p-4 space-y-2.5">
        {generating && (
          <div className="text-xs text-primary italic px-2 py-8 text-center flex flex-col items-center gap-2">
            <ThinkingDots />
            <span>{t("whatsapp.drafts.composing")}</span>
          </div>
        )}
        {!generating && error && (
          <div className="text-xs text-red-600 bg-red-500/10 border border-red-500/30 rounded-md p-3">
            {error}
          </div>
        )}
        {!generating && !error && drafts.length === 0 && (
          <div className="text-xs text-muted-foreground italic px-3 py-8 text-center">
            <Sparkles className="w-6 h-6 mx-auto mb-3 opacity-30" />
            {t("whatsapp.drafts.pickToneHint")}
          </div>
        )}
        {!generating && drafts.map((text, idx) => (
          <button
            key={idx}
            onClick={() => onPicked(text)}
            className="w-full text-left p-3.5 rounded-xl bg-card border border-border hover:border-primary/60 hover:shadow-md hover:-translate-y-px transition group"
          >
            <div className="flex items-center gap-2 mb-2">
              <span className="text-2xs font-bold px-2 py-0.5 rounded-full bg-primary/15 text-primary">
                {t("whatsapp.drafts.option", { n: idx + 1 })}
              </span>
            </div>
            <div className="text-xs whitespace-pre-line leading-relaxed text-foreground/90 group-hover:text-foreground">
              {text}
            </div>
          </button>
        ))}
        {!generating && drafts.length > 0 && activeState && (
          <button
            onClick={() => generate(activeState)}
            className="w-full mt-1 px-3 py-2 rounded-lg text-xs text-muted-foreground hover:text-foreground hover:bg-muted/60 transition inline-flex items-center justify-center gap-1.5"
          >
            <RefreshCw className="w-3 h-3" /> {t("whatsapp.drafts.regenerate")}
          </button>
        )}
      </div>
    </>
  );
}

function ThinkingDots() {
  return (
    <div className="flex gap-1">
      {[0, 1, 2].map(i => (
        <span
          key={i}
          className="w-1.5 h-1.5 rounded-full bg-primary"
          style={{
            animation: `wa-dot 1.2s ease-in-out infinite`,
            animationDelay: `${i * 0.15}s`,
          }}
        />
      ))}
      <style>{`
        @keyframes wa-dot {
          0%, 60%, 100% { opacity: 0.25; transform: translateY(0); }
          30% { opacity: 1; transform: translateY(-2px); }
        }
      `}</style>
    </div>
  );
}

// ───────────────────────── empty states ──────────────────────────────

function EmptyThread({ chatsCount, onOpenList }: { chatsCount: number; onOpenList?: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex-1 flex items-center justify-center p-12">
      <div className="text-center max-w-sm">
        <div className="w-20 h-20 rounded-full bg-emerald-500/10 flex items-center justify-center mx-auto mb-6">
          <MessageSquare className="w-10 h-10 text-emerald-500" />
        </div>
        <h2 className="text-lg font-semibold mb-2">{t("whatsapp.drafts.emptyTitle")}</h2>
        <p className="text-sm text-muted-foreground leading-relaxed">
          {chatsCount === 0
            ? t("whatsapp.drafts.emptyScan")
            : <>
                <span className="hidden md:inline">{t("whatsapp.drafts.emptyPickDesktop")}</span>
                <span className="md:hidden">{t("whatsapp.drafts.emptyPickPhone")}</span>
              </>}
        </p>
        {chatsCount > 0 && onOpenList && (
          // On a phone the list is a drawer — "on the left" pointed at
          // nothing (2026-09-29), so the list gets a real button.
          <button
            onClick={onOpenList}
            className="md:hidden mt-5 inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white"
          >
            <MessageSquare className="w-4 h-4" /> {t("whatsapp.drafts.showChats")}
          </button>
        )}
      </div>
    </div>
  );
}

function EmptyDrafts() {
  const { t } = useTranslation();
  return (
    <div className="flex-1 flex items-center justify-center p-6 text-center">
      <div>
        <Sparkles className="w-8 h-8 mx-auto mb-3 text-primary/40" />
        <p className="text-xs text-muted-foreground leading-relaxed">
          {t("whatsapp.drafts.emptyDraftsLine1")}<br/>{t("whatsapp.drafts.emptyDraftsLine2")}
        </p>
      </div>
    </div>
  );
}

// ───────────────────────── QR pairing modal ──────────────────────────

interface BridgeInfo {
  docker_available:  boolean;
  compose_available: boolean;
  container_state:   string;  // running | exited | restarting | absent | unknown
  container_exists:  boolean;
  container_name:    string;
}

// WhatsApp on the same phone as Yorik can't scan a QR on its own screen:
// link by phone number instead ("Link with phone number instead").
function PairByCode() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function ask() {
    setBusy(true); setErr(null);
    try {
      const r = await api.post<{ code: string }>("/api/whatsapp/pairing-code", { phone });
      setCode(r.code);
    } catch (e: any) { setErr(e?.message || t("whatsapp.pair.noCode")); }
    finally { setBusy(false); }
  }
  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className="mt-4 text-sm text-primary underline">
        {t("whatsapp.pair.linkWithCode")}
      </button>
    );
  }
  return (
    <div className="mt-5 text-left space-y-3 border-t border-border pt-4">
      {!code ? (
        <>
          <label className="block text-sm">
            <span className="font-medium">{t("whatsapp.pair.yourNumber")}</span>
            <input value={phone} onChange={e => setPhone(e.target.value)} inputMode="tel" placeholder="+49 170 1234567"
                   className="mt-1.5 w-full h-10 px-3 rounded-lg bg-background border border-border" />
          </label>
          {err && <p className="text-xs text-destructive">{err}</p>}
          <button onClick={ask} disabled={busy || phone.replace(/\D/g, "").length < 8}
                  className="w-full h-10 rounded-lg bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50 flex items-center justify-center gap-2">
            {busy && <Loader2 className="w-4 h-4 animate-spin" />} {t("whatsapp.pair.getCode")}
          </button>
        </>
      ) : (
        <>
          <div className="text-center text-3xl font-mono font-semibold tracking-[0.3em] py-2">{code}</div>
          <ol className="text-xs text-muted-foreground space-y-1 pl-4 list-decimal">
            <li><Trans i18nKey="whatsapp.pair.codeStep1" components={{ b: <b /> }} /></li>
            <li><Trans i18nKey="whatsapp.pair.codeStep2" components={{ b: <b /> }} /></li>
            <li>{t("whatsapp.pair.codeStep3")}</li>
          </ol>
        </>
      )}
    </div>
  );
}

function QrModal({
  status, onRefreshStatus,
}: {
  status: WaStatus | null | undefined;
  onRefreshStatus: () => void;
}) {
  const { t } = useTranslation();
  const [dismissed, setDismissed] = useState(false);
  const [qrUrl, setQrUrl] = useState<string | null>(null);
  const [bridgeInfo, setBridgeInfo] = useState<BridgeInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // When the bridge is unreachable, pull container info so we can show
  // the right action ("Start" vs "Restart" vs "Install Docker first").
  useEffect(() => {
    if (!status?.bridge_unreachable) return;
    let alive = true;
    api.get<BridgeInfo>("/api/whatsapp/bridge/info")
      .then(r => { if (alive) setBridgeInfo(r); })
      .catch(() => { if (alive) setBridgeInfo(null); });
    return () => { alive = false; };
  }, [status?.bridge_unreachable]);

  // Poll status every 2.5s while busy (after Start/Restart click) so
  // the modal flips to QR mode the moment the bridge comes up. Auto-
  // stops when bridge_unreachable flips false OR after 3 minutes.
  useEffect(() => {
    if (!busy) return;
    const startedAt = Date.now();
    const id = setInterval(() => {
      if (Date.now() - startedAt > 180_000) {
        setBusy(false);
        setErrorMsg(t("whatsapp.pair.timeout"));
        clearInterval(id);
        return;
      }
      onRefreshStatus();
    }, 2500);
    return () => clearInterval(id);
  }, [busy, onRefreshStatus, t]);

  // When status flips reachable, clear busy + load QR.
  useEffect(() => {
    if (busy && status && !status.bridge_unreachable) setBusy(false);
  }, [busy, status?.bridge_unreachable]);

  useEffect(() => {
    if (!status?.hasQr) return;
    let alive = true;
    api.get<{ qrPng: string }>("/api/whatsapp/qr")
      .then(r => { if (alive) setQrUrl(r.qrPng); })
      .catch(() => {});
    return () => { alive = false; };
  }, [status?.hasQr]);

  // The bridge's Baileys client takes 1-3s to spin up after the first
  // /status call eagerly creates the session dir — so the initial fetch
  // returns hasQr:false and the modal would stay on "Waiting for QR…"
  // forever. Poll /status every 3s while we're in that limbo so the QR
  // appears the moment it's ready. Stops once we have a QR, are
  // connected, the modal is dismissed, or the bridge is unreachable
  // (that case is handled by the busy/Start-bridge polling above).
  //
  // Ref-then-deref the refetcher: even though `statusApi.refetch` is
  // a useCallback, the same instability that bit `refreshAll` (see
  // chatsRefetchRef in the parent) hits us too — including
  // `onRefreshStatus` in the dep array let the effect re-fire on
  // every render, which combined with QR-expiry cycles turned the
  // 2s poll into a parallel-request flood (hit the 120/min API rate
  // limit and the whole modal stalled on 429s).
  //
  // setTimeout recursion (not setInterval) so the next poll only
  // starts AFTER the previous response settles — prevents request
  // overlap on slow networks or when the backend returns 429.
  const onRefreshStatusRef = useRef(onRefreshStatus);
  useEffect(() => { onRefreshStatusRef.current = onRefreshStatus; });
  useEffect(() => {
    if (dismissed) return;
    if (status?.hasQr || status?.connected) return;
    if (status?.bridge_unreachable) return;
    let cancelled = false;
    let timer: number | null = null;
    const tick = async () => {
      if (cancelled) return;
      try { await onRefreshStatusRef.current(); } catch { /* swallow */ }
      if (!cancelled) timer = window.setTimeout(tick, 3000);
    };
    timer = window.setTimeout(tick, 3000);
    return () => {
      cancelled = true;
      if (timer != null) clearTimeout(timer);
    };
  }, [dismissed, status?.hasQr, status?.connected, status?.bridge_unreachable]);

  async function startBridge(action: "start" | "restart") {
    setErrorMsg(null);
    setBusy(true);
    try {
      await api.post(`/api/whatsapp/bridge/${action}`, {});
    } catch (e: any) {
      setBusy(false);
      setErrorMsg(e?.message || t("whatsapp.pair.requestFailed"));
    }
  }

  if (dismissed) return null;

  // Figure out which action button to show. Container state buckets:
  //   absent | exited       → "Start bridge"           (compose up)
  //   running | restarting  → "Restart bridge"         (compose restart) — bridge is up but unreachable, repair
  //   unknown               → "Try to start" (best-effort) when docker is at least available
  const cs = bridgeInfo?.container_state || "unknown";
  const dockerOk = bridgeInfo?.docker_available && bridgeInfo?.compose_available;
  const isRunning = cs === "running" || cs === "restarting";
  const actionLabel = isRunning ? t("whatsapp.pair.restartBridge") : t("whatsapp.pair.startBridge");
  const actionVerb: "start" | "restart" = isRunning ? "restart" : "start";

  return (
    <div
      className="fixed inset-0 z-[800] flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
      onClick={() => setDismissed(true)}
    >
      <div
        className="relative bg-card border border-border rounded-2xl shadow-2xl max-w-md w-full p-8 text-center"
        onClick={e => e.stopPropagation()}
      >
        <button
          onClick={() => setDismissed(true)}
          className="absolute top-4 right-4 p-1.5 hover:bg-muted rounded-md text-muted-foreground"
        >
          <X className="w-4 h-4" />
        </button>
        <div className="w-14 h-14 rounded-full bg-emerald-500/15 flex items-center justify-center mx-auto mb-4">
          <MessageSquare className="w-7 h-7 text-emerald-500" />
        </div>
        <h2 className="text-lg font-semibold mb-1">{t("whatsapp.pair.title")}</h2>
        <p className="text-sm text-muted-foreground mb-6">
          {status?.bridge_unreachable
            ? (isRunning
                ? t("whatsapp.pair.bridgeNotResponding")
                : t("whatsapp.pair.bridgeNotRunning"))
            : t("whatsapp.pair.scanIntro")}
        </p>
        {status?.bridge_unreachable ? (
          <div className="space-y-3">
            {dockerOk === false ? (
              <div className="text-xs text-left bg-amber-500/10 border border-amber-500/30 rounded-md p-3 text-amber-700 dark:text-amber-300">
                <strong>{bridgeInfo?.docker_available ? t("whatsapp.pair.dockerComposeMissing") : t("whatsapp.pair.dockerMissing")}</strong>{" "}
                {t("whatsapp.pair.installDocker")}
              </div>
            ) : (
              <>
                <button
                  onClick={() => startBridge(actionVerb)}
                  disabled={busy}
                  className={cn(
                    "w-full px-4 py-2.5 rounded-lg font-medium text-sm inline-flex items-center justify-center gap-2 transition",
                    busy
                      ? "bg-muted text-muted-foreground cursor-not-allowed"
                      : "bg-emerald-500 hover:bg-emerald-600 text-white",
                  )}
                >
                  {busy ? (
                    <><Loader2 className="w-4 h-4 animate-spin" /> {t("whatsapp.pair.startingBridge")}</>
                  ) : (
                    <><Power className="w-4 h-4" /> {actionLabel}</>
                  )}
                </button>
                <p className="text-xs text-muted-foreground leading-relaxed">
                  {busy
                    ? t("whatsapp.pair.firstStartHint")
                    : isRunning
                      ? t("whatsapp.pair.restartHint")
                      : t("whatsapp.pair.startHint")}
                </p>
                {bridgeInfo && (
                  <p className="text-2xs text-muted-foreground/70 font-mono">
                    {t("whatsapp.pair.containerInfo", { name: bridgeInfo.container_name, state: cs })}
                  </p>
                )}
              </>
            )}
            {errorMsg && (
              <div className="text-xs text-left bg-red-500/10 border border-red-500/30 rounded-md p-3 text-red-700 dark:text-red-300">
                {errorMsg}
              </div>
            )}
          </div>
        ) : qrUrl ? (
          <>
            <div className="bg-white rounded-xl p-4 inline-block shadow-inner">
              <img src={qrUrl} alt={t("whatsapp.pair.qrAlt")} className="w-64 h-64" />
            </div>
            <ol className="text-left text-xs text-muted-foreground mt-6 space-y-1 pl-4 list-decimal">
              <li>{t("whatsapp.pair.qrStep1")}</li>
              <li><Trans i18nKey="whatsapp.pair.qrStep2" components={{ b: <b /> }} /></li>
              <li>{t("whatsapp.pair.qrStep3")}</li>
            </ol>
            <PairByCode />
          </>
        ) : (
          <div className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
            <Loader2 className="w-4 h-4 animate-spin" />
            {t("whatsapp.pair.waitingQr")}
          </div>
        )}
      </div>
    </div>
  );
}

// ───────────────────────── import + briefing dialogs ─────────────────

function ImportDialog({ onClose, onImported }:
  { onClose: () => void; onImported: () => void }) {
  const { t } = useTranslation();
  const [uploading, setUploading] = useState(false);
  const ref = useRef<HTMLInputElement>(null);

  async function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setUploading(true);
    try {
      const r = await fetch("/api/whatsapp/import", {
        method: "POST", body: form, credentials: "include",
      });
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();
      toast(t("whatsapp.import.imported", { count: data.messages_inserted, name: file.name }));
      onImported();
      onClose();
    } catch (e: any) {
      toast(t("whatsapp.import.importFailed", { error: e.message }));
    } finally { setUploading(false); }
  }

  return (
    <div className="fixed inset-0 z-[800] bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}>
      <div className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md p-7"
        onClick={e => e.stopPropagation()}>
        <h2 className="font-semibold text-base mb-2">{t("whatsapp.import.title")}</h2>
        <p className="text-sm text-muted-foreground mb-5 leading-relaxed">
          <Trans i18nKey="whatsapp.import.intro"
                 components={{ b: <b />, code: <code className="text-xs bg-muted px-1 rounded" />, code2: <code className="text-xs bg-muted px-1 rounded ml-1" /> }} />
        </p>
        <input ref={ref} type="file" accept=".txt,.zip" onChange={handleFile} className="hidden" />
        <button
          onClick={() => ref.current?.click()}
          disabled={uploading}
          className="w-full py-3 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 font-medium"
        >
          {uploading ? t("whatsapp.import.importing") : t("whatsapp.import.pickFile")}
        </button>
        <button onClick={onClose} className="w-full mt-2 py-2 text-sm text-muted-foreground hover:bg-muted rounded-md">
          {t("whatsapp.import.cancel")}
        </button>
      </div>
    </div>
  );
}

// Disconnect this WhatsApp account so a different one can be paired.
// Orchestrates: bridge sock.logout() → wipe bridge auth on disk →
// bridge starts a fresh session (which fires a QR event). The user
// can then scan the new QR with a different WhatsApp account.
//
// Local history (wa_messages / wa_chats / wa_drafts) is KEPT by
// default — disconnect is most commonly used to swap accounts and
// users almost always want the old conversation history retained
// for archival. A checkbox makes the destructive "wipe history"
// path explicit and opt-in.
function DisconnectDialog({
  currentName, onClose, onDisconnected,
}: {
  currentName: string;
  onClose: () => void;
  onDisconnected: () => Promise<void> | void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [wipeHistory, setWipeHistory] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function confirm() {
    if (busy) return;
    setBusy(true); setErr(null);
    try {
      const r = await api.post<{
        ok: boolean;
        bridge?: { logged_out?: boolean; auth_wiped?: boolean; restarted?: boolean };
        bridge_error?: string | null;
        history_wiped?: { messages: number; chats: number; drafts: number } | null;
      }>("/api/whatsapp/disconnect", { wipe_history: wipeHistory });
      if (!r.ok && r.bridge_error) {
        // Surface the bridge error explicitly — partial states (e.g.
        // auth was wiped but logout failed) leave you in a workable
        // state but worth telling the user.
        setErr(t("whatsapp.disconnect.partialFailed", { error: r.bridge_error }));
        return;
      }
      await onDisconnected();
    } catch (e: any) {
      setErr(e?.message || t("whatsapp.disconnect.failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[800] bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={busy ? undefined : onClose}>
      <div className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md p-6"
        onClick={e => e.stopPropagation()}>
        <h2 className="font-semibold text-base mb-1">{t("whatsapp.disconnect.title")}</h2>
        <p className="text-sm text-muted-foreground mb-4 leading-relaxed">
          <Trans i18nKey="whatsapp.disconnect.intro" values={{ name: currentName }}
                 components={{ b: <span className="font-medium text-foreground" /> }} />
        </p>

        <label className="flex items-start gap-2 mb-4 cursor-pointer select-none p-2.5 rounded-md border border-border hover:bg-muted/40">
          <input
            type="checkbox"
            checked={wipeHistory}
            onChange={e => setWipeHistory(e.target.checked)}
            disabled={busy}
            className="mt-0.5 accent-primary"
          />
          <span className="text-xs leading-snug">
            <span className="text-foreground font-medium">{t("whatsapp.disconnect.wipeLabel")}</span>
            <span className="block text-muted-foreground mt-0.5">
              {t("whatsapp.disconnect.wipeHint")}
            </span>
          </span>
        </label>

        {err && (
          <div className="mb-3 text-xs text-destructive bg-destructive/10 border border-destructive/30 rounded-md px-3 py-2">
            {err}
          </div>
        )}

        <div className="flex gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="flex-1 py-2 text-sm text-muted-foreground hover:bg-muted rounded-md disabled:opacity-50"
          >
            {t("whatsapp.disconnect.cancel")}
          </button>
          <button
            onClick={confirm}
            disabled={busy}
            className="flex-1 py-2 text-sm rounded-md bg-destructive text-destructive-foreground hover:opacity-90 disabled:opacity-50 inline-flex items-center justify-center gap-1.5"
          >
            {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            {busy ? t("whatsapp.disconnect.disconnecting") : t("whatsapp.disconnect.disconnect")}
          </button>
        </div>
      </div>
    </div>
  );
}


function BriefingDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const briefingApi = useApi<{ summary: string; stats: any; generated_at: string }>(
    "/api/briefings/morning-overview/run?window_hours=24", []
  );

  return (
    <div className="fixed inset-0 z-[800] bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}>
      <div className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-xl max-h-[80vh] overflow-y-auto p-7"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-5">
          <h2 className="font-semibold flex items-center gap-2">
            <Newspaper className="w-5 h-5 text-primary" /> {t("whatsapp.briefing.title")}
          </h2>
          <a href="/r/briefing" className="text-xs text-primary hover:underline">{t("whatsapp.briefing.openFull")}</a>
        </div>
        {briefingApi.loading && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="w-4 h-4 animate-spin" /> {t("whatsapp.briefing.generating")}
          </div>
        )}
        {briefingApi.error && <div className="text-sm text-destructive">{briefingApi.error}</div>}
        {briefingApi.data && (
          <pre className="text-sm whitespace-pre-wrap font-sans leading-relaxed">{briefingApi.data.summary}</pre>
        )}
      </div>
    </div>
  );
}

// ───────────────────────── helpers ──────────────────────────────────

function SilkBtn({ icon: Icon, title, onClick, loading = false }:
  { icon: any; title: string; onClick: () => void; loading?: boolean }) {
  return (
    <button onClick={onClick} title={title} aria-label={title}
      className="w-8 h-8 rounded-md hover:bg-muted/60 text-muted-foreground flex items-center justify-center transition">
      <Icon className={cn("w-4 h-4", loading && "animate-spin")} />
    </button>
  );
}

function ConnectionDot({ status }: { status: WaStatus | null | undefined }) {
  const color = status?.connected ? "bg-emerald-500"
    : status?.bridge_unreachable ? "bg-yellow-500" : "bg-muted-foreground/40";
  return (
    <span className="relative inline-flex shrink-0">
      <span className={cn("w-2 h-2 rounded-full", color)} />
      {status?.connected && (
        <span className={cn("absolute inset-0 w-2 h-2 rounded-full animate-ping opacity-60", color)} />
      )}
    </span>
  );
}

// JIDs that are never personal contacts and have no profile picture.
// Skipping the avatar request for these saves a roundtrip per chat-list
// render (the backend short-circuits them too, this just avoids the
// trip in the first place).
function isPseudoJid(jid: string): boolean {
  return jid === "status@broadcast"
      || jid.endsWith("@newsletter")
      || jid.endsWith("@lid");
}

function WaAvatar({ jid, name, size = "md" }:
  { jid: string; name: string; size?: "md" | "lg" }) {
  const initials = (name || "?")
    .split(/[\s+@()-]+/).filter(Boolean).slice(0, 2)
    .map(s => s[0]).join("").toUpperCase();
  const [errored, setErrored] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const dim = size === "lg" ? "w-11 h-11 text-sm" : "w-11 h-11 text-xs";
  const hue = Math.abs(hash(name)) % 360;
  const fallbackStyle = {
    background: `linear-gradient(135deg, hsl(${hue} 55% 50% / 0.30), hsl(${(hue+30)%360} 55% 45% / 0.30))`,
    color: `hsl(${hue} 60% 55%)`,
  };

  if (errored || !jid || isPseudoJid(jid)) {
    return (
      <div className={cn("rounded-full flex items-center justify-center font-semibold shrink-0", dim)}
        style={fallbackStyle}>
        {initials}
      </div>
    );
  }
  return (
    <div className={cn("relative shrink-0", dim)}>
      {/* Fallback placeholder visible until the image loads; prevents
          flash-of-broken-img when avatar URL is slow or returns 404. */}
      {!loaded && (
        <div className={cn("absolute inset-0 rounded-full flex items-center justify-center font-semibold", dim)}
          style={fallbackStyle}>
          {initials}
        </div>
      )}
      <img
        src={`/api/whatsapp/avatar/${encodeURIComponent(jid)}`}
        alt={name}
        onError={() => setErrored(true)}
        onLoad={() => setLoaded(true)}
        className={cn("rounded-full object-cover", dim,
                       loaded ? "opacity-100" : "opacity-0")}
      />
    </div>
  );
}

function formatChatListTime(d: Date): string {
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
  }
  const y = new Date(now); y.setDate(y.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return i18n.t("common.yesterday");
  if (now.getTime() - d.getTime() < 7 * 24 * 3600 * 1000) {
    return d.toLocaleDateString(locale(), { weekday: "short" });
  }
  return d.toLocaleDateString(locale(), { day: "2-digit", month: "short" });
}

function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h) + s.charCodeAt(i);
  return h;
}

// ───────────────────────── WebSocket hook ───────────────────────────

function useWaSocket(onEvent: (evt: { type: string; payload?: any }) => void) {
  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let attempts = 0;
    let alive = true;

    function connect() {
      if (!alive) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      try {
        ws = new WebSocket(`${proto}//${location.host}/api/whatsapp/ws`);
      } catch {
        return scheduleReconnect();
      }
      ws.addEventListener("open", () => { attempts = 0; });
      ws.addEventListener("message", (e) => {
        try { onEvent(JSON.parse(e.data)); } catch {}
      });
      ws.addEventListener("close", () => {
        ws = null;
        if (alive) scheduleReconnect();
      });
    }
    function scheduleReconnect() {
      if (reconnectTimer) return;
      const delay = Math.min(500 * Math.pow(1.7, attempts), 10_000);
      attempts += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    }

    connect();
    return () => {
      alive = false;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws) try { ws.close(1000, "unmount"); } catch {}
    };
  }, [onEvent]);
}

// ─── Contacts hub inbound banner ─────────────────────────────────────
// Looks up the chat's WhatsApp number in /api/contacts. Three states:
//   - "active" contact found → nothing rendered (banner is noise).
//   - "pending" contact found → amber banner with Confirm / Spam buttons.
//   - "spam" contact found → red banner with Restore button.
//   - 404 (unknown sender) → blue banner with "Save to contacts" button.
// Banner re-fetches on jid change.

interface BannerContact {
  id: number;
  status: "active" | "pending" | "spam" | "archived";
  display_name: string;
}

function ContactBanner({ jid, fallbackName }: { jid: string; fallbackName: string }) {
  const { t } = useTranslation();
  // We pass the FULL jid (including @s.whatsapp.net / @lid) so the
  // backend can match contact_channels exactly. The earlier "digits only"
  // version was what made @lid pseudo-jids collide with each other.
  const [contact, setContact] = useState<BannerContact | null>(null);
  const [unknown, setUnknown] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setDismissed(false);
    try {
      const c = await api.get<BannerContact>(
        `/api/contacts/by-channel/whatsapp/${encodeURIComponent(jid)}`,
      );
      setContact(c);
      setUnknown(false);
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 404) {
        setContact(null);
        setUnknown(true);
      } else {
        // Silent — banner just hides on transient errors.
        setContact(null);
        setUnknown(false);
      }
    } finally { setLoading(false); }
  }, [jid]);

  useEffect(() => { refresh(); }, [refresh]);

  async function promote() {
    if (!contact) return;
    setBusy(true);
    try {
      await api.post(`/api/contacts/${contact.id}/promote`);
      await refresh();
    } finally { setBusy(false); }
  }
  async function markSpam() {
    if (!contact) return;
    if (!confirm(t("whatsapp.contact.confirmSpam", { name: contact.display_name }))) return;
    setBusy(true);
    try {
      await api.post(`/api/contacts/${contact.id}/spam`);
      await refresh();
    } finally { setBusy(false); }
  }
  async function restoreFromSpam() {
    if (!contact) return;
    setBusy(true);
    try {
      await api.patch(`/api/contacts/${contact.id}`, { status: "active" });
      await refresh();
    } finally { setBusy(false); }
  }
  async function saveUnknown() {
    setBusy(true);
    try {
      const c = await api.post<{ id: number }>("/api/contacts", {
        display_name: fallbackName.trim() || jid.split("@")[0],
        kind: "person",
        status: "active",
        source: "wa_manual",
      });
      try {
        await api.post(`/api/contacts/${c.id}/channels`, {
          kind: "whatsapp", value: jid,
        });
      } catch { /* dup is fine — find_by_channel will pick it up next refresh */ }
      await refresh();
    } finally { setBusy(false); }
  }

  if (loading || dismissed) return null;

  // Active contact found → silent. The presence of the name in the
  // header is already enough information.
  if (contact && contact.status === "active") return null;

  if (contact && contact.status === "pending") {
    return (
      <div className="px-6 py-2 bg-amber-500/10 border-b border-amber-500/30 flex items-center gap-2 text-xs">
        <UsersRound className="w-3.5 h-3.5 text-amber-600" />
        <span className="text-foreground">
          <Trans i18nKey="whatsapp.contact.pending" values={{ name: contact.display_name }} components={{ b: <span className="font-medium" /> }} />
        </span>
        <button
          onClick={promote}
          disabled={busy}
          className="ml-auto px-2.5 py-1 rounded-md bg-emerald-500 text-white text-xs font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
        >
          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
          {t("whatsapp.contact.confirm")}
        </button>
        <button
          onClick={markSpam}
          disabled={busy}
          className="px-2.5 py-1 rounded-md border border-border bg-card text-xs hover:bg-red-500/10 hover:border-red-500/30 hover:text-red-500 disabled:opacity-50 flex items-center gap-1"
        >
          <ShieldAlert className="w-3 h-3" /> {t("whatsapp.contact.spam")}
        </button>
        <button
          onClick={() => setDismissed(true)}
          className="p-1 text-muted-foreground hover:text-foreground"
          title={t("whatsapp.contact.dismissSession")}
        >
          <X className="w-3 h-3" />
        </button>
      </div>
    );
  }

  if (contact && contact.status === "spam") {
    return (
      <div className="px-6 py-2 bg-red-500/10 border-b border-red-500/30 flex items-center gap-2 text-xs">
        <ShieldAlert className="w-3.5 h-3.5 text-red-500" />
        <span className="text-foreground">
          <Trans i18nKey="whatsapp.contact.onSpamList" values={{ name: contact.display_name }} components={{ b: <span className="font-medium" /> }} />
        </span>
        <button
          onClick={restoreFromSpam}
          disabled={busy}
          className="ml-auto px-2.5 py-1 rounded-md border border-border bg-card text-xs hover:bg-muted disabled:opacity-50"
        >
          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : t("whatsapp.contact.restore")}
        </button>
        <button
          onClick={() => setDismissed(true)}
          className="p-1 text-muted-foreground hover:text-foreground"
          title={t("whatsapp.contact.dismissSession")}
        >
          <X className="w-3 h-3" />
        </button>
      </div>
    );
  }

  if (unknown) {
    return (
      <div className="px-6 py-2 bg-blue-500/10 border-b border-blue-500/30 flex items-center gap-2 text-xs">
        <UsersRound className="w-3.5 h-3.5 text-blue-500" />
        <span className="text-foreground">
          {t("whatsapp.contact.unknownNumber")}
        </span>
        <button
          onClick={saveUnknown}
          disabled={busy}
          className="ml-auto px-2.5 py-1 rounded-md bg-blue-500 text-white text-xs font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
        >
          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
          {t("whatsapp.contact.saveToContacts")}
        </button>
        <button
          onClick={() => setDismissed(true)}
          className="p-1 text-muted-foreground hover:text-foreground"
          title={t("whatsapp.contact.dismissSession")}
        >
          <X className="w-3 h-3" />
        </button>
      </div>
    );
  }

  return null;
}

