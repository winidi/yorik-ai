/**
 * Floating "Talk" button — always available bottom-right across every
 * React route. Mirrors the vanilla shell's `.voice-fab`.
 *
 * Flow (streaming):
 *   1. Tap mic → start MediaRecorder
 *   2. Tap again → stop recording, POST to /api/ask-voice/stream
 *   3. Read NDJSON event stream:
 *        transcript → show what user said (mode: 'transcribing' → 'thinking')
 *        ack        → play instant ack audio ("klar, moment")
 *        audio      → enqueue + play TTS chunks as they arrive
 *        done       → finalize, dispatch ui_actions, mode: 'done'
 *   4. UI actions (e.g. show_calendar with highlight_event_ids) are
 *      dispatched as window events so the relevant app picks them up.
 *
 * Hidden when the user is on the WhatsApp app (its own mic UI lives
 * inside the composer — having two recording fabs is confusing).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Mic, Square, Loader2, X, Sparkles, AlertCircle, Volume2, MessageSquare } from "lucide-react";
import { cn } from "@/lib/utils";
import { api, ApiError, wallDeviceHeader } from "@/lib/api";
import { PendingActionPanel } from "@/components/PendingActionPanel";
import { emitUiAction } from "@/lib/uiActions";

type Mode = "idle" | "recording" | "transcribing" | "thinking" | "speaking" | "done";

interface PendingAction {
  pending_id: string;
  skill: string;
  preview: any;
  llm_model?: string;
}

interface VoiceResult {
  transcript: string;
  response: string;
  degraded?: boolean;
  conversation_id?: string;
  pendingAction?: PendingAction;
  /** Counts of card-shaped surfaces (documents_found / photos_found)
   *  the voice popover deliberately does NOT render inline. We keep
   *  the numbers so the "Continue in chat" CTA can say "Open 5
   *  documents in chat" — the chat surface already renders the full
   *  cards, no need to maintain a second render path. */
  doc_count?: number;
  photo_count?: number;
  /** Speaker-ID match — set when the backend's identification event
   *  matched a voice-enrolled profile above the similarity threshold. */
  identifiedName?: string;
  identifiedLanguage?: string;
  /** True once /api/auth/voice-login has redeemed the swap_token and
   *  the session cookie has been swapped to the identified user.
   *  Drives the green "verified" dot in the popover header. */
  identifiedVerified?: boolean;
}

interface UiAction {
  type: string;
  [k: string]: any;
}

