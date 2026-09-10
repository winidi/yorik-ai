/**
 * RecorderDock — the one recorder for long conversations (dinner,
 * meeting). Mounted once outside ChromeGate so it survives every route
 * change, including the kiosk wrapper's idle-return to /ambient in the
 * middle of a 60-minute dinner.
 *
 * Flow: someone creates a recording (POST /api/recordings, or the
 * start_recording skill from chat/voice, which arrives as a ui_action)
 * → startRecording() opens the mic, MediaRecorder hands over a chunk
 * every CHUNK_MS, each chunk is uploaded in order with retries →
 * stop (button, "the dinner is over" via the stop_recording ui_action,
 * or stop_requested seen while polling) → last chunk, then /finish.
 * The per-recording upload token keeps the uploads valid even when the
 * shared tablet's session changes hands (PIN switch) during the meal.
 *
 * State lives in a module-level store (not React state) so a reload
 * can resume: the active recording is mirrored to sessionStorage and a
 * fresh MediaRecorder continues with the next chunk sequence number.
 */
import { useEffect, useState } from "react";
import { Circle, Loader2, Pause, Play, Square, X } from "lucide-react";
import { api } from "@/lib/api";
import { drainUiActions } from "@/lib/uiActions";
import { cn } from "@/lib/utils";

const CHUNK_MS = 120_000;         // MediaRecorder timeslice
const POLL_MS = 15_000;           // stop_requested check
const RESUME_KEY = "yorik:recorder";
const HIDE_AFTER_MS = 8_000;

export type RecorderPhase = "idle" | "starting" | "recording" | "paused" | "finishing" | "done" | "error";

export interface RecorderState {
  phase: RecorderPhase;
  recordingId: number | null;
  title: string;
  seconds: number;
  pendingUploads: number;
  error: string | null;
  needsResume: boolean;         // a reload interrupted a recording; tap to continue
}

interface Active {
  id: number;
  title: string;
  token: string | null;
  seq: number;
  startedAt: number;            // epoch ms of the first start
  pausedMs: number;             // accumulated pause time
  pausedAt: number | null;
}

let state: RecorderState = { phase: "idle", recordingId: null, title: "", seconds: 0, pendingUploads: 0, error: null, needsResume: false };
let active: Active | null = null;
let recorder: MediaRecorder | null = null;
let stream: MediaStream | null = null;
let tick: number | null = null;
let poll: number | null = null;
let wakeLock: any = null;
let stopping = false;
const queue: Array<{ seq: number; blob: Blob }> = [];
let uploading = false;
const listeners = new Set<(s: RecorderState) => void>();

function set(patch: Partial<RecorderState>) {
  state = { ...state, ...patch };
  for (const fn of listeners) fn(state);
}

