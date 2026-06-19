/**
 * Settings → Devices.
 *
 * Lists the current user's active sessions (browser cookies, mobile
 * PWAs, kiosks). For each session:
 *   - shows what the user-agent looks like + last seen
 *   - lets the user revoke any session (logs that device out)
 *   - lets an ADMIN promote the session to kiosk mode + pick the
 *     Immich album the slideshow reads from
 *
 * Non-admins still see the kiosk indicator + can revoke; the
 * "Make this a kiosk" toggle is hidden because the backend would
 * 403 anyway. PIN is set on the Profile tab, not here.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Loader2, Trash2, MonitorSmartphone, Tv, Tablet, Play, ShieldCheck, Mic, MicOff,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";

interface DeviceRow {
  id:              string;
  created_at:      string;
  last_seen_at:    string;
  expires_at:      string;
  user_agent:      string;
  ip_seen:         string;
  is_kiosk:        boolean;
  is_current:      boolean;
  kiosk_album_id:  string | null;
  device_label:    string;
  trusted_until:   string | null;
  show_today:      boolean;
  block_phrases:   string[];
}

interface Album {
  id:          string;
  name:        string;
  asset_count: number;
  shared:      boolean;
}

interface Props {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}

export function DevicesTab({ toast }: Props) {
  const auth = useAuth();
  const navigate = useNavigate();
  const isAdmin = auth.user.role === "admin" || auth.user.role === "platform_admin";

  const [devices, setDevices]   = useState<DeviceRow[] | null>(null);
  const [albums,  setAlbums]    = useState<Album[] | null>(null);
  const [busy,    setBusy]      = useState<Record<string, boolean>>({});
  const [editingFor, setEditingFor] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await api.get<DeviceRow[]>("/api/devices");
      setDevices(r);
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      toast(`Couldn't load devices: ${msg}`, "error");
    }
  }, [toast]);

  useEffect(() => { refresh(); }, [refresh]);

  // Admins fetch the album catalogue once on mount so the toggle is
  // ready when they open it. Non-admins skip this — the endpoint
  // returns 403 for them anyway.
  useEffect(() => {
    if (!isAdmin) return;
    (async () => {
      try {
        const r = await api.get<Album[]>("/api/devices/albums");
        setAlbums(r);
      } catch (err: any) {
        // Non-fatal — the dropdown shows "couldn't load albums" inline.
        console.warn("devices: album list failed", err);
      }
    })();
  }, [isAdmin]);

  // Trusted-device list (UUIDs the admin has marked as kiosk-permanent
  // via the YorikWall wrapper). Refreshed alongside /api/devices.
  // Stored as a Map device_id → row so the hotword toggle can read
  // the current kiosk_hotword_enabled state without a re-fetch.
  const [trustedDevices, setTrustedDevices] = useState<
    Map<string, { kiosk_hotword_enabled: boolean }>
  >(new Map());
  const refreshTrusted = useCallback(async () => {
    try {
      const r = await api.get<
        { device_id: string; kiosk_hotword_enabled: number | boolean }[]
      >("/api/devices/trust");
      const m = new Map<string, { kiosk_hotword_enabled: boolean }>();
      for (const t of r || []) {
        m.set(t.device_id, { kiosk_hotword_enabled: !!t.kiosk_hotword_enabled });
      }
      setTrustedDevices(m);
    } catch {
      // Non-admins get []; old backends 404. Silent — the badge just
      // won't appear.
    }
  }, []);
  useEffect(() => { if (isAdmin) refreshTrusted(); }, [isAdmin, refreshTrusted]);

  // The wrapper's UUID for "this device". Memoised since the bridge
  // call is synchronous and the value never changes for the life of
  // the page.
  const wallDeviceId = useMemo(() => {
    try {
      const n = (window as any).YorikNative;
      if (n && typeof n.getDeviceId === "function") {
        return String(n.getDeviceId() || "");
      }
    } catch {}
    return "";
  }, []);

  async function forgetTrust() {
    if (!wallDeviceId) return;
    if (!confirm("Forget this device as a kiosk? Future logins will need to be set up again.")) return;
    try {
      await api.delete(`/api/devices/trust/${wallDeviceId}`);
      toast("This device is no longer remembered as a kiosk", "success");
      await refreshTrusted();
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      toast(`Forget failed: ${msg}`, "error");
    }
  }

  async function toggleHotword() {
    if (!wallDeviceId) return;
    const current = trustedDevices.get(wallDeviceId);
    const next = !(current?.kiosk_hotword_enabled);
    try {
      await api.patch(`/api/devices/trust/${wallDeviceId}/hotword`, { enabled: next });
      // Flip the native service immediately. The wrapper reads the
      // flag at cold start too, so even without this bridge call the
      // next launch would honour it — but flipping in-page means the
      // mic notification appears / disappears the moment the user
      // ticks the box.
      try {
        const n = (window as any).YorikNative;
        if (n && typeof n.setHotwordEnabled === "function") {
          n.setHotwordEnabled(next);
        }
      } catch {}
      toast(next ? "Wake word “Hey Yorik” enabled" : "Wake word disabled", "success");
      await refreshTrusted();
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      toast(`Wake-word toggle failed: ${msg}`, "error");
    }
  }

  async function revoke(sid: string) {
    if (!confirm("Revoke this session? The device will be logged out immediately.")) return;
    setBusy(b => ({ ...b, [sid]: true }));
    try {
      await api.delete(`/api/devices/${sid}`);
      toast("Session revoked", "success");
      await refresh();
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      toast(`Revoke failed: ${msg}`, "error");
    } finally {
      setBusy(b => ({ ...b, [sid]: false }));
    }
  }

  async function applyKiosk(
    sid: string,
    is_kiosk: boolean,
    kiosk_album_id: string | null,
    device_label: string,
    show_today: boolean,
    block_phrases: string[],
  ) {
    setBusy(b => ({ ...b, [sid]: true }));
    try {
      await api.post(`/api/devices/${sid}/kiosk`, {
        is_kiosk, kiosk_album_id, device_label, show_today,
        block_phrases,
      });
      // Auto-trust: if we're inside the YorikWall wrapper AND this
      // edit enabled kiosk mode, persist the policy under the
      // wrapper's device UUID so future logins inherit it without
      // any setup. The user wants this implicit — they're already
      // configuring kiosk inside the wrapper, of course they want
      // it permanent. Disabling kiosk doesn't auto-revoke trust;
      // forgetTrust() is the explicit knob for that.
      if (is_kiosk && wallDeviceId) {
        try {
          await api.post("/api/devices/trust");
          await refreshTrusted();
        } catch {
          // Non-fatal — the kiosk config saved. The next login just
          // won't auto-apply if the trust call failed.
        }
      }
      toast(is_kiosk ? "Kiosk mode enabled" : "Kiosk mode disabled", "success");
      setEditingFor(null);
      await refresh();
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      toast(`Kiosk toggle failed: ${msg}`, "error");
    } finally {
      setBusy(b => ({ ...b, [sid]: false }));
    }
  }

  const albumById = useMemo(
    () => Object.fromEntries((albums || []).map(a => [a.id, a])),
    [albums],
  );

  if (devices === null) {
    return (
      <div className="py-12 text-center text-muted-foreground">
        <Loader2 className="w-5 h-5 animate-spin inline" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold mb-1">Devices</h2>
        <p className="text-sm text-muted-foreground">
          Browsers, tablets, and phones currently signed in as you.
          Revoke a session to sign that device out instantly.
        </p>
      </div>

      <div className="space-y-3">
        {devices.length === 0 && (
          <div className="text-sm text-muted-foreground italic">
            No sessions found — that's unusual; you're reading this from one
            right now. Try refreshing.
          </div>
        )}
        {devices.map(d => (
          <DeviceCard
            key={d.id}
            device={d}
            albumName={d.kiosk_album_id ? albumById[d.kiosk_album_id]?.name : undefined}
            editing={editingFor === d.id}
            isAdmin={isAdmin}
            busy={!!busy[d.id]}
            albums={albums || []}
            onRevoke={() => revoke(d.id)}
            onStartEdit={() => setEditingFor(d.id)}
            onCancelEdit={() => setEditingFor(null)}
            onApplyKiosk={applyKiosk}
            onOpenKiosk={() => navigate("/ambient")}
            onForgetTrust={forgetTrust}
            onToggleHotword={toggleHotword}
            trusted={d.is_current ? trustedDevices.has(wallDeviceId) : false}
            hotwordOn={d.is_current ? !!trustedDevices.get(wallDeviceId)?.kiosk_hotword_enabled : false}
            isWrapper={d.is_current && !!wallDeviceId}
          />
        ))}
      </div>
    </div>
  );
}

function DeviceCard({
  device, albumName, editing, isAdmin, busy, albums, trusted,
  hotwordOn, isWrapper,
  onRevoke, onStartEdit, onCancelEdit, onApplyKiosk, onOpenKiosk, onForgetTrust,
  onToggleHotword,
}: {
  trusted?:        boolean;
  hotwordOn?:      boolean;
  isWrapper?:      boolean;
  onForgetTrust:   () => void;
  onToggleHotword: () => void;
  device:          DeviceRow;
  albumName?:      string;
  editing:         boolean;
  isAdmin:         boolean;
  busy:            boolean;
  albums:          Album[];
  onRevoke:        () => void;
  onStartEdit:     () => void;
  onCancelEdit:    () => void;
  onApplyKiosk:    (sid: string, is_kiosk: boolean, album_id: string | null, label: string, show_today: boolean, block_phrases: string[]) => void;
  onOpenKiosk:     () => void;
}) {
  const [draftLabel, setDraftLabel] = useState(device.device_label || "");
  const [draftAlbum, setDraftAlbum] = useState(device.kiosk_album_id || "");
  const [draftShowToday, setDraftShowToday] = useState(device.show_today);
  // Stored as a single textarea string ("medicine, prescription, receipt")
  // and split to a list[str] on submit. Comma is the canonical separator;
  // we also accept newlines so the admin can drop one phrase per line if
  // that reads cleaner for them.
  const [draftPhrases, setDraftPhrases] = useState((device.block_phrases || []).join(", "));

  const ua = device.user_agent || "(unknown user-agent)";
  const lastSeen = formatRelative(device.last_seen_at);
  const Icon = device.is_kiosk ? Tv : guessIcon(ua);

  return (
    <div className="rounded-xl border border-border bg-card p-4 space-y-3">
      <div className="flex items-start gap-3">
        <div className={cn(
          "w-10 h-10 shrink-0 rounded-lg flex items-center justify-center",
          device.is_kiosk ? "bg-blue-500/15 text-blue-500" : "bg-muted text-muted-foreground",
        )}>
          <Icon className="w-5 h-5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium truncate">
              {device.device_label || trimUA(ua)}
            </span>
            {device.is_kiosk && (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-blue-500/15 text-blue-500 font-medium">
                Kiosk
              </span>
            )}
            {device.is_kiosk && device.show_today && (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-500 font-medium">
                Today
              </span>
            )}
            {device.trusted_until && (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-500 font-medium">
                Trusted
              </span>
            )}
          </div>
          <div className="text-xs text-muted-foreground mt-1 truncate">
            {ua}
          </div>
          <div className="text-xs text-muted-foreground mt-0.5">
            Last seen {lastSeen} from {device.ip_seen || "unknown IP"}
          </div>
          {device.is_kiosk && (
            <div className="text-xs text-blue-500 mt-1">
              Album: {albumName || (device.kiosk_album_id ? device.kiosk_album_id.slice(0, 8) + "…" : "not configured")}
            </div>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={onRevoke}
            disabled={busy}
            className="h-8 px-2 inline-flex items-center gap-1 rounded-md text-xs text-rose-500 hover:bg-rose-500/10 disabled:opacity-50"
            title="Sign out this device"
          >
            <Trash2 className="w-3.5 h-3.5" /> Revoke
          </button>
        </div>
      </div>

      {/* Kiosk toggle — admin only */}
      {isAdmin && !editing && (
        <div className="flex items-center gap-2">
          <button
            onClick={onStartEdit}
            disabled={busy}
            className="h-7 px-2.5 inline-flex items-center gap-1 rounded-md border border-border text-xs hover:bg-muted disabled:opacity-50"
          >
            {device.is_kiosk ? "Edit kiosk settings" : "Make this a kiosk"}
          </button>
          {/* Open the kiosk wall — the auto-redirect on cold load only
              fires once per app boot, so this gives a no-fail manual
              way back in. Only meaningful on a session that's actually
              flagged as a kiosk; for sessions on other devices it would
              open a non-kiosk /ambient that bounces straight home. */}
          {device.is_kiosk && device.is_current && (
            <button
              onClick={onOpenKiosk}
              className="h-7 px-2.5 inline-flex items-center gap-1 rounded-md text-xs bg-blue-500 text-white hover:bg-blue-600"
            >
              <Play className="w-3 h-3" /> Open kiosk wall
            </button>
          )}
          {/* Trusted-device chip + revoke. Shown when this exact device
              has a persistent kiosk policy stored under its UUID, so
              the household admin can see at a glance "yes, the next
              login here auto-kiosks." Inverse case (wrapper but not
              trusted) is silent — the implicit auto-trust on save
              will create it on the next kiosk-enable. */}
          {device.is_current && trusted && (
            <span
              className="h-7 px-2.5 inline-flex items-center gap-1 rounded-md text-xs bg-emerald-500/15 text-emerald-600 border border-emerald-500/30"
              title="Future logins from this physical tablet will auto-apply this kiosk config."
            >
              <ShieldCheck className="w-3 h-3" /> Trusted
              <button
                onClick={onForgetTrust}
                className="ml-1 text-emerald-700/70 hover:text-emerald-700 underline-offset-2 hover:underline"
                title="Forget this device — future logins will need fresh setup"
              >
                forget
              </button>
            </span>
          )}
          {/* Wake-word toggle — only meaningful inside the YorikWall
              wrapper (regular browsers can't run the foreground mic
              service). Shown when the device is trusted so the policy
              has somewhere to live; the native service reads
              kiosk_hotword_enabled on cold start. */}
          {device.is_current && trusted && isWrapper && (
            <button
              onClick={onToggleHotword}
              className={cn(
                "h-7 px-2.5 inline-flex items-center gap-1 rounded-md text-xs border transition",
                hotwordOn
                  ? "bg-blue-500/15 text-blue-600 border-blue-500/30 hover:bg-blue-500/25"
                  : "bg-muted text-muted-foreground border-border hover:bg-muted/70",
              )}
              title={
                hotwordOn
                  ? "Tap to stop listening for 'Hey Yorik'"
                  : "Tap to start listening for 'Hey Yorik' on this tablet"
              }
            >
              {hotwordOn
                ? <><Mic className="w-3 h-3" /> "Hey Yorik" on</>
                : <><MicOff className="w-3 h-3" /> "Hey Yorik" off</>}
            </button>
          )}
        </div>
      )}

      {isAdmin && editing && (
        <div className="border-t border-border pt-3 space-y-3">
          <div>
            <label className="block text-xs font-medium mb-1">Device label</label>
            <input
              type="text"
              value={draftLabel}
              onChange={e => setDraftLabel(e.target.value)}
              placeholder="e.g. Living Room Tablet"
              className="w-full h-9 px-2 text-sm bg-background border border-border rounded-md focus:outline-none focus:ring-1 focus:ring-ring/40"
            />
          </div>
          <div>
            <label className="block text-xs font-medium mb-1">Photo album</label>
            <select
              value={draftAlbum}
              onChange={e => setDraftAlbum(e.target.value)}
              className="w-full h-9 px-2 text-sm bg-background border border-border rounded-md focus:outline-none focus:ring-1 focus:ring-ring/40"
            >
              <option value="">— Pick an Immich album —</option>
              {albums.map(a => (
                <option key={a.id} value={a.id}>
                  {a.name} ({a.asset_count} photo{a.asset_count === 1 ? "" : "s"})
                </option>
              ))}
            </select>
            {albums.length === 0 && (
              <div className="text-[11px] text-muted-foreground mt-1">
                No albums visible. Configure Immich under Settings → Connectors first.
              </div>
            )}
          </div>
          {/* "Show today's photos" toggle — adds dynamic family-wall behavior
              on top of the curated album. Today's photos surface first
              (newest-first), then the album cycles after them. */}
          <label className="flex items-start gap-2.5 p-2.5 rounded-md border border-border bg-background/40 cursor-pointer hover:bg-background/70 transition">
            <input
              type="checkbox"
              checked={draftShowToday}
              onChange={e => setDraftShowToday(e.target.checked)}
              className="mt-0.5"
            />
            <div className="flex-1 min-w-0">
              <div className="text-xs font-medium">Also show today's photos</div>
              <div className="text-[11px] text-muted-foreground mt-0.5">
                Surface photos taken today from your Immich library first,
                then cycle the curated album. The family-wall mode — walk
                past, see what happened today.
              </div>
            </div>
          </label>
          {/* Content filter — CLIP-based blocklist. Each phrase runs through
              Immich's smart search; the union of matches gets hidden from
              the slideshow. Empty = no filter. */}
          <div>
            <label className="block text-xs font-medium mb-1">Hide photos matching</label>
            <textarea
              value={draftPhrases}
              onChange={e => setDraftPhrases(e.target.value)}
              rows={2}
              placeholder="medicine, prescription bottle, receipt, screenshot"
              className="w-full px-2 py-1.5 text-sm bg-background border border-border rounded-md focus:outline-none focus:ring-1 focus:ring-ring/40 resize-y"
            />
            <div className="text-[11px] text-muted-foreground mt-1">
              Comma-separated phrases. Uses Immich's CLIP smart search to
              recognise photo content — so "medicine" hides pill bottles
              and prescription packaging even if they aren't tagged.
              False positives happen with similar visuals (a spice rack
              may look like medicine to CLIP). Refresh takes ~10 minutes.
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onApplyKiosk(
                device.id, true, draftAlbum || null, draftLabel.trim(),
                draftShowToday, parsePhrases(draftPhrases),
              )}
              disabled={busy || (!draftAlbum && !draftShowToday)}
              className="h-8 px-3 rounded-md bg-blue-500 text-white text-xs font-medium hover:opacity-90 disabled:opacity-40 inline-flex items-center gap-1.5"
            >
              {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
              {device.is_kiosk ? "Save kiosk settings" : "Enable kiosk mode"}
            </button>
            {device.is_kiosk && (
              <button
                onClick={() => onApplyKiosk(device.id, false, null, "", false, [])}
                disabled={busy}
                className="h-8 px-3 rounded-md border border-rose-500/40 text-rose-500 text-xs hover:bg-rose-500/10 disabled:opacity-40"
              >
                Disable kiosk
              </button>
            )}
            <button
              onClick={onCancelEdit}
              disabled={busy}
              className="h-8 px-2.5 rounded-md text-xs text-muted-foreground hover:bg-muted disabled:opacity-40"
            >
              Cancel
            </button>
          </div>
          <div className="text-[11px] text-muted-foreground">
            Enabling kiosk mode extends this session's trust to 365 days and
            unlocks PIN-based user switching from the wall. Disable any time
            from here.
          </div>
        </div>
      )}
    </div>
  );
}

