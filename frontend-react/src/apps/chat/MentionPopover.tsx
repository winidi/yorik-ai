/**
 * MentionPopover — autocomplete that floats above the chat composer.
 *
 * Triggered by the chat composer when the user types `@` or `/` at a
 * word boundary. Two modes:
 *
 *   @prefix  → entity lookup (contacts / events / docs) via
 *              /api/chat/mentions. The user picks one; the popover
 *              emits the chosen item back so the composer can splice
 *              `@Hans Müller [contact:5]` into the textarea. The
 *              bracket tag travels with the message so the LLM can
 *              resolve to a contact_id without re-asking.
 *
 *   /prefix  → slash commands from a curated catalog. Each command
 *              expands to a templated user message ("@today" =>
 *              "Was steht heute an?"). Yorik's backend doesn't need
 *              to know they exist — the catalog is purely client-side
 *              syntactic sugar that produces normal LLM input.
 *
 * The popover does NO state of its own beyond the highlighted index;
 * the parent (ChatApp) owns the prefix + open/close and feeds them in.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Loader2, AtSign, Slash, UsersRound, CalendarDays, FileText,
  CornerDownLeft,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import type { MentionResults, MentionItem } from "./types";


export type MentionMode = "@" | "/";

/** A picked item the parent splices into the textarea. */
export interface MentionPick {
  /** "@<label>" or "/<command name>" for display, plus a structured tag
   *  the LLM can resolve unambiguously. */
  displayText: string;
  /** Optional ASCII bracket tag (e.g. "[contact:5]") appended after
   *  the display label so the agent loop can map back to the real
   *  database row. Slash commands omit this. */
  tag?: string;
  /** When a slash command expands to a full templated message — used
   *  for commands like /today that REPLACE the composer text. */
  fullMessage?: string;
}


/* ─── Slash-command catalog ────────────────────────────────────── */
//
// Each command produces one of two outcomes:
//   1. `fullMessage` set → composer text is REPLACED with that prompt
//      (and usually auto-sent). Use for one-shot queries that don't
//      need any user typing after the trigger.
//   2. `template` set → composer text becomes "<template> " with the
//      caret placed after, so the user types the rest. Use for
//      action commands ("/event Zahnarzt morgen 14 Uhr").
//
// Adding a command here is purely client-side — the LLM sees the
// expanded text like any other user input.

interface SlashCommand {
  name: string;       // shown after the slash, e.g. "event"
  label: string;      // human-friendly title
  description: string;
  /** Templated text replacing the composer (will auto-send). */
  fullMessage?: string;
  /** Text prefix to seed the composer with; user types the rest. */
  template?: string;
}

const SLASH_COMMANDS: SlashCommand[] = [
  { name: "today",  label: "What's on today",
    description: "Show today's calendar + open tasks",
    fullMessage: "Was steht heute an? Bitte zeig auch offene Aufgaben." },
  { name: "week",  label: "This week",
    description: "Week overview",
    fullMessage: "Was steht diese Woche an?" },
  { name: "tasks", label: "Open tasks",
    description: "List currently open tasks",
    fullMessage: "Welche Aufgaben sind noch offen?" },
  { name: "event", label: "New calendar event",
    description: "Add an event — type the rest after the prefix",
    template: "Trag einen Termin ein: " },
  { name: "task",  label: "New task",
    description: "Add a task — type the rest after the prefix",
    template: "Neue Aufgabe: " },
  { name: "letter", label: "Write a letter",
    description: "Draft a letter — type the recipient + topic after",
    template: "Schreib einen Brief an " },
  { name: "find", label: "Find a document",
    description: "Search your filing cabinet",
    template: "Finde das Dokument zu " },
  { name: "contact", label: "Find a contact",
    description: "Search contacts by name",
    template: "Wer ist " },
];


interface Props {
  mode: MentionMode;
  prefix: string;
  /** Pick handler — parent splices the result into the composer. */
  onPick: (pick: MentionPick) => void;
  /** Called when the user types Escape — parent closes the popover. */
  onCancel: () => void;
}


export function MentionPopover({ mode, prefix, onPick, onCancel }: Props) {
  if (mode === "/") {
    return <SlashPanel prefix={prefix} onPick={onPick} onCancel={onCancel} />;
  }
  return <AtPanel prefix={prefix} onPick={onPick} onCancel={onCancel} />;
}


