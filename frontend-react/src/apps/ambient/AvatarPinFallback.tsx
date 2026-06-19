/**
 * Avatar + PIN modal on the kiosk wall.
 *
 * Shown when the voice stream emits `identify_needed` — backend couldn't
 * match the speaker against an enrolled voice profile, so it refuses to
 * attribute the turn to anyone (kiosk session is bound to the device
 * owner, NOT to whoever's standing in front of the wall).
 *
 * Two-step:
 *   1. Avatar grid: every user that has a PIN set is pickable. No PIN
 *      = can't be the actor on a kiosk; they need to set one in
 *      Settings → Profile → Kiosk PIN first.
 *   2. PinPad: 4 digits. Submit hits POST /api/auth/pin-switch which
 *      issues an ephemeral 5-min session cookie for the picked user.
 *      After that, the tablet runs as them and the picker dismisses.
 *
 * If the user wants to retry their original ask, the modal exposes a
 * "Retry as <name>" button that POSTs to /api/ask (text path, no voice
 * round-trip) with the transcript the backend captured.
 */
import { useState } from "react";
import { Loader2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { api, ApiError } from "@/lib/api";
import { PinPad } from "@/components/PinPad";

export interface PickableUser {
  id:         number;
  name:       string;
  first_name: string;
}

interface Props {
  users:        PickableUser[];
  transcript:   string;     // what the speaker said — shown so they know the picker is about THEIR ask
  retryMessage: string;     // text to POST to /api/ask after switching
  onClose:      () => void;
  onSwitched:   (user: { id: number; name: string }) => void;
  // Escape hatch for the chicken-and-egg case: a wall whose UUID
  // isn't in trusted_kiosk_devices yet can't load pin-pickable
  // (kiosk-gated) and can't do anything from /ambient. When the
  // parent passes this callback, a small bottom-left button lets
  // the admin sign in with full password + land in /settings to
  // mark this device as a kiosk. Omitted on the voice-ID picker
  // where the user is mid-conversation and shouldn't see it.
  onSignInWithPassword?: () => void;
}

export function AvatarPinFallback({ users, transcript, retryMessage, onClose, onSwitched, onSignInWithPassword }: Props) {
  const [picked, setPicked]   = useState<PickableUser | null>(null);
  const [errorText, setError] = useState<string | undefined>(undefined);
  const [busy, setBusy]       = useState(false);

  async function trySwitch(pin: string): Promise<boolean> {
    if (!picked) return false;
    setBusy(true);
    setError(undefined);
    try {
      const r = await api.post<{ ok: boolean; user: { id: number; name: string } }>(
        "/api/auth/pin-switch",
        { user_id: picked.id, pin },
      );
      if (r?.ok && r?.user) {
        onSwitched(r.user);
        // Retry the original turn as the new user — through the
        // SAME voice flow that produced the transcript, not via a
        // text-only chat detour. Dispatch yorik:voice:resume; the
        // global VoiceFab listens, calls /api/ask-voice/resume
        // (which skips Whisper + voice ID and goes straight to
        // agent + TTS), and the answer streams back through the
        // same UI surface as a normal voice tap — staying on
        // /ambient. UI actions (show_calendar, show_photo, etc.)
        // dispatch from VoiceFab the same way they always do, so
        // "show me my calendar" actually opens the calendar.
        if (retryMessage) {
          try {
            window.dispatchEvent(new CustomEvent("yorik:voice:resume", {
              detail: { transcript: retryMessage },
            }));
          } catch {}
        }
        return true;
      }
      setError("Switch failed — try again");
      return false;
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      if (msg.includes("401") || msg.toLowerCase().includes("wrong pin")) {
        setError("Wrong PIN");
        return false;
      }
      if (msg.includes("429") || msg.toLowerCase().includes("too many")) {
        setError("Too many attempts — wait a minute");
        return false;
      }
      setError(msg);
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/85 backdrop-blur-md flex flex-col items-center justify-center px-6 py-10 text-white">
      {/* Close button — for "never mind" cases */}
      <button
        type="button"
        onClick={onClose}
        className="absolute top-6 right-6 h-10 w-10 rounded-full bg-white/10 hover:bg-white/20 flex items-center justify-center"
        aria-label="Cancel"
      >
        <X className="w-5 h-5" />
      </button>

      {!picked && (
        <>
          <div className="text-2xl font-light mb-2 text-center">
            Who's there?
          </div>
          {transcript && (
            <div className="text-sm text-white/60 mb-8 max-w-md text-center italic">
              "{transcript}"
            </div>
          )}
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4 max-w-3xl">
            {users.length === 0 && (
              <div className="col-span-full text-sm text-white/60 text-center max-w-md mx-auto">
                Nobody in this household has set a kiosk PIN yet. Open
                Settings → Profile on any device to set one and try again.
              </div>
            )}
            {users.map(u => (
              <button
                key={u.id}
                type="button"
                onClick={() => setPicked(u)}
                className="flex flex-col items-center gap-3 p-5 rounded-xl bg-white/10 hover:bg-white/20 active:scale-95 transition"
              >
                <div className="w-20 h-20 rounded-full bg-gradient-to-br from-violet-500 to-blue-500 flex items-center justify-center text-3xl font-semibold">
                  {(u.first_name?.[0] || u.name?.[0] || "?").toUpperCase()}
                </div>
                <div className="text-base font-medium">{u.first_name || u.name}</div>
              </button>
            ))}
          </div>
        </>
      )}

      {picked && (
        <div className="flex flex-col items-center">
          <button
            type="button"
            onClick={() => { setPicked(null); setError(undefined); }}
            className="mb-6 text-sm text-white/70 hover:text-white"
          >
            ← Not me
          </button>
          <PinPad
            prompt={`Hi ${picked.first_name || picked.name}, enter your PIN`}
            subline="4 digits"
            errorText={errorText}
            busy={busy}
            onSubmit={trySwitch}
          />
          {busy && (
            <div className="mt-4 text-white/60 text-xs inline-flex items-center gap-1.5">
              <Loader2 className="w-3 h-3 animate-spin" /> verifying…
            </div>
          )}
        </div>
      )}

      {/* Bottom-left escape hatch — only when the parent wired
          onSignInWithPassword AND we're still on the avatar grid
          (not in PIN entry). Lets a fresh-install wall break out
          of the kiosk overlay when pin-pickable returned 403
          because no trusted_kiosk_devices row exists yet. */}
      {!picked && onSignInWithPassword && (
        <button
          type="button"
          onClick={onSignInWithPassword}
          className="absolute bottom-6 left-6 h-10 px-4 rounded-full bg-white/10 hover:bg-white/20 text-sm text-white/80"
        >
          Sign in with password
        </button>
      )}

      {/* Backdrop click cancels — only when the avatar grid is up; the
          PinPad blocks accidental dismissal during entry. */}
      <button
        type="button"
        onClick={picked ? undefined : onClose}
        className={cn(
          "absolute inset-0 -z-10",
          picked ? "cursor-default" : "cursor-pointer",
        )}
        aria-hidden="true"
      />
    </div>
  );
}
