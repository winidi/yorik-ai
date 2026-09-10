/**
 * AgentCard — Settings → You. "My agent": the Hermes (or any
 * OpenAI-shaped chat endpoint) Yorik hands your questions to. Per
 * person, so what you ask never lands on somebody else's machine.
 * Backend: GET/PATCH /api/profile/agent (session only; key never
 * returned).
 */
import { useCallback, useEffect, useState } from "react";
import { Bot, Loader2 } from "lucide-react";
import { api } from "@/lib/api";

type Status = { url: string; name: string; key_set: boolean; shared_available: boolean };

export function AgentCard({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = await api.get<Status>("/api/profile/agent");
      setStatus(s);
      setUrl(s.url);
      setName(s.name);
    } catch (e: any) {
      toast(`Couldn't load agent settings: ${e.message}`, "error");
    }
  }, [toast]);
  useEffect(() => { load(); }, [load]);

  async function save(clear = false) {
    setBusy(true);
    try {
      const body: Record<string, string> = clear ? { url: "" } : { url: url.trim(), name: name.trim() };
      if (!clear && key.trim()) body.key = key.trim();
      const s = await api.patch<Status>("/api/profile/agent", body);
      setStatus(s);
      setUrl(s.url);
      setName(s.name);
      setKey("");
      toast(clear ? "Agent removed" : "Agent saved", "success");
    } catch (e: any) {
      toast(`Couldn't save: ${e?.message || e}`, "error");
    } finally {
      setBusy(false);
    }
  }

  if (!status) return null;
  const configured = !!status.url;

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h3 className="text-xs uppercase tracking-wider font-semibold text-muted-foreground mb-3">My agent</h3>
      <div className="mb-3 flex items-start gap-2">
        <Bot className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">
            {configured ? `Questions go to ${status.name || "your agent"}` : "No agent set up for you"}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            When Yorik cannot answer something itself (web research, files on your PC, coding), it hands the question
            to your own agent, for example Hermes on your computer. Only yours: nobody's questions reach another
            member's machine.{status.shared_available && !configured ? " Until you set one, the household's shared agent answers for you." : ""}
          </p>
        </div>
      </div>
      <div className="grid gap-2 sm:grid-cols-[1fr_10rem]">
        <input value={url} onChange={e => setUrl(e.target.value)} placeholder="http://my-pc:8642/v1  (Hermes API server, or over Tailscale)"
               className="rounded-lg border border-border bg-transparent px-3 py-2 text-sm" />
        <input value={name} onChange={e => setName(e.target.value)} placeholder="Name, e.g. Hermes"
               className="rounded-lg border border-border bg-transparent px-3 py-2 text-sm" />
      </div>
      <input value={key} onChange={e => setKey(e.target.value)} type="password" autoComplete="off"
             placeholder={status.key_set ? "API key (stored; type to replace)" : "API key (API_SERVER_KEY from the agent's .env)"}
             className="mt-2 w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm" />
      <div className="mt-3 flex items-center gap-2">
        <button onClick={() => save(false)} disabled={busy || !url.trim()}
                className="flex items-center gap-2 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-semibold disabled:opacity-50">
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : null} Save
        </button>
        {configured && (
          <button onClick={() => save(true)} disabled={busy} className="rounded-lg px-3 py-1.5 text-sm hover:bg-muted">Remove</button>
        )}
      </div>
    </div>
  );
}