function parsePhrases(raw: string): string[] {
  // Split on commas and newlines (admin's choice) so either separator
  // works. Trim each piece; drop empties so a trailing comma doesn't
  // produce a "" phrase that smart-search would interpret as "all".
  return raw
    .split(/[,\n]/)
    .map(s => s.trim())
    .filter(Boolean);
}

function guessIcon(ua: string) {
  const u = ua.toLowerCase();
  if (u.includes("ipad") || u.includes("tablet")) return Tablet;
  if (u.includes("iphone") || u.includes("android")) return MonitorSmartphone;
  return MonitorSmartphone;
}

function trimUA(ua: string): string {
  // YorikWall wrapper tags the User-Agent as
  //   "<chrome stuff> YorikWall/<version> (<manufacturer> <model>)"
  // when so tagged, show the friendly device label instead of the
  // raw browser string. Matches "(...)" right after "YorikWall/X.Y.Z".
  const wallMatch = ua.match(/YorikWall\/[0-9.]+\s*\(([^)]+)\)/);
  if (wallMatch && wallMatch[1].trim()) return wallMatch[1].trim();
  // Compact UA snippet — first quoted browser/version-ish token.
  const m = ua.match(/([A-Za-z][A-Za-z+ -]+)\/[0-9.]+/g);
  return m ? m.slice(0, 2).join(" ") : ua.slice(0, 50);
}

function formatRelative(iso: string): string {
  try {
    const t = new Date(iso).getTime();
    const diff = (Date.now() - t) / 1000;
    if (diff < 60)     return "just now";
    if (diff < 3600)   return `${Math.round(diff / 60)} min ago`;
    if (diff < 86400)  return `${Math.round(diff / 3600)} h ago`;
    return new Date(iso).toLocaleDateString();
  } catch {
    return iso;
  }
}
