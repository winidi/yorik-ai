/**
 * PushCard — Settings → You. Turns this device's notifications on, lists
 * the devices that opted in, and sets the two daily nudges (morning plan,
 * evening review). Backend: backend/push.py.
 */
import { useCallback, useEffect, useState } from "react";
import { BellRing, Loader2, Smartphone, Trash2 } from "lucide-react";
import { api } from "@/lib/api";

type Status = {
  devices: { id: number; endpoint: string; user_agent: string | null; created_at: string; last_ok_at: string | null }[];
  nudge_morning: string | null;
  nudge_evening: string | null;
  timezone: string;
};

function b64ToUint8(b64: string): Uint8Array {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

export function PushCard({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [thisEndpoint, setThisEndpoint] = useState<string | null>(null);
  const [morning, setMorning] = useState("");
  const [evening, setEvening] = useState("");
  const supported = typeof window !== "undefined" && "PushManager" in window && "serviceWorker" in navigator;

  const load = useCallback(async () => {
    try {
      const s = await api.get<Status>("/api/push/status");
      setStatus(s);
      setMorning(s.nudge_morning || "");
      setEvening(s.nudge_evening || "");
    } catch (e: any) {
      toast(`Couldn't load push settings: ${e.message}`, "error");
    }
    try {
      const reg = await navigator.serviceWorker?.ready;
      const sub = await reg?.pushManager.getSubscription();
      setThisEndpoint(sub?.endpoint || null);
    } catch { /* no sw */ }
  }, [toast]);
  useEffect(() => { load(); }, [load]);

  async function enable() {
    setBusy(true);
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { toast("Notifications were not allowed by the browser.", "error"); return; }
      const reg = await navigator.serviceWorker.ready;
      const { key } = await api.get<{ key: string }>("/api/push/vapid-public-key");
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToUint8(key) });
      await api.post("/api/push/subscribe", { subscription: sub.toJSON() });
      toast("This device will get Yorik's notifications.", "success");
      await load();
    } catch (e: any) {
      toast(`Couldn't enable: ${e?.message || e}`, "error");
    } finally {
      setBusy(false);
    }
  }

  async function disableThis() {
    setBusy(true);
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await api.post("/api/push/unsubscribe", { endpoint: sub.endpoint });
        await sub.unsubscribe();
      }
      await load();
    } catch (e: any) {
      toast(`Couldn't disable: ${e?.message || e}`, "error");
    } finally {
      setBusy(false);
    }
  }

  async function removeDevice(endpoint: string) {
    setBusy(true);
    try {
      await api.post("/api/push/unsubscribe", { endpoint });
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function sendTest() {
    setBusy(true);
    try {
      const r = await api.post<{ ok: boolean; delivered: number }>("/api/push/test", {});
      toast(r.ok ? `Sent to ${r.delivered} device${r.delivered === 1 ? "" : "s"}.` : "No device accepted the push.", r.ok ? "success" : "error");
    } catch (e: any) {
      toast(`Test failed: ${e?.message || e}`, "error");
    } finally {
      setBusy(false);
    }
  }

  async function saveNudges() {
    setBusy(true);
    try {
      await api.patch("/api/push/nudges", { nudge_morning: morning || null, nudge_evening: evening || null });
      toast("Saved.", "success");
      await load();
    } catch (e: any) {
      toast(`Couldn't save: ${e?.message || e}`, "error");
    } finally {
      setBusy(false);
    }
  }

  const thisDeviceOn = !!thisEndpoint && !!status?.devices.some(d => d.endpoint === thisEndpoint);

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h3 className="text-xs uppercase tracking-wider font-semibold text-muted-foreground mb-3">Notifications on your phone</h3>
      <div className="mb-3 flex items-start gap-2">
        <BellRing className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">Yorik taps you on the shoulder</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Everything that lands in your bell also reaches this device while the app is closed:
            a finished task from your agent, a deletion waiting for you, the morning plan. Works in
            the installed app on Android and, since iOS 16.4, on iPhone when Yorik sits on the home screen.
          </p>
        </div>
      </div>

      {!supported && (
        <p className="text-[11px] text-amber-600 mb-3">This browser does not support Web Push. Open Yorik in Chrome or Safari and add it to the home screen.</p>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-4">
        {thisDeviceOn ? (
          <button onClick={disableThis} disabled={busy} className="text-xs h-8 px-3 rounded-md border border-border bg-card hover:bg-muted inline-flex items-center gap-1.5 disabled:opacity-60">
            Turn off on this device
          </button>
        ) : (
          <button onClick={enable} disabled={busy || !supported} className="text-xs h-8 px-3 rounded-md bg-primary text-primary-foreground inline-flex items-center gap-1.5 disabled:opacity-60">
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <BellRing className="w-3.5 h-3.5" />}
            Turn on for this device
          </button>
        )}
        {(status?.devices.length || 0) > 0 && (
          <button onClick={sendTest} disabled={busy} className="text-xs h-8 px-3 rounded-md border border-border bg-card hover:bg-muted disabled:opacity-60">
            Send a test
          </button>
        )}
      </div>

      {(status?.devices.length || 0) > 0 && (
        <ul className="divide-y divide-border mb-4">
          {status!.devices.map(d => (
            <li key={d.id} className="py-2 flex items-center gap-3">
              <Smartphone className="w-4 h-4 text-muted-foreground shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-sm truncate">{(d.user_agent || "Device").replace(/^Mozilla\/5\.0 \(([^)]+)\).*$/, "$1")}{d.endpoint === thisEndpoint ? " · this device" : ""}</div>
                <div className="text-[11px] text-muted-foreground">added {d.created_at.slice(0, 10)}{d.last_ok_at ? ` · last push ${d.last_ok_at.slice(0, 16)}` : ""}</div>
              </div>
              <button onClick={() => removeDevice(d.endpoint)} disabled={busy} className="text-xs inline-flex items-center gap-1 text-muted-foreground hover:text-red-600 disabled:opacity-50">
                <Trash2 className="w-3.5 h-3.5" /> Remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="border-t border-border pt-3">
        <div className="text-xs font-medium mb-1">Daily nudges</div>
        <p className="text-[11px] text-muted-foreground mb-2">
          Morning: "Shall I plan your day?" opens the chat with the planner. Evening: a short review of what you got done. Times in {status?.timezone || "your timezone"}; leave empty to switch off.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <label className="block">
            <div className="text-[11px] text-muted-foreground mb-1">Morning</div>
            <input type="time" value={morning} onChange={e => setMorning(e.target.value)} className="h-9 px-3 rounded-md bg-muted/60 text-sm" />
          </label>
          <label className="block">
            <div className="text-[11px] text-muted-foreground mb-1">Evening</div>
            <input type="time" value={evening} onChange={e => setEvening(e.target.value)} className="h-9 px-3 rounded-md bg-muted/60 text-sm" />
          </label>
          <button onClick={saveNudges} disabled={busy} className="h-9 px-3 rounded-md bg-primary text-primary-foreground text-sm disabled:opacity-60">Save</button>
        </div>
      </div>
    </div>
  );
}