export function VoiceFab() {
  const location = useLocation();
  const navigate = useNavigate();
  const [mode, setMode] = useState<Mode>("idle");
  const [seconds, setSeconds] = useState(0);
  const [result, setResult] = useState<VoiceResult | null>(null);
  // Past turns in the SAME conversation_id — the streaming `result`
  // above is the current/active turn; history accumulates everything
  // before it so "Speak again" keeps the thread visible instead of
  // wiping it. Cleared on "New conversation".
  const [history, setHistory] = useState<VoiceResult[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const audioQueueRef = useRef<HTMLAudioElement[]>([]);
  const audioPlayingRef = useRef<boolean>(false);
  // VAD — silence-detection that auto-stops the recorder when the
  // user finishes speaking. Web Audio AnalyserNode tap on the same
  // MediaStream the MediaRecorder consumes; an interval polls RMS
  // and calls rec.stop() once silence has lasted EOS_SILENCE_MS.
  const vadCtxRef      = useRef<AudioContext | null>(null);
  const vadIntervalRef = useRef<number | null>(null);

  // Continuous (ping-pong) voice mode. Only enabled inside the
  // YorikWall wrapper, only on /chat (where the visual surface for
  // photos/docs/emails lives). After a voice turn's TTS finishes,
  // the mic auto-reopens for the next turn — no wake word needed.
  // Exits on: tap the "end" pill, 30s silence with no speech, error,
  // or navigation away from /chat.
  const continuousRef = useRef(false);
  const voiceDetectedRef = useRef(false);
  const ghostTimerRef = useRef<number | null>(null);
  // True once the server sent the `done` event for the current turn —
  // i.e., no more TTS audio chunks are coming. The audio queue might
  // still have items playing, but at least we know nothing new will
  // arrive. The continuous-mode auto-restart waits for BOTH this and
  // queue-empty before reopening the mic. Without it, the queue
  // briefly empties between sentences while the next sentence is
  // still arriving, and the mic would reopen mid-reply → permanent
  // feedback loop with Yorik's own voice as input.
  const ttsStreamCompleteRef = useRef(false);
  const CONTINUOUS_GHOST_MS = 20_000;
  const isWrapper = () => typeof window !== "undefined" &&
    navigator.userAgent.includes("YorikWall");
  // Routes where ping-pong stays alive. /ambient is included because
  // a voice turn often answers WITHOUT photos/docs (e.g. the LLM
  // asks a clarifying question) — the user stays on /ambient and
  // needs the mic to reopen for their reply. /chat is the
  // photo/doc/card surface we auto-nav to. Anywhere else (settings,
  // calendar, etc.), continuous exits.
  const isContinuousSurface = (path: string) =>
    path.includes("/ambient") || path.includes("/chat");

  // Cleanup if the user navigates away mid-recording.
  // IMPORTANT: this useEffect MUST stay above the route-based early return
  // below — otherwise the hook count changes between renders when the
  // user navigates onto/off of /whatsapp, which is a Rules of Hooks
  // violation and trips React error #300 / #310. Refresh "fixes" it
  // because a fresh mount on /whatsapp never has a prior render with
  // more hooks to compare against, but dock-click navigation does. This
  // was the actual cause of the long-standing intermittent black screen.
  useEffect(() => {
    return () => {
      stopRecorderImmediate();
      flushAudioQueue();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Apps that have their own voice/dictation entry inside their
  // composer — the global FAB would either duplicate the affordance
  // (WhatsApp's composer-native dictation) or visually collide with
  // a fixed-position composer button (Chat's Send button sits at the
  // same right/bottom rectangle as the FAB → FAB obscures Send).
  // Both of these screens dispatch the same yorik:voice:start event
  // from their inline Mic button, so nothing is lost.
  const hideOnRoutes = ["/whatsapp", "/chat", "/ambient"];
  const hidden = hideOnRoutes.some(r => location.pathname.startsWith(r));

  function stopRecorderImmediate() {
    if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }
    if (vadIntervalRef.current) { clearInterval(vadIntervalRef.current); vadIntervalRef.current = null; }
    if (vadCtxRef.current) {
      try { vadCtxRef.current.close(); } catch {}
      vadCtxRef.current = null;
    }
    const wasRecording = recorderRef.current?.state === "recording";
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      try { recorderRef.current.stop(); } catch {}
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(t => t.stop());
      streamRef.current = null;
    }
    recorderRef.current = null;
    if (wasRecording) {
      try {
        window.dispatchEvent(new CustomEvent("yorik:voice:recording-stopped"));
      } catch {}
    }
  }

  function flushAudioQueue() {
    for (const a of audioQueueRef.current) {
      try { a.pause(); a.src = ""; } catch {}
    }
    audioQueueRef.current = [];
    audioPlayingRef.current = false;
  }

  // Enqueue inline audio (base64) — used for ack + LLM TTS chunks.
  // Backend embeds bytes directly in the stream event because a
  // separate /api/tts-audio fetch was taking 5-10s due to HTTP/1.1
  // contention with the active streaming request. Inlining is instant.
  function enqueueAudioFromB64(b64: string, mime: string, tag: string) {
    // eslint-disable-next-line no-console
    console.log(`[voice] enqueue ${tag} inline_bytes=${b64.length} queue_len=${audioQueueRef.current.length}`);
    try {
      const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
      const blob = new Blob([bytes], { type: mime });
      const blobUrl = URL.createObjectURL(blob);
      const audio = new Audio(blobUrl);
      (audio as any)._yorikTag = tag;
      audioQueueRef.current.push(audio);
      if (!audioPlayingRef.current) void playNext();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn(`[voice] base64 decode failed for ${tag}:`, err);
    }
  }

  // Legacy URL-based enqueue (kept for back-compat if backend ever
  // serves a URL instead of inline bytes — currently unused by the
  // streaming endpoint but harmless).
  function enqueueAudio(url: string) {
    const tag = url.split("/").pop() || "audio";
    // eslint-disable-next-line no-console
    console.log(`[voice] enqueue ${tag} url=${url} queue_len=${audioQueueRef.current.length}`);
    fetch(url, { credentials: "include" })
      .then(r => r.blob())
      .then(blob => {
        const blobUrl = URL.createObjectURL(blob);
        const audio = new Audio(blobUrl);
        (audio as any)._yorikTag = tag;
        audioQueueRef.current.push(audio);
        if (!audioPlayingRef.current) void playNext();
      })
      .catch(err => {
        // eslint-disable-next-line no-console
        console.warn(`[voice] fetch failed for ${tag}:`, err);
      });
  }

  // Called from playNext when the audio queue fully drains. Decides
  // whether to auto-reopen the mic for ping-pong.
  //
  // Reads window.location.pathname (NOT the useLocation() value)
  // because playNext is created during a render and may still be
  // running its async chain when the user has navigated away —
  // useLocation's closure capture would still see the OLD path
  // (/ambient) and exit continuous mode by mistake. window.location
  // is always current. The leading "/r/" comes from BrowserRouter's
  // basename, so we substring-check for "/chat" rather than
  // exact-match.
  function onAudioQueueDrained() {
    if (!continuousRef.current) return;
    if (!isWrapper()) { exitContinuous(); return; }
    if (!isContinuousSurface(window.location.pathname)) { exitContinuous(); return; }
    // 800ms grace so the tail of the TTS doesn't bleed into the
    // mic's first VAD sample. echoCancellation helps but isn't
    // perfect on tablet speakers.
    window.setTimeout(() => {
      if (!continuousRef.current) return;
      if (!isContinuousSurface(window.location.pathname)) { exitContinuous(); return; }
      // Schedule the no-speech-yet ghost timer. If the VAD never
      // crosses speechThr in CONTINUOUS_GHOST_MS, we exit ping-pong
      // rather than sit on a live mic indefinitely.
      voiceDetectedRef.current = false;
      if (ghostTimerRef.current) window.clearTimeout(ghostTimerRef.current);
      ghostTimerRef.current = window.setTimeout(() => {
        if (!continuousRef.current) return;
        if (voiceDetectedRef.current) return;  // they spoke — VAD will handle it
        // Silent the whole time → exit continuous + close mic.
        exitContinuous();
        stopRecorderImmediate();
      }, CONTINUOUS_GHOST_MS);
      void start();
    }, 800);
  }

  function exitContinuous() {
    continuousRef.current = false;
    voiceDetectedRef.current = false;
    if (ghostTimerRef.current) {
      window.clearTimeout(ghostTimerRef.current);
      ghostTimerRef.current = null;
    }
    try {
      window.dispatchEvent(new CustomEvent("yorik:voice:continuous-ended"));
    } catch {}
  }

  async function playNext() {
    const audio = audioQueueRef.current.shift();
    if (!audio) {
      audioPlayingRef.current = false;
      // ONLY notify continuous-mode that TTS is done if the server
      // has sent its `done` event. Between TTS sentences the queue
      // briefly empties — without this gate we'd reopen the mic
      // mid-reply and pick up Yorik's own voice as the next turn.
      if (ttsStreamCompleteRef.current) {
        onAudioQueueDrained();
      }
      return;
    }
    audioPlayingRef.current = true;
    setMode(m => (m === "thinking" || m === "transcribing" ? "speaking" : m));
    const tag = (audio as any)._yorikTag || "audio";
    // eslint-disable-next-line no-console
    console.log(`[voice] playing ${tag}`);
    await new Promise<void>(resolve => {
      audio.onended = () => {
        // eslint-disable-next-line no-console
        console.log(`[voice] ended ${tag}`);
        // Free the blob URL to avoid memory leak.
        try { URL.revokeObjectURL(audio.src); } catch {}
        resolve();
      };
      audio.onerror = () => {
        // eslint-disable-next-line no-console
        console.warn(`[voice] error ${tag}`);
        resolve();
      };
      audio.play().catch(err => {
        // eslint-disable-next-line no-console
        console.warn(`[voice] play() rejected for ${tag}:`, err);
        resolve();
      });
    });
    void playNext();
  }

  const start = useCallback(async () => {
    setErr(null);
    // Park the just-finished turn in history so the popover keeps
    // showing it while the new turn is in flight. Skip incomplete
    // turns (no transcript) — those are just stale spinners.
    setResult(prev => {
      if (prev && prev.transcript && prev.transcript !== "(no transcript)") {
        setHistory(h => [...h, prev]);
      }
      return null;
    });
    flushAudioQueue();
    // Reset per-turn flags. ttsStreamComplete gates the continuous-
    // mode mic restart; voiceDetected gates the silent-skip in
    // handleStopped below. Both must start false for each new turn
    // — without this, a previous turn's value leaks into the next
    // (e.g., quiet turn after a loud one would skip the POST
    // incorrectly because voiceDetectedRef was still true).
    ttsStreamCompleteRef.current = false;
    voiceDetectedRef.current = false;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setErr("Your browser doesn't support audio recording.");
      setMode("done");
      return;
    }
    try {
      // Keep AGC + noise-suppression ON. Turning them off gave the VAD
      // a cleaner raw signal but starved Whisper of audio level (tablet
      // mics deliver very low input without AGC) — transcription
      // degraded noticeably. The dynamic-noise-floor calibration below
      // adapts to whatever level the browser's processing delivers, so
      // we don't actually need to fight it.
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const rec = new MediaRecorder(stream);
      recorderRef.current = rec;
      chunksRef.current = [];
      rec.ondataavailable = e => { if (e.data && e.data.size) chunksRef.current.push(e.data); };
      rec.onstop = () => { void handleStopped(rec.mimeType); };
      rec.start();
      setMode("recording");
      setSeconds(0);
      // Cross-surface signal — AmbientApp (and any other route that
      // hides the FAB) listens for these so it can render its own
      // listening indicator while VoiceFab itself is invisible.
      try {
        window.dispatchEvent(new CustomEvent("yorik:voice:recording-started"));
      } catch {}
      const startedAt = Date.now();
      tickRef.current = setInterval(() => {
        setSeconds(Math.floor((Date.now() - startedAt) / 1000));
      }, 250);

      // VAD: tap the same MediaStream with an AnalyserNode and poll
      // RMS every ~80ms. Once silence has lasted EOS_SILENCE_MS we
      // stop the recorder — same MediaRecorder.onstop handler runs
      // as if the user tapped "stop", so the rest of the flow is
      // unchanged. Falls back silently if AudioContext isn't
      // available; the user can still tap-stop manually.
      try {
        const Ctx: typeof AudioContext | undefined =
          (window as any).AudioContext || (window as any).webkitAudioContext;
        if (Ctx) {
          const ctx = new Ctx();
          vadCtxRef.current = ctx;
          const source   = ctx.createMediaStreamSource(stream);
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 512;
          source.connect(analyser);
          const data = new Uint8Array(analyser.fftSize);
          // Two-phase VAD:
          //   Phase 1 (waiting)  — mic open, no EOS yet. We only wait
          //                        for the user to START speaking. Safety
          //                        cap MAX_PRE_SPEECH_MS so a forgotten
          //                        mic eventually closes itself.
          //   Phase 2 (speaking) — once speech detected, the normal
          //                        EOS_SILENCE_MS-after-last-voice rule
          //                        kicks in. Short EOS = snappy turn end.
          const EOS_SILENCE_MS    = 1000;   // silence after speech = end of utterance
          const MAX_PRE_SPEECH_MS = 25_000; // safety cap if user never speaks
          // Dynamic threshold: measure the room's RMS floor during the
          // first NOISE_FLOOR_MS and set the speech threshold to a
          // multiple of it. Fixed thresholds fail on tablets where
          // mic input level varies wildly between OEMs / rooms.
          const NOISE_FLOOR_MS = 350;
          const SPEECH_MARGIN  = 2.5;   // RMS must exceed floor × this to count as voice
          const MIN_THR        = 0.004; // never go below this — pure silence
          // Hard cap on the calibrated threshold. If the user starts
          // speaking during the 350ms noise-floor window (common —
          // wake-word fires and the user already has their question
          // ready), the floor reading gets contaminated and the
          // dynamic threshold can land above normal speech RMS.
          // Without this cap, the VAD would then never detect
          // speech → recorder runs until MAX_PRE_SPEECH_MS → in
          // ping-pong, that's a permanent re-record loop.
          const MAX_THR        = 0.025;
          let lastVoiceAt = Date.now();
          let speechStarted = false;
          let noiseFloor  = 0;
          let floorSampleCount = 0;
          let speechThr   = MIN_THR;
          vadIntervalRef.current = window.setInterval(() => {
            analyser.getByteTimeDomainData(data);
            let ss = 0;
            for (let i = 0; i < data.length; i++) {
              const v = (data[i] - 128) / 128;
              ss += v * v;
            }
            const rms = Math.sqrt(ss / data.length);
            const now = Date.now();
            const elapsed = now - startedAt;
            // Broadcast amplitude for AmbientApp's visualiser. Same RMS
            // the VAD already computed — sharing avoids running a
            // second analyser tap.
            try {
              window.dispatchEvent(new CustomEvent("yorik:voice:level", {
                detail: { rms },
              }));
            } catch {}
            // Floor-calibration window: rolling-average RMS so we get
            // a sense of the room's resting noise. lastVoiceAt is kept
            // pinned at start, so we don't trigger EOS until floor is
            // set.
            if (elapsed < NOISE_FLOOR_MS) {
              noiseFloor += rms;
              floorSampleCount++;
              lastVoiceAt = now;
              return;
            }
            if (speechThr === MIN_THR && floorSampleCount > 0) {
              const avgFloor = noiseFloor / floorSampleCount;
              const raw = Math.max(MIN_THR, avgFloor * SPEECH_MARGIN);
              speechThr = Math.min(MAX_THR, raw);
              console.log("[VAD] calibrated speechThr=", speechThr.toFixed(4),
                          "(raw=", raw.toFixed(4), "floor=", avgFloor.toFixed(4), ")");
            }
            if (rms > speechThr) {
              lastVoiceAt = now;
              if (!speechStarted) {
                speechStarted = true;
                console.log("[VAD] speech started at", elapsed, "ms");
              }
              // Mark that the user actually spoke this turn — used by
              // continuous-mode's ghost timer to distinguish "stopped
              // after speech" from "never spoke at all".
              voiceDetectedRef.current = true;
            }
            // Phase 1: still waiting for first word. Don't EOS. Only
            // bail if the safety cap is hit (mic open, no speech for
            // 25s — user walked away or the mic is muted).
            if (!speechStarted) {
              if (elapsed > MAX_PRE_SPEECH_MS) {
                console.log("[VAD] no speech in", MAX_PRE_SPEECH_MS, "ms — closing mic");
                if (vadIntervalRef.current) {
                  clearInterval(vadIntervalRef.current);
                  vadIntervalRef.current = null;
                }
                if (recorderRef.current?.state === "recording") {
                  try { recorderRef.current.stop(); } catch {}
                }
              }
              return;
            }
            // Phase 2: speech detected, run normal EOS check.
            if (now - lastVoiceAt >= EOS_SILENCE_MS) {
              // Stop both the recorder AND the VAD loop. handleStopped
              // takes over from MediaRecorder.onstop.
              if (vadIntervalRef.current) {
                clearInterval(vadIntervalRef.current);
                vadIntervalRef.current = null;
              }
              if (recorderRef.current?.state === "recording") {
                try { recorderRef.current.stop(); } catch {}
              }
            }
          }, 80);
        }
      } catch (vadErr) {
        // Non-fatal — tap-to-stop still works.
        console.warn("VoiceFab: VAD setup failed", vadErr);
      }

    } catch (e: any) {
      setErr(e?.message || "Couldn't access the microphone.");
      setMode("done");
    }
  }, []);

  // Cross-component trigger — any UI can pop the recorder open by
  // dispatching `yorik:voice:start`. Used by the chat composer's
  // inline mic button so the user doesn't have to leave the
  // composer area to start talking. Ignored when already recording
  // so a double-fire is a no-op.
  useEffect(() => {
    function onTrigger(ev: Event) {
      if (mode === "recording" || mode === "transcribing" || mode === "thinking") return;
      // Continuous (ping-pong) mode: only inside the YorikWall
      // wrapper. A laptop user clicking the chat composer mic gets a
      // single turn, same as today. Wrapper users get auto-reopen
      // after TTS finishes, until they tap the end pill / stay
      // silent for 30s / navigate away.
      if (isWrapper()) {
        if (!continuousRef.current) {
          continuousRef.current = true;
          try {
            window.dispatchEvent(new CustomEvent("yorik:voice:continuous-started"));
          } catch {}
        }
      }
      // detail.source is set by the native wake-word receiver
      // ('wake') and by the AmbientApp idle overlay ('tap'); both
      // are kiosk-only paths so the continuous-mode flip above
      // already covered them. The detail is informational only.
      void ev;
      void start();
    }
    function onContinuousEnd() {
      exitContinuous();
      // Stop the mic if it's currently open. handleStopped's
      // dispatch of recording-stopped will hide the listening
      // overlay; exitContinuous already hides the end-pill.
      if (recorderRef.current?.state === "recording") {
        try { recorderRef.current.stop(); } catch {}
      } else {
        stopRecorderImmediate();
      }
    }
    window.addEventListener("yorik:voice:start", onTrigger);
    window.addEventListener("yorik:voice:continuous-end", onContinuousEnd);
    return () => {
      window.removeEventListener("yorik:voice:start", onTrigger);
      window.removeEventListener("yorik:voice:continuous-end", onContinuousEnd);
    };
  }, [start, mode]);

  // Exit continuous mode the moment the user navigates away from
  // /chat — they're not in the ping-pong surface anymore, the mic
  // shouldn't stay open. Uses the React-router pathname here (NOT
  // window.location) because this useEffect runs on each render
  // when the dep changes — that IS the freshest value React knows.
  useEffect(() => {
    if (!continuousRef.current) return;
    if (!isContinuousSurface(location.pathname)) {
      exitContinuous();
      if (recorderRef.current?.state === "recording") {
        try { recorderRef.current.stop(); } catch {}
      }
    }
  }, [location.pathname]);

  // Ping both idle timers every 5 seconds while a voice turn is
  // in flight (recording → transcribing → thinking → speaking).
  //   - YorikNative.resetIdleTimer keeps the Android wrapper's
  //     20s "return to ambient" from kicking in mid-sentence.
  //   - yorik:voice:active-tick keeps the PWA-level KioskIdleWatch
  //     from doing the same in a regular browser kiosk session.
  //     Without it, /chat would either need a static exemption
  //     (which over-applies to manual visits where the user is
  //     just sitting on /chat) or get yanked while Yorik is
  //     speaking. The event fires whether or not YorikNative is
  //     present.
  useEffect(() => {
    const active = mode === "recording" || mode === "transcribing"
                || mode === "thinking" || mode === "speaking";
    if (!active) return;
    const native = (window as any).YorikNative;
    const nativeReset = native?.resetIdleTimer;
    const ping = () => {
      if (typeof nativeReset === "function") {
        try { nativeReset.call(native); } catch {}
      }
      try {
        window.dispatchEvent(new CustomEvent("yorik:voice:active-tick"));
      } catch {}
    };
    ping();
    const id = window.setInterval(ping, 5_000);
    return () => clearInterval(id);
  }, [mode]);

  // PIN-switch retry path. AvatarPinFallback dispatches this after
  // a successful kiosk PIN-switch with the original transcript;
  // VoiceFab runs the agent + TTS with that text and streams the
  // answer through the same UI surface as a normal voice tap.
  useEffect(() => {
    function onResume(e: any) {
      const transcript = (e?.detail?.transcript || "").trim();
      const language   = e?.detail?.language;
      if (!transcript) return;
      if (mode === "recording" || mode === "transcribing" || mode === "thinking") return;
      void startResume(transcript, language);
    }
    window.addEventListener("yorik:voice:resume", onResume as any);
    return () => window.removeEventListener("yorik:voice:resume", onResume as any);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  async function handleStopped(mimeType: string | undefined) {
    if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }
    if (vadIntervalRef.current) { clearInterval(vadIntervalRef.current); vadIntervalRef.current = null; }
    if (vadCtxRef.current) {
      try { vadCtxRef.current.close(); } catch {}
      vadCtxRef.current = null;
    }
    const stream = streamRef.current;
    if (stream) stream.getTracks().forEach(t => t.stop());
    streamRef.current = null;
    try {
      window.dispatchEvent(new CustomEvent("yorik:voice:recording-stopped"));
    } catch {}

    // Silent-close shortcut. The recorder can stop without the user
    // ever having spoken: VAD's MAX_PRE_SPEECH_MS safety cap, the
    // continuous-mode ghost timer firing, or the user tapping the
    // end pill while the mic was open and idle. In any of those
    // cases the captured audio is just room noise — POSTing it
    // makes the server fire its unconditional "Sure, one moment"
    // ack and (worse) burn an LLM call on the resulting empty /
    // hallucinated transcript. Better to just drop the turn.
    if (!voiceDetectedRef.current) {
      chunksRef.current = [];
      setMode("idle");
      return;
    }

    setMode("transcribing");

    const blob = new Blob(chunksRef.current, { type: mimeType || "audio/webm" });
    chunksRef.current = [];
    if (blob.size < 1000) {
      setErr("That was too short — try again.");
      setMode("done");
      return;
    }

    const fd = new FormData();
    fd.append("audio", blob, "voice.webm");
    // continuous=1 tells the backend "this is a ping-pong follow-up,
    // not the wake-fire turn that decides identity". On a voice-ID
    // threshold miss the backend then falls through with the current
    // session instead of emitting identify_needed (which would dead-
    // end on /chat where the avatar+PIN picker isn't mounted, and
    // would interrupt an authenticated conversation anyway).
    const params: string[] = [];
    if (conversationId) params.push(`conversation_id=${encodeURIComponent(conversationId)}`);
    if (continuousRef.current) params.push("continuous=1");
    const url = `/api/ask-voice/stream${params.length ? `?${params.join("&")}` : ""}`;

    try {
      const r = await fetch(url, {
        method: "POST", body: fd, credentials: "include",
        headers: { ...wallDeviceHeader() },
      });
      if (!r.ok || !r.body) {
        const j = await r.json().catch(() => ({} as any));
        throw new Error(j.detail || `HTTP ${r.status}`);
      }
      await processVoiceNdjson(r);
    } catch (e: any) {
      setErr(e?.message || "Voice request failed.");
      setMode("done");
    }
  }

  /**
   * Shared NDJSON processor for both /api/ask-voice/stream (audio
   * input) and /api/ask-voice/resume (text input, used by the
   * AvatarPinFallback to re-fire the original ask after a PIN
   * switch). The resume endpoint omits `transcript` and `ack`
   * events — the parser handles that silently (nothing to do for
   * unsent events).
   */
  async function processVoiceNdjson(r: Response): Promise<void> {
      // Both callers already verified r.body before calling — this is
      // belt-and-braces for the type checker.
      if (!r.body) throw new Error("voice stream: no response body");
      // NDJSON parser — one JSON object per line. The server flushes after
      // each event so we get instant feedback for transcript/ack/audio.
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let transcript = "";
      let response = "";
      let degraded = false;
      let convId: string | undefined;
      let uiActions: UiAction[] = [];
      let identifiedName: string | undefined;
      let identifiedLanguage: string | undefined;

      const streamStart = performance.now();
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) !== -1) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          let evt: any;
          try { evt = JSON.parse(line); } catch { continue; }
          const elapsedMs = Math.round(performance.now() - streamStart);
          // eslint-disable-next-line no-console
          console.log(`[voice] +${elapsedMs}ms event=${evt.type}`, evt.text ? `text=${(evt.text || "").slice(0, 40)}` : "");

          switch (evt.type) {
            case "transcript":
              transcript = (evt.text || "").trim();
              setResult({ transcript, response: "", degraded: false });
              setMode("thinking");
              break;
            case "identification":
              // Backend matched the speaker against an enrolled voice
              // profile. Surface the name + a green dot in the popover
              // header so the user sees "Hi Anna" wow effect immediately.
              if (evt.identified && evt.identified.name) {
                identifiedName = evt.identified.name;
                identifiedLanguage = evt.identified.language;
                setResult(r => r ? { ...r, identifiedName, identifiedLanguage } : r);
                // If the backend minted a swap_token, redeem it in the
                // background to swap the cookie over to the matched
                // user. Fire-and-forget: the LLM stream keeps flowing
                // either way, and the matched user's role/language
                // already informed the response server-side, so a
                // failed redemption only means "next request runs as
                // the device-owner cookie again." Other surfaces
                // listen for yorik:user:switched to refresh their
                // user-scoped state (calendar, mail, etc.).
                if (evt.identified.swap_token && evt.identified.profile_id) {
                  api.post<{ ok: boolean; user: { id: number; name: string; role: string } }>(
                    "/api/auth/voice-login",
                    {
                      swap_token: String(evt.identified.swap_token),
                      profile_id: Number(evt.identified.profile_id),
                    },
                  ).then(r => {
                    if (r?.ok && r?.user) {
                      setResult(rr => rr ? { ...rr, identifiedVerified: true } : rr);
                      try {
                        window.dispatchEvent(new CustomEvent("yorik:user:switched", {
                          detail: r.user,
                        }));
                      } catch {}
                    }
                  }).catch(e => {
                    // Don't surface as an error toast — the voice turn
                    // is still answering correctly; just the session
                    // swap didn't take. Worst case: user PIN-switches
                    // by tapping their avatar.
                    if (!(e instanceof ApiError)) console.warn("voice-login failed:", e);
                  });
                }
              }
              break;
            case "ack":
              // Tiny delay before playing the "Klar, Moment" ack so it
              // doesn't feel like Yorik is interrupting / pre-loaded.
              // Natural human conversation has a small beat between
              // "I heard you" and the actual acknowledgement.
              if (evt.audio_b64) {
                const b64 = evt.audio_b64; const mime = evt.mime || "audio/wav";
                setTimeout(() => enqueueAudioFromB64(b64, mime, "ack"), 600);
              } else if (evt.url) {
                const u = evt.url;
                setTimeout(() => enqueueAudio(u), 600);
              }
              break;
            case "audio":
              if (evt.audio_b64) enqueueAudioFromB64(evt.audio_b64, evt.mime || "audio/wav", `audio-${evt.index}`);
              else if (evt.url) enqueueAudio(evt.url);
              break;
            case "text_delta":
              // Token-level text streaming from the LLM. Append to the
              // running response buffer and surface it in the modal so
              // the user SEES Yorik forming the answer (the audio also
              // streams per-sentence via the `audio` events above).
              // On `done` we replace this buffer with the canonical
              // response text in case the agent post-processed it.
              if (evt.text) {
                response += evt.text;
                // Switch out of "thinking" once the first delta lands.
                setMode(m => m === "thinking" ? "speaking" : m);
                setResult(r => r
                  ? { ...r, response }
                  : { transcript, response, degraded: false });
              }
              break;
            case "done":
              response = (evt.response || response || "").trim();
              convId = evt.conversation_id;
              uiActions = evt.ui_actions || [];
              degraded = !!evt.degraded;
              // Mark the TTS stream complete. From now on, when the
              // audio queue finishes draining, playNext will fire
              // onAudioQueueDrained → continuous-mode mic restart.
              // If audio was already drained when `done` arrived
              // (e.g., the reply was text-only or the last sentence
              // finished before the `done` event), fire the drain
              // handler directly — there's no future playNext call
              // to trigger it otherwise.
              ttsStreamCompleteRef.current = true;
              // Stop-phrase shortcut from backend — user said "stop"
              // / "stopp" / "halt" / "ende" mid-conversation. Exit
              // continuous mode immediately, no audio is coming.
              // No turn-completed event either: nothing was saved
              // to the conversation, nothing to refetch in /chat.
              if (evt.early_exit) {
                if (continuousRef.current) exitContinuous();
              }
              if (!audioPlayingRef.current && audioQueueRef.current.length === 0) {
                onAudioQueueDrained();
              }
              // Tell ChatApp (or anything else watching this thread)
              // that a voice turn just landed so it can refetch. The
              // server has persisted both the user transcript and
              // the assistant reply by the time `done` fires, so a
              // GET /api/conversations/{id} now returns the full
              // updated thread. Skipped on early_exit — there's
              // nothing new on the server to fetch.
              if (convId && !evt.early_exit) {
                try {
                  window.dispatchEvent(new CustomEvent("yorik:voice:turn-completed", {
                    detail: { conversation_id: convId },
                  }));
                } catch {}
              }
              break;
            case "identify_needed":
              // Kiosk + unknown speaker — backend refused to attribute
              // the turn to anyone. Dispatch a window event so the
              // AmbientApp can render its avatar+PIN picker. Stop the
              // stream cleanly: no error toast, no chat continuation;
              // the picker takes over the UX from here.
              try {
                window.dispatchEvent(new CustomEvent("yorik:identify-needed", {
                  detail: {
                    users:          evt.users || [],
                    transcript:     evt.transcript || "",
                    retry_message:  evt.retry_message || evt.transcript || "",
                  },
                }));
              } catch {}
              // Reset the popover state so the FAB doesn't sit on
              // "thinking" forever; the picker is the new active
              // surface.
              setMode("idle");
              setResult(null);
              return;
            case "error":
              // Errors break the ping-pong loop — don't auto-reopen
              // the mic if we were in continuous mode, the user
              // probably needs to see what went wrong.
              if (continuousRef.current) exitContinuous();
              throw new Error(evt.error || "unknown error");
          }
        }
      }

      // Pick out the pending_confirmation action (if any) so we can
      // render an inline panel — yes/nein is a voice-native flow and
      // stays here. Card-shaped surfaces (documents_found,
      // photos_found) are deliberately NOT rendered: we only count
      // them so the "Continue in chat" CTA can say "Open N documents
      // in chat", and let the chat surface — which already does
      // full cards — handle the actual UI. Avoids two render paths
      // for the same data.
      const pendingAction = uiActions.find(a => a.type === "pending_confirmation") as any;
      let docCount = 0;
      let photoCount = 0;
      const dispatchable: UiAction[] = [];
      for (const a of uiActions) {
        if (a.type === "pending_confirmation") continue;
        if (a.type === "documents_found" && Array.isArray((a as any).documents)) {
          docCount += ((a as any).documents as unknown[]).length;
          continue;
        }
        if (a.type === "photos_found" && Array.isArray((a as any).photos)) {
          photoCount += ((a as any).photos as unknown[]).length;
          continue;
        }
        dispatchable.push(a);
      }

      setResult({
        transcript: transcript || "(no transcript)",
        response:   response   || "(no response)",
        degraded,
        conversation_id: convId,
        identifiedName,
        identifiedLanguage,
        doc_count:   docCount   || undefined,
        photo_count: photoCount || undefined,
        pendingAction: pendingAction ? {
          pending_id: pendingAction.pending_id,
          skill:      pendingAction.skill,
          preview:    pendingAction.preview,
          llm_model:  pendingAction.llm_model,
        } : undefined,
      });
      if (convId) setConversationId(convId);

      // Auto-navigate from /ambient to /chat when the response has
      // photos or documents — those need the chat surface to render
      // (photos_found / documents_found are deliberately filtered
      // out of `dispatchable` above so check the raw uiActions).
      // Wrapper-only; non-kiosk routes manage their own navigation.
      // Calendar keeps its own /calendar route via the existing
      // check below.
      const needsChatSurface = uiActions.some(a =>
        a.type === "photos_found" || a.type === "documents_found"
      );
      if (needsChatSurface && isWrapper() && location.pathname.startsWith("/ambient")) {
        navigate("/chat" + (convId ? `?conversation_id=${encodeURIComponent(convId)}` : ""));
      }

      for (const action of dispatchable) {
        if (action.type === "show_calendar" && location.pathname !== "/calendar") {
          navigate("/calendar");
        }
        // emitUiAction queues the action so CalendarApp can pick it up
        // on mount, even if the navigation hasn't completed yet (it's
        // async — the event would otherwise fire before the new
        // component's listener attaches).
        emitUiAction(action);
      }

      // Stay in 'speaking' until queue drains; if no audio came back,
      // jump straight to 'done'.
      if (!audioPlayingRef.current && audioQueueRef.current.length === 0) {
        setMode("done");
      } else {
        // Watcher: flip to 'done' when audio finishes playing
        const id = setInterval(() => {
          if (!audioPlayingRef.current && audioQueueRef.current.length === 0) {
            clearInterval(id);
            setMode("done");
          }
        }, 250);
      }
  }

  /**
   * Resume the original voice ask after a PIN switch on the kiosk
   * wall. Skips STT + voice-ID (already done by the original
   * /api/ask-voice/stream call that emitted identify_needed) and
   * runs the agent + TTS with the captured transcript as the new
   * actor. Reaches the same UI through the same NDJSON parser,
   * so the household experience is "PIN → answer streams back +
   * speaks" without leaving /ambient.
   */
  async function startResume(transcript: string, language?: string) {
    setMode("thinking");
    setResult({ transcript, response: "", degraded: false });
    try {
      const r = await fetch("/api/ask-voice/resume", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...wallDeviceHeader() },
        body: JSON.stringify({
          transcript,
          conversation_id: conversationId || undefined,
          language: language || undefined,
        }),
      });
      if (!r.ok || !r.body) {
        const j = await r.json().catch(() => ({} as any));
        throw new Error(j.detail || `HTTP ${r.status}`);
      }
      await processVoiceNdjson(r);
    } catch (e: any) {
      setErr(e?.message || "Voice resume failed.");
      setMode("done");
    }
  }

  function stop() {
    if (recorderRef.current && recorderRef.current.state === "recording") {
      try { recorderRef.current.stop(); } catch {}
    }
  }

  function reset() {
    setMode("idle");
    setResult(null);
    setErr(null);
    setSeconds(0);
    flushAudioQueue();
  }

  function clearConversation() {
    setConversationId(null);
    setHistory([]);
    reset();
  }

  // ── render ─────────────────────────────────────────────────────────

  const fabBase = "fixed right-4 z-[60] inline-flex items-center gap-2 rounded-full font-medium shadow-lg transition";
  const fabPos  = "bottom-20";

  // Recording: red FAB with timer
  if (mode === "recording") {
    return (
      <button
        onClick={stop}
        className={cn(fabBase, fabPos, "px-4 py-2.5 bg-red-500 hover:bg-red-600 text-white animate-pulse-soft")}
        title="Tap to stop"
        aria-label="Stop recording"
      >
        <Square className="w-4 h-4 fill-current" />
        <span className="text-xs tabular-nums">REC {seconds}s</span>
        <style>{`
          @keyframes pulse-soft { 0%,100% { transform: scale(1) } 50% { transform: scale(1.04) } }
          .animate-pulse-soft { animation: pulse-soft 1.4s ease-in-out infinite; }
        `}</style>
      </button>
    );
  }

  // Inflight modes (transcribing / thinking / speaking) — keep FAB visible
  // with status icon + show the popover early with the transcript as soon as it lands.
  const inflight = mode === "transcribing" || mode === "thinking" || mode === "speaking";

  // Hidden on routes that have their own dictation UX (WhatsApp composer).
  // Early return AFTER all hooks so the hook count is stable across renders
  // when the route changes.
  if (hidden) return null;

  return (
    <>
      <button
        onClick={inflight ? undefined : start}
        disabled={inflight}
        className={cn(
          fabBase, fabPos,
          "w-12 h-12 justify-center bg-card border border-border text-foreground hover:bg-muted hover:shadow-xl",
          inflight && "opacity-90 cursor-wait",
        )}
        aria-label={
          mode === "transcribing" ? "Transcribing…" :
          mode === "thinking"     ? "Thinking…" :
          mode === "speaking"     ? "Speaking…" :
          "Talk to Yorik"
        }
        title={
          mode === "transcribing" ? "Transcribing…" :
          mode === "thinking"     ? "Thinking…" :
          mode === "speaking"     ? "Speaking…" :
          "Talk to Yorik"
        }
      >
        {mode === "transcribing" || mode === "thinking"
          ? <Loader2 className="w-5 h-5 animate-spin text-violet-500" />
          : mode === "speaking"
          ? <Volume2 className="w-5 h-5 text-violet-500 animate-pulse" />
          : <Mic className="w-5 h-5 text-violet-500" />}
      </button>

      {(result || err || history.length > 0) && (inflight || mode === "done") && (
        <ResultPopover
          mode={mode}
          result={result}
          history={history}
          error={err}
          conversationId={conversationId}
          onClose={reset}
          onSpeakAgain={start}
          onNewConversation={clearConversation}
          onContinueInChat={() => {
            // Park the live turn into history first — same as start()
            // does — so when the user comes back to voice the thread
            // is still visible. (Chat picks up from server-side ledger
            // independently.)
            reset();
            const id = conversationId;
            if (id) navigate(`/chat?conversation_id=${encodeURIComponent(id)}`);
            else    navigate(`/chat`);
          }}
        />
      )}
    </>
  );
}

