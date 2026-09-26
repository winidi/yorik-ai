/**
 * "Invite someone": name + grown-up or child → a QR code the person
 * scans with their phone camera (and a link to send instead).
 * Backend: POST /api/invites (backend/member_invites.py).
 */
import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { AlertCircle, Check, Copy, Loader2, Share2, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Created {
  id: number; name: string; role: string; expires_at: string;
  join_url: string | null; qr_url: string | null; join_page: boolean;
  tailscale: string; tailscale_invite_url: string | null; problem?: string; home_only?: boolean;
}

export function InviteDialog({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [kid, setKid] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [made, setMade] = useState<Created | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!made?.qr_url) return;
    QRCode.toDataURL(made.qr_url, { margin: 1, width: 560, errorCorrectionLevel: "M" })
      .then(setQr).catch(() => setQr(null));
  }, [made?.qr_url]);

  async function create() {
    setBusy(true); setErr(null);
    try {
      setMade(await api.post<Created>("/api/invites", { name: name.trim(), role: kid ? "restricted" : "member" }));
    } catch (e: any) {
      setErr(e?.message || "The invite couldn't be made.");
    } finally { setBusy(false); }
  }

  async function share() {
    if (!made?.qr_url) return;
    const text = `Join our Yorik: ${made.qr_url}`;
    if ((navigator as any).share) {
      try { await (navigator as any).share({ title: "Yorik", text, url: made.qr_url }); return; } catch { /* fall back to copy */ }
    }
    try { await navigator.clipboard.writeText(made.qr_url); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch { /* ignore */ }
  }

  const first = (made?.name || name).trim().split(" ")[0];

  return (
    <div className="fixed inset-0 z-[850] flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <div className="w-full max-w-md bg-background border border-border rounded-2xl shadow-2xl p-6 relative" onClick={e => e.stopPropagation()}>
        <button onClick={onClose} className="absolute top-3 right-3 w-9 h-9 rounded-md hover:bg-muted flex items-center justify-center" aria-label="Close">
          <X className="w-4 h-4" />
        </button>

        {!made ? (
          <div className="space-y-5">
            <div>
              <h2 className="text-xl font-semibold">Invite someone</h2>
              <p className="text-sm text-muted-foreground mt-1">They scan a code with their phone camera and choose a 4-digit PIN. No password, no email needed.</p>
            </div>
            <label className="block">
              <span className="text-sm font-medium">Name</span>
              <input value={name} onChange={e => setName(e.target.value)} maxLength={60} placeholder="e.g. Mama"
                     className="mt-1.5 w-full h-11 px-3 rounded-xl bg-card border border-border focus:outline-none focus:ring-2 focus:ring-ring/40" />
            </label>
            <div className="grid grid-cols-2 gap-2">
              {[{ v: false, t: "Grown-up", d: "All apps" }, { v: true, t: "Child", d: "Board, tasks, calendar, photos, chat" }].map(o => (
                <button key={String(o.v)} onClick={() => setKid(o.v)}
                        className={cn("text-left p-3 rounded-xl border transition", kid === o.v ? "border-primary/60 bg-primary/10" : "border-border hover:bg-muted/50")}>
                  <div className="text-sm font-medium">{o.t}</div>
                  <div className="text-xs text-muted-foreground mt-0.5">{o.d}</div>
                </button>
              ))}
            </div>
            {err && <p className="text-sm text-destructive">{err}</p>}
            <button onClick={create} disabled={!name.trim() || busy}
                    className="w-full h-11 rounded-xl bg-primary text-primary-foreground font-medium disabled:opacity-50 flex items-center justify-center gap-2">
              {busy && <Loader2 className="w-4 h-4 animate-spin" />} Make the code
            </button>
          </div>
        ) : (
          <div className="space-y-4 text-center">
            <h2 className="text-xl font-semibold">{first} scans this with the phone camera</h2>
            {made.problem ? (
              <div className="flex items-start gap-2 text-left p-3 rounded-xl bg-amber-500/10 border border-amber-500/20 text-sm">
                <AlertCircle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" /> {made.problem}
              </div>
            ) : (
              <>
                <div className="bg-white rounded-2xl p-3 inline-block">
                  {qr ? <img src={qr} alt={`Invite code for ${first}`} className="w-64 h-64" /> : <div className="w-64 h-64 flex items-center justify-center"><Loader2 className="w-5 h-5 animate-spin text-zinc-400" /></div>}
                </div>
                <p className="text-sm text-muted-foreground">
                  Works once, for 24 hours.{" "}
                  {made.home_only
                    ? "This code works on your home Wi-Fi. For Yorik on the go, connect Tailscale under Settings → System → Phones."
                    : made.join_page
                    ? (made.tailscale_invite_url
                        ? "The page walks them through Tailscale first."
                        : "The page explains Tailscale; you'll still need to share Yorik with them in Tailscale.")
                    : "Their phone needs Tailscale on, or to be on your home Wi-Fi."}
                </p>
                <button onClick={share} className="w-full h-11 rounded-xl border border-border bg-card font-medium flex items-center justify-center gap-2">
                  {copied ? <><Check className="w-4 h-4" /> Link copied</> : (navigator as any).share ? <><Share2 className="w-4 h-4" /> Send the link instead</> : <><Copy className="w-4 h-4" /> Copy the link instead</>}
                </button>
              </>
            )}
            <button onClick={onClose} className="w-full h-11 rounded-xl bg-primary text-primary-foreground font-medium">Done</button>
          </div>
        )}
      </div>
    </div>
  );
}