export function getRecorderState(): RecorderState { return state; }
export function subscribeRecorder(fn: (s: RecorderState) => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

function persist() {
  try {
    if (active) sessionStorage.setItem(RESUME_KEY, JSON.stringify(active));
    else sessionStorage.removeItem(RESUME_KEY);
  } catch {}
}

function elapsedSeconds(): number {
  if (!active) return 0;
  const pausedNow = active.pausedAt ? Date.now() - active.pausedAt : 0;
  return Math.max(0, Math.floor((Date.now() - active.startedAt - active.pausedMs - pausedNow) / 1000));
}

function headers(): Record<string, string> | undefined {
  return active?.token ? { "X-Recording-Token": active.token } : undefined;
}

async function requestWakeLock() {
  try {
    const nav: any = navigator;
    if (nav.wakeLock?.request && !wakeLock) {
      wakeLock = await nav.wakeLock.request("screen");
      wakeLock.addEventListener?.("release", () => { wakeLock = null; });
    }
  } catch {}
}

function releaseWakeLock() {
  try { wakeLock?.release?.(); } catch {}
  wakeLock = null;
}

async function drainQueue(): Promise<void> {
  if (uploading || !active) return;
  uploading = true;
  try {
    while (queue.length && active) {
      const item = queue[0];
      const form = new FormData();
      form.append("seq", String(item.seq));
      form.append("audio", item.blob, `chunk-${item.seq}.webm`);
      let ok = false;
      for (let attempt = 0; attempt < 4 && !ok; attempt++) {
        try {
          await api.postForm(`/api/recordings/${active.id}/chunk`, form, headers());
          ok = true;
        } catch (e: any) {
          if (e?.status === 403 || e?.status === 404 || e?.status === 409) throw e;   // no point retrying
          await new Promise(r => setTimeout(r, 1500 * (attempt + 1)));
        }
      }
      if (!ok) break;                          // leave it queued; the next chunk retries
      queue.shift();
      set({ pendingUploads: queue.length });
    }
  } catch (e: any) {
    set({ error: e?.message || "upload failed", pendingUploads: queue.length });
  } finally {
    uploading = false;
  }
}

function attachRecorder(rec: MediaRecorder) {
  rec.ondataavailable = (e: BlobEvent) => {
    if (!e.data || !e.data.size || !active) return;
    queue.push({ seq: active.seq++, blob: e.data });
    persist();
    set({ pendingUploads: queue.length });
    void drainQueue();
  };
  rec.onerror = () => set({ error: "the microphone stopped" });
}

async function openMic(): Promise<MediaRecorder> {
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    throw new Error("this browser cannot record audio");
  }
  stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find(m => (MediaRecorder as any).isTypeSupported?.(m));
  const rec = mime ? new MediaRecorder(stream, { mimeType: mime, audioBitsPerSecond: 32_000 }) : new MediaRecorder(stream);
  attachRecorder(rec);
  return rec;
}

function startTicking() {
  if (tick) window.clearInterval(tick);
  tick = window.setInterval(() => set({ seconds: elapsedSeconds() }), 1000);
  if (poll) window.clearInterval(poll);
  poll = window.setInterval(async () => {
    if (!active || state.phase !== "recording" && state.phase !== "paused") return;
    try {
      const r = await api.get<{ stop_requested?: boolean; status?: string }>(`/api/recordings/${active.id}`);
      if (r.stop_requested || (r.status && r.status !== "recording")) void stopRecording();
    } catch {}
  }, POLL_MS);
}

function teardown() {
  if (tick) { window.clearInterval(tick); tick = null; }
  if (poll) { window.clearInterval(poll); poll = null; }
  try { stream?.getTracks().forEach(t => t.stop()); } catch {}
  stream = null;
  recorder = null;
  releaseWakeLock();
}

/** Start capturing for an existing recording row. */
export async function startRecording(opts: { id: number; title?: string; upload_token?: string | null }): Promise<void> {
  if (active && state.phase !== "idle" && state.phase !== "done" && state.phase !== "error") {
    if (active.id === opts.id) return;
    throw new Error("another recording is running");
  }
  active = { id: opts.id, title: opts.title || "Recording", token: opts.upload_token || null, seq: 0, startedAt: Date.now(), pausedMs: 0, pausedAt: null };
  queue.length = 0;
  stopping = false;
  set({ phase: "starting", recordingId: opts.id, title: active.title, seconds: 0, pendingUploads: 0, error: null, needsResume: false });
  try {
    recorder = await openMic();
    recorder.start(CHUNK_MS);
    persist();
    await requestWakeLock();
    startTicking();
    set({ phase: "recording" });
    window.dispatchEvent(new CustomEvent("yorik:recorder:started", { detail: { id: opts.id } }));
  } catch (e: any) {
    teardown();
    set({ phase: "error", error: e?.message || "could not start" });
    active = null;
    persist();
    throw e;
  }
}

/** Create the row and start; what the kiosk tile and the phone page use. */
export async function createAndStart(body: { title?: string; kind?: string; participants?: string[] }): Promise<number> {
  const row = await api.post<{ id: number; title: string; upload_token?: string }>("/api/recordings", body);
  await startRecording({ id: row.id, title: row.title, upload_token: row.upload_token || null });
  return row.id;
}

