/**
 * "Back up this phone's photos": the Immich app, the address it needs,
 * the login, and the one switch to flip. Everything copyable, nothing to
 * look up. Backend: GET /api/setup/photos-phone.
 */
import { useEffect, useState } from "react";
import { Check, Copy, ExternalLink, Loader2, X } from "lucide-react";
import { api } from "@/lib/api";
import { detectPlatform } from "@/components/PhoneSetup";

interface Info { server_url: string | null; email: string | null; password: string | null; uses_yorik_password: boolean }

function Copyable({ label, value, secret }: { label: string; value: string; secret?: boolean }) {
  const [done, setDone] = useState(false);
  const [show, setShow] = useState(!secret);
  return (
    <div>
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="flex items-center gap-2">
        <code className="flex-1 min-w-0 text-sm bg-muted/50 border border-border rounded-lg px-3 py-2 break-all">
          {show ? value : "••••••••••"}
        </code>
        {secret && <button onClick={() => setShow(s => !s)} className="text-xs text-muted-foreground underline">{show ? "Hide" : "Show"}</button>}
        <button onClick={async () => { try { await navigator.clipboard.writeText(value); setDone(true); setTimeout(() => setDone(false), 1500); } catch { setShow(true); } }}
                className="w-9 h-9 rounded-lg border border-border flex items-center justify-center shrink-0" aria-label={`Copy ${label}`}>
          {done ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
        </button>
      </div>
    </div>
  );
}

export function PhotosPhoneSetup({ onClose }: { onClose: () => void }) {
  const [info, setInfo] = useState<Info | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.get<Info>("/api/setup/photos-phone").then(setInfo).catch(e => setErr(e?.message || "Couldn't load.")); }, []);
  const p = detectPlatform();
  const store = p === "ios" ? "https://apps.apple.com/app/immich/id1613945652"
    : p === "android" ? "https://play.google.com/store/apps/details?id=app.alextran.immich"
    : "https://immich.app/docs/features/mobile-app";

  return (
    <div className="fixed inset-0 z-[850] flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <div className="w-full max-w-md max-h-[90vh] overflow-y-auto bg-background border border-border rounded-2xl shadow-2xl p-6 relative space-y-5" onClick={e => e.stopPropagation()}>
        <button onClick={onClose} className="absolute top-3 right-3 w-9 h-9 rounded-md hover:bg-muted flex items-center justify-center" aria-label="Close"><X className="w-4 h-4" /></button>
        <div>
          <h2 className="text-xl font-semibold">Back up this phone's photos</h2>
          <p className="text-sm text-muted-foreground mt-1">New pictures go to your family's Yorik by themselves, not to a cloud.</p>
        </div>
        {err && <p className="text-sm text-destructive">{err}</p>}
        {!info && !err && <Loader2 className="w-5 h-5 animate-spin text-muted-foreground" />}
        {info && (
          <ol className="space-y-5">
            <li className="space-y-2">
              <div className="font-medium">1. Get the Immich app</div>
              <a href={store} target="_blank" rel="noopener noreferrer"
                 className="flex items-center justify-center gap-1.5 h-10 rounded-xl border border-border bg-card font-medium text-sm">
                {p === "ios" ? "Immich in the App Store" : p === "android" ? "Immich on Google Play" : "Immich for your phone"} <ExternalLink className="w-3.5 h-3.5" />
              </a>
            </li>
            <li className="space-y-2">
              <div className="font-medium">2. Tell it where your photos live</div>
              {info.server_url
                ? <Copyable label="Server address (paste it where the app asks)" value={info.server_url} />
                : <p className="text-sm text-muted-foreground">An admin needs to publish the photo library in Tailscale first (Help → Photos).</p>}
            </li>
            <li className="space-y-2">
              <div className="font-medium">3. Sign in</div>
              <p className="text-xs text-muted-foreground">If the app shows <b>Sign in with Yorik</b>, tap that. Otherwise:</p>
              {info.email && <Copyable label="Email" value={info.email} />}
              {info.password
                ? <Copyable label="Password" value={info.password} secret />
                : <p className="text-sm text-muted-foreground">Password: the same one you use for Yorik.</p>}
            </li>
            <li className="space-y-1">
              <div className="font-medium">4. Switch on backup</div>
              <p className="text-sm text-muted-foreground">In Immich, tap the cloud symbol at the top, choose <b>Recents</b> (or all albums) and switch <b>Backup</b> on. Allow access to all photos when the phone asks.</p>
            </li>
          </ol>
        )}
        <button onClick={onClose} className="w-full h-11 rounded-xl bg-primary text-primary-foreground font-medium">Done</button>
      </div>
    </div>
  );
}
