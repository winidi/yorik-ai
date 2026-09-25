/**
 * Home's family row: one face per person on the family board, with what
 * today holds for them ("2 events · 1 to-do", or "Free today"). Same
 * feed as the board (GET /api/ambient/board), so only people who chose
 * to appear there show up. Clicking a face opens the board. Renders
 * nothing when the feed is empty or refused, so Home never shows a
 * broken strip.
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { PersonAvatar } from "@/components/PersonAvatar";

interface Person { id: string; name: string; first_name: string; color: string; avatar_url: string | null }
interface Ev { owner_id: string | null; shared: boolean; starts_at: string; all_day: boolean }
interface Task { due_date: string | null; done: boolean; assignee_ids: string[] }
interface Feed { today: string; people: Person[]; events: Ev[]; tasks: Task[] }

function plural(n: number, one: string, many: string) { return `${n} ${n === 1 ? one : many}`; }

export function FamilyRow() {
  const navigate = useNavigate();
  const [feed, setFeed] = useState<Feed | null>(null);

  useEffect(() => {
    api.get<Feed>("/api/ambient/board?days=1").then(setFeed).catch(() => setFeed(null));
  }, []);

  if (!feed || !feed.people?.length) return null;

  const today = feed.today;
  const rows = feed.people.map(p => {
    const events = feed.events.filter(e =>
      (e.owner_id === p.id || e.shared) && (e.starts_at || "").slice(0, 10) === today).length;
    const todos = feed.tasks.filter(t =>
      !t.done && t.assignee_ids?.includes(p.id) && (!t.due_date || t.due_date <= today)).length;
    const parts = [
      events ? plural(events, "event", "events") : "",
      todos ? plural(todos, "to-do", "to-dos") : "",
    ].filter(Boolean);
    return { p, line: parts.length ? parts.join(", ") : "Free today" };
  });

  return (
    <section className="mb-8">
      <div className="flex gap-5 overflow-x-auto pb-1">
        {rows.map(({ p, line }) => (
          <button
            key={p.id}
            onClick={() => navigate("/board")}
            className="flex flex-col items-center gap-1.5 min-w-[72px] group"
            title={`${p.first_name || p.name}: ${line}. Open the family board`}
          >
            <PersonAvatar
              name={p.name} color={p.color} avatarUrl={p.avatar_url} size={48}
              className="ring-2 ring-transparent group-hover:ring-foreground/20 transition"
            />
            <span className="text-sm font-medium leading-none">{p.first_name || p.name}</span>
            <span className="text-xs text-muted-foreground leading-none whitespace-nowrap">{line}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