export function pauseRecording() {
  if (!recorder || !active || state.phase !== "recording") return;
  try { recorder.pause(); } catch { return; }
  active.pausedAt = Date.now();
  persist();
  set({ phase: "paused" });
}

export function resumeRecording() {
  if (!recorder || !active || state.phase !== "paused") return;
  try { recorder.resume(); } catch { return; }
  if (active.pausedAt) { active.pausedMs += Date.now() - active.pausedAt; active.pausedAt = null; }
  persist();
  set({ phase: "recording" });
}

/** Stop, upload the tail, finish. Safe to call twice. */
export async function stopRecording(): Promise<void> {
  if (!active || stopping) return;
  stopping = true;
  set({ phase: "finishing" });
  const rec = recorder;
  const id = active.id;
  const duration = elapsedSeconds();
  if (rec && rec.state !== "inactive") {
    await new Promise<void>(resolve => {
      const done = () => resolve();
      rec.addEventListener("stop", done, { once: true });
      try { rec.stop(); } catch { resolve(); }
      window.setTimeout(resolve, 5000);
    });
  }
  // the final ondataavailable fired before "stop"; push everything out
  for (let i = 0; i < 6 && queue.length; i++) {
    await drainQueue();
    if (queue.length) await new Promise(r => setTimeout(r, 2000));
  }
  const hdr = headers();
  teardown();
  if (queue.length) {
    set({ phase: "error", error: `${queue.length} piece(s) could not be uploaded; the recording is kept on this device until the network is back` });
    stopping = false;
    return;
  }
  try {
    await api.post(`/api/recordings/${id}/finish`, { duration_s: duration }, ) ;
  } catch (e: any) {
    // the session may have changed hands on a shared tablet; the token path is a plain fetch
    try {
      const res = await fetch(`/api/recordings/${id}/finish`, { method: "POST", credentials: "include",
        headers: { "content-type": "application/json", ...(hdr || {}) }, body: JSON.stringify({ duration_s: duration }) });
      if (!res.ok) throw new Error(`finish failed (${res.status})`);
    } catch (e2: any) {
      set({ phase: "error", error: e2?.message || e?.message || "finish failed" });
      stopping = false;
      return;
    }
  }
  active = null;
  persist();
  set({ phase: "done", seconds: duration, pendingUploads: 0 });
  window.dispatchEvent(new CustomEvent("yorik:recorder:finished", { detail: { id } }));
  window.setTimeout(() => { if (state.phase === "done") set({ phase: "idle", recordingId: null, title: "" }); }, HIDE_AFTER_MS);
  stopping = false;
}

/** Discard: stop the mic and forget; the row stays 'recording' until the retention sweep fails it. */
export function cancelRecording() {
  teardown();
  active = null;
  queue.length = 0;
  persist();
  set({ phase: "idle", recordingId: null, title: "", seconds: 0, pendingUploads: 0, error: null, needsResume: false });
}

/** After a reload: continue the recording that was running, with a fresh MediaRecorder. */
export async function resumeAfterReload(): Promise<boolean> {
  let saved: Active | null = null;
  try { saved = JSON.parse(sessionStorage.getItem(RESUME_KEY) || "null"); } catch {}
  if (!saved || !saved.id) return false;
  active = { ...saved, pausedAt: null };
  set({ phase: "starting", recordingId: saved.id, title: saved.title, seconds: elapsedSeconds(), needsResume: false, error: null });
  try {
    recorder = await openMic();
    recorder.start(CHUNK_MS);
    await requestWakeLock();
    startTicking();
    set({ phase: "recording" });
    return true;
  } catch {
    teardown();
    set({ phase: "paused", needsResume: true, error: "tap Resume to continue recording" });
    return false;
  }
}

function fmt(s: number): string {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : String(m)) + ":" + String(sec).padStart(2, "0");
}