/* ─── Slash command popover ─────────────────────────────────────── */

function SlashPanel({ prefix, onPick, onCancel }: {
  prefix: string;
  onPick: (pick: MentionPick) => void;
  onCancel: () => void;
}) {
  const matches = useMemo(() => {
    const q = prefix.toLowerCase();
    if (!q) return SLASH_COMMANDS;
    return SLASH_COMMANDS.filter(
      c => c.name.startsWith(q) || c.label.toLowerCase().includes(q),
    );
  }, [prefix]);
  const [highlight, setHighlight] = useState(0);
  useEffect(() => { setHighlight(0); }, [prefix]);

  useKeyHandler(matches.length, highlight, setHighlight, () => {
    const pick = matches[highlight];
    if (!pick) return;
    onPick({
      displayText: `/${pick.name}`,
      fullMessage: pick.fullMessage,
      tag: pick.template,
    });
  }, onCancel);

  return (
    <Shell title="Slash commands" icon={<Slash className="w-3 h-3" />}>
      {matches.length === 0 ? (
        <div className="px-3 py-4 text-[11px] text-muted-foreground text-center italic">
          No commands match.
        </div>
      ) : matches.map((c, i) => (
        <button
          key={c.name}
          type="button"
          onMouseEnter={() => setHighlight(i)}
          onClick={() => onPick({
            displayText: `/${c.name}`,
            fullMessage: c.fullMessage,
            tag: c.template,
          })}
          className={cn(
            "w-full text-left px-3 py-2.5 md:py-1.5 text-sm md:text-xs flex items-start gap-2 transition active:bg-violet-500/20",
            i === highlight ? "bg-violet-500/10" : "hover:bg-muted/50",
          )}
        >
          <div className="flex-1 min-w-0">
            <div className="font-medium text-foreground flex items-center gap-1.5">
              <code className="text-[10px] bg-muted px-1 rounded">/{c.name}</code>
              <span>{c.label}</span>
            </div>
            <div className="text-[10px] text-muted-foreground mt-0.5 truncate">
              {c.description}
            </div>
          </div>
          {i === highlight && (
            <CornerDownLeft className="w-2.5 h-2.5 text-muted-foreground mt-1.5 shrink-0" />
          )}
        </button>
      ))}
    </Shell>
  );
}


/* ─── @-mention popover ─────────────────────────────────────────── */