function ResultPopover({
  mode, result, history, error, conversationId,
  onClose, onSpeakAgain, onNewConversation, onContinueInChat,
}: {
  mode: Mode;
  result: VoiceResult | null;
  history: VoiceResult[];
  error: string | null;
  conversationId: string | null;
  onClose: () => void;
  onSpeakAgain: () => void;
  onNewConversation: () => void;
  onContinueInChat: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  // Keep the newest turn visible: scroll to the bottom whenever new
  // text lands (transcript arrives, response streams, history grows).
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [history.length, result?.transcript, result?.response]);

  const statusLabel =
    mode === "transcribing" ? "Transcribing…" :
    mode === "thinking"     ? "Thinking…" :
    mode === "speaking"     ? "Speaking…" :
    null;

  return (
    <div className="fixed bottom-36 right-4 z-[59] w-[360px] max-w-[calc(100vw-2rem)] bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
      <header className="px-4 py-2.5 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-full bg-violet-500/15 flex items-center justify-center">
            <Sparkles className="w-3.5 h-3.5 text-violet-500" />
          </div>
          <span className="text-xs font-semibold">Yorik · voice</span>
          {result?.identifiedName && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-700 inline-flex items-center gap-1"
              title="Speaker matched a voice-enrolled profile"
            >
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
              {result.identifiedName}
              {result.identifiedLanguage && (
                <span className="text-emerald-600/60 font-mono uppercase">{result.identifiedLanguage}</span>
              )}
            </span>
          )}
          {statusLabel && (
            <span className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-violet-500/10 text-violet-600 inline-flex items-center gap-1">
              <Loader2 className="w-2.5 h-2.5 animate-spin" />
              {statusLabel}
            </span>
          )}
          {result?.degraded && (
            <span className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-600">
              offline
            </span>
          )}
        </div>
        <button
          onClick={onClose}
          aria-label="Close"
          className="w-7 h-7 rounded-md hover:bg-muted text-muted-foreground flex items-center justify-center"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </header>

      <div ref={scrollRef} className="px-4 py-3 space-y-4 max-h-[50vh] overflow-y-auto">
        {error && (
          <div className="text-xs text-red-600 flex items-start gap-2">
            <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}
        {/* Past turns in this conversation. Pending-action panel is
            intentionally not re-rendered here — past turns are read-
            only history; the active turn (below) is where any
            in-progress action lives. */}
        {history.map((t, i) => (
          <div
            key={i}
            className="space-y-2 pb-3 border-b border-border/60 last:border-b-0 opacity-80"
          >
            {t.transcript && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-0.5">You said</div>
                <div className="text-sm italic">"{t.transcript}"</div>
              </div>
            )}
            {t.response && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-0.5">Yorik</div>
                <div className="text-sm leading-relaxed whitespace-pre-wrap">{t.response}</div>
              </div>
            )}
          </div>
        ))}
        {/* Active turn (live transcript + streaming response + pending action). */}
        {result?.transcript && (
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-0.5">You said</div>
            <div className="text-sm italic">"{result.transcript}"</div>
          </div>
        )}
        {result?.response && (
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-0.5">Yorik</div>
            <div className="text-sm leading-relaxed whitespace-pre-wrap">{result.response}</div>
          </div>
        )}
        {result?.pendingAction && (
          <PendingActionPanel action={result.pendingAction} compact />
        )}
        {!result?.response && (mode === "thinking" || mode === "speaking") && (
          <div className="text-xs text-muted-foreground italic">
            {mode === "thinking" ? "(working on it…)" : "(speaking…)"}
          </div>
        )}
      </div>

      {mode === "done" && (() => {
        // Promote the chat handoff when the agent produced card-shaped
        // results (documents, photos): the voice popover deliberately
        // doesn't render those, so chat is genuinely the right next
        // step. Otherwise "Speak again" stays primary and chat is a
        // secondary affordance.
        const docCount = result?.doc_count || 0;
        const photoCount = result?.photo_count || 0;
        const hasCards = docCount > 0 || photoCount > 0;
        const chatLabel = !hasCards ? "Continue in chat"
          : docCount && !photoCount   ? `Open ${docCount} document${docCount === 1 ? "" : "s"} in chat`
          : photoCount && !docCount   ? `Open ${photoCount} photo${photoCount === 1 ? "" : "s"} in chat`
          : `Open ${docCount + photoCount} results in chat`;
        return (
          <footer className="px-4 py-2.5 border-t border-border bg-muted/20 flex items-center justify-between gap-2 flex-wrap">
            <button
              onClick={onNewConversation}
              className="text-[10px] text-muted-foreground hover:text-foreground transition"
              title="Start a fresh conversation (forget context)"
            >
              New conversation
            </button>
            <div className="flex items-center gap-2">
              {conversationId && (
                <button
                  onClick={onContinueInChat}
                  className={cn(
                    "text-xs px-3 py-1.5 rounded-md font-medium inline-flex items-center gap-1.5 transition",
                    hasCards
                      ? "bg-violet-500 hover:bg-violet-600 text-white"
                      : "bg-card border border-border hover:bg-muted text-foreground",
                  )}
                  title="Open this conversation in Chat for the full card UI + scrollback"
                >
                  <MessageSquare className="w-3 h-3" /> {chatLabel}
                </button>
              )}
              <button
                onClick={onSpeakAgain}
                className={cn(
                  "text-xs px-3 py-1.5 rounded-md font-medium inline-flex items-center gap-1.5 transition",
                  hasCards
                    ? "bg-card border border-border hover:bg-muted text-foreground"
                    : "bg-violet-500 hover:bg-violet-600 text-white",
                )}
              >
                <Mic className="w-3 h-3" /> Speak again
              </button>
            </div>
          </footer>
        );
      })()}
    </div>
  );
}