export function RecorderDock() {
  const [s, setS] = useState<RecorderState>(state);

  useEffect(() => subscribeRecorder(setS), []);

  // ui_actions from chat/voice: start_recording carries the row the
  // skill created (plus its token); stop_recording ends it.
  useEffect(() => {
    function handle(a: any) {
      if (!a || typeof a !== "object") return;
      if (a.type === "start_recording" && a.recording_id) {
        void startRecording({ id: Number(a.recording_id), title: a.title, upload_token: a.upload_token || null }).catch(() => {});
      } else if (a.type === "stop_recording") {
        void stopRecording();
      }
    }
    function onEvt(e: Event) { handle((e as CustomEvent).detail); }
    window.addEventListener("yorik-ui-action", onEvt);
    for (const a of drainUiActions(["start_recording", "stop_recording"])) handle(a);
    return () => window.removeEventListener("yorik-ui-action", onEvt);
  }, []);

  // a reload mid-recording: try to carry on
  useEffect(() => {
    if (state.phase === "idle") void resumeAfterReload();
    const onVisible = () => { if (document.visibilityState === "visible" && (state.phase === "recording")) void requestWakeLock(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);

  if (s.phase === "idle") return null;
  const live = s.phase === "recording";
  return (
    <div className="fixed left-1/2 -translate-x-1/2 bottom-20 sm:bottom-6 z-[70] pointer-events-auto"
         onPointerDown={e => e.stopPropagation()} onPointerUp={e => e.stopPropagation()}>
      <div className={cn("flex items-center gap-3 rounded-full px-4 py-2 shadow-lg backdrop-blur-md border text-sm",
                         "bg-black/80 text-white border-white/15")}>
        {s.phase === "finishing" || s.phase === "starting"
          ? <Loader2 className="w-4 h-4 animate-spin text-white/80" />
          : s.phase === "done"
            ? <Circle className="w-3 h-3 fill-emerald-400 text-emerald-400" />
            : <Circle className={cn("w-3 h-3", live ? "fill-red-500 text-red-500 animate-pulse" : "fill-amber-400 text-amber-400")} />}
        <div className="min-w-0">
          <div className="font-medium truncate max-w-[12rem]">{s.title || "Recording"}</div>
          <div className="text-[11px] text-white/70">
            {s.phase === "starting" && "starting…"}
            {(live || s.phase === "paused") && (<>{fmt(s.seconds)}{s.phase === "paused" && " · paused"}{s.pendingUploads > 0 && ` · uploading ${s.pendingUploads}`}</>)}
            {s.phase === "finishing" && "uploading the last piece…"}
            {s.phase === "done" && `${fmt(s.seconds)} · transcript is being written; everybody gets a notification`}
            {s.phase === "error" && <span className="text-red-300">{s.error}</span>}
          </div>
        </div>
        {(live || s.phase === "paused") && (
          <>
            {s.needsResume
              ? <button onClick={() => { void resumeAfterReload(); }} className="p-2 rounded-full hover:bg-white/10" title="Resume"><Play className="w-4 h-4" /></button>
              : live
                ? <button onClick={pauseRecording} className="p-2 rounded-full hover:bg-white/10" title="Pause"><Pause className="w-4 h-4" /></button>
                : <button onClick={resumeRecording} className="p-2 rounded-full hover:bg-white/10" title="Resume"><Play className="w-4 h-4" /></button>}
            <button onClick={() => { void stopRecording(); }} className="flex items-center gap-1 rounded-full bg-red-500 hover:bg-red-400 text-white px-3 py-1.5 text-xs font-semibold" title="Stop and write the transcript">
              <Square className="w-3 h-3 fill-current" /> Stop
            </button>
          </>
        )}
        {s.phase === "error" && (
          <>
            {s.recordingId && <button onClick={() => { void stopRecording(); }} className="rounded-full bg-white/10 hover:bg-white/20 px-3 py-1.5 text-xs">Retry</button>}
            <button onClick={cancelRecording} className="p-2 rounded-full hover:bg-white/10" title="Dismiss"><X className="w-4 h-4" /></button>
          </>
        )}
        {s.phase === "done" && (
          <button onClick={() => set({ phase: "idle", recordingId: null, title: "" })} className="p-2 rounded-full hover:bg-white/10" title="Dismiss"><X className="w-4 h-4" /></button>
        )}
      </div>
    </div>
  );
}