function AtPanel({ prefix, onPick, onCancel }: {
  prefix: string;
  onPick: (pick: MentionPick) => void;
  onCancel: () => void;
}) {
  const [results, setResults] = useState<MentionResults>(
    { contact: [], event: [], doc: [] },
  );
  const [loading, setLoading] = useState(false);
  const reqIdRef = useRef(0);

  // Debounced fetch. The /api/chat/mentions endpoint is cheap (one
  // indexed SQL per type) so a 120ms debounce is enough to coalesce
  // rapid typing without making the popover feel laggy.
  useEffect(() => {
    const myId = ++reqIdRef.current;
    setLoading(true);
    const handle = window.setTimeout(async () => {
      try {
        const r = await api.get<MentionResults>(
          `/api/chat/mentions?prefix=${encodeURIComponent(prefix)}&types=contact,event,doc&limit=6`,
        );
        if (myId === reqIdRef.current) {
          setResults(r);
        }
      } catch {
        if (myId === reqIdRef.current) {
          setResults({ contact: [], event: [], doc: [] });
        }
      } finally {
        if (myId === reqIdRef.current) setLoading(false);
      }
    }, 120);
    return () => window.clearTimeout(handle);
  }, [prefix]);

  // Flatten into a single keyboard-navigable list, grouped visually.
  const flat = useMemo(() => {
    const items: Array<{ kind: "contact" | "event" | "doc"; item: MentionItem }> = [];
    for (const k of ["contact", "event", "doc"] as const) {
      for (const it of results[k]) items.push({ kind: k, item: it });
    }
    return items;
  }, [results]);
  const [highlight, setHighlight] = useState(0);
  useEffect(() => { setHighlight(0); }, [prefix, flat.length]);

  useKeyHandler(flat.length, highlight, setHighlight, () => {
    const sel = flat[highlight];
    if (!sel) return;
    onPick(toPick(sel.kind, sel.item));
  }, onCancel);

  return (
    <Shell title={`Mention${prefix ? ` "${prefix}"` : ""}`}
           icon={<AtSign className="w-3 h-3" />}>
      {loading && flat.length === 0 && (
        <div className="px-3 py-3 text-[11px] text-muted-foreground inline-flex items-center gap-1.5">
          <Loader2 className="w-3 h-3 animate-spin" /> searching…
        </div>
      )}
      {!loading && flat.length === 0 && (
        <div className="px-3 py-4 text-[11px] text-muted-foreground text-center italic">
          No matches{prefix && ` for "${prefix}"`}.
        </div>
      )}
      {(["contact", "event", "doc"] as const).map(kind => {
        const items = results[kind];
        if (!items.length) return null;
        return (
          <div key={kind}>
            <div className="px-3 pt-1.5 pb-1 text-[9px] uppercase tracking-wider text-muted-foreground font-semibold flex items-center gap-1">
              {kind === "contact" && <UsersRound className="w-2.5 h-2.5" />}
              {kind === "event"   && <CalendarDays className="w-2.5 h-2.5" />}
              {kind === "doc"     && <FileText className="w-2.5 h-2.5" />}
              {kind === "contact" ? "Contacts" : kind === "event" ? "Events" : "Documents"}
            </div>
            {items.map(it => {
              const i = flat.findIndex(f => f.kind === kind && f.item.id === it.id);
              return (
                <button
                  key={`${kind}-${it.id}`}
                  type="button"
                  onMouseEnter={() => setHighlight(i)}
                  onClick={() => onPick(toPick(kind, it))}
                  className={cn(
                    "w-full text-left px-3 py-2.5 md:py-1.5 text-sm md:text-xs flex items-center gap-2 transition active:bg-violet-500/20",
                    i === highlight ? "bg-violet-500/10" : "hover:bg-muted/50",
                  )}
                >
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-foreground truncate">{it.label}</div>
                    {it.sub && (
                      <div className="text-[10px] text-muted-foreground truncate">{it.sub}</div>
                    )}
                  </div>
                  {i === highlight && (
                    <CornerDownLeft className="w-2.5 h-2.5 text-muted-foreground shrink-0" />
                  )}
                </button>
              );
            })}
          </div>
        );
      })}
    </Shell>
  );
}


/* ─── helpers ──────────────────────────────────────────────────── */

function toPick(kind: "contact" | "event" | "doc", item: MentionItem): MentionPick {
  return {
    displayText: `@${item.label}`,
    tag: ` [${kind}:${item.id}]`,
  };
}

function Shell({ title, icon, children }: {
  title: string; icon: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="absolute bottom-full left-0 right-0 mb-2 max-w-md mx-auto z-30
                    rounded-xl border border-border bg-popover shadow-2xl overflow-hidden">
      <div className="px-3 py-1.5 border-b border-border bg-muted/30 flex items-center gap-1.5">
        {icon}
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
          {title}
        </span>
        {/* Keyboard hint only on desktop — soft keyboards don't have
            arrow keys / Enter / Esc as discoverable affordances. */}
        <span className="ml-auto text-[10px] text-muted-foreground/70 hidden md:inline">
          ↑↓ · Enter · Esc
        </span>
      </div>
      {/* max-h tightened on mobile so the popover never extends past
          the top of the visible area when the soft keyboard is open
          (keyboard ~270px + composer ~80px + dock ~70px leaves ~270px
          of usable area on a 667px iPhone SE). */}
      <div className="max-h-52 md:max-h-72 overflow-y-auto py-1">
        {children}
      </div>
    </div>
  );
}

/** Wire up arrow keys + Enter + Escape on the document while the popover
 *  is open. Captures so it runs before the textarea's own onKeyDown. */
function useKeyHandler(
  total: number,
  highlight: number,
  setHighlight: (n: number) => void,
  onEnter: () => void,
  onEscape: () => void,
) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        e.stopPropagation();
        setHighlight((highlight + 1) % Math.max(1, total));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        e.stopPropagation();
        setHighlight((highlight - 1 + Math.max(1, total)) % Math.max(1, total));
      } else if (e.key === "Enter") {
        if (total === 0) return;
        e.preventDefault();
        e.stopPropagation();
        onEnter();
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onEscape();
      } else if (e.key === "Tab") {
        if (total === 0) return;
        e.preventDefault();
        e.stopPropagation();
        onEnter();
      }
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [total, highlight, setHighlight, onEnter, onEscape]);
}
