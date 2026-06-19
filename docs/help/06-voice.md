---
title: Voice — speaking to Yorik
nav_app: settings
nav_query:
  tab: voice
summary: Voice input via Whisper (offline) — voice profiles, dictation in chat composer, calibration. TTS output is v2.
---

# Voice — speaking to Yorik

Yorik runs Whisper locally for speech-to-text. Two voice paths:

1. **Voice button anywhere** (the global FAB at the bottom-right): records → transcribes → sends to chat → executes. Fastest "speak to act" flow.
2. **Mic icon in the chat composer**: records → transcribes → drops text into the input field. You review + edit before sending. "Speak to dictate."

The two paths are different on purpose. Don't conflate them.

## Voice profiles

Settings → Voice → **Profiles**. Each household member can enroll their voice (30 seconds of speaking). Yorik then identifies who's talking on each voice turn, so *"trag mir einen Zahnarzttermin ein"* goes into the right person's calendar.

Enrollment is optional. Without profiles, voice still works — Yorik just doesn't tell speakers apart.

## Whisper model choice

`HOMEOS_WHISPER_MODEL` in `config.env`. Defaults to `tiny` (~300ms transcription, decent for German + English). For higher accuracy: `base` (~800ms) or `small` (~2s). Larger models need more RAM.

## Local vs cloud transcription

Yorik defaults to running Whisper on your own machine — your audio never leaves the device. If you want faster, more accurate transcription and don't mind sending audio to a cloud provider, **Settings → LLM → Speech-to-text** lets you switch engines.

Three options:

- **Local Whisper** *(default)* — no API key, no network, audio stays on this machine.
- **Groq Cloud** — Whisper Large v3 Turbo on Groq's LPU hardware. ~10× faster than local Whisper, with noticeably better German. You'll need a free API key from <https://console.groq.com/keys>. Pricing as of 2026: roughly €1.20 / month for an hour of conversation per day.
- **OpenAI-compatible endpoint** — point at OpenAI's `whisper-1`, a self-hosted llama.cpp / CrispASR server, or any provider that speaks OpenAI's `/v1/audio/transcriptions` shape. You provide the URL, model name, and key.

Whichever engine you pick, Yorik automatically falls back to local Whisper if the cloud is unreachable, returns a 4xx/5xx, or times out. A network blip never blocks a voice query — it just gets transcribed locally for that one request.

Audio uploaded to a cloud engine is processed by the provider under their terms; Yorik does not retain a copy beyond the in-flight request. If you switch back to Local Whisper, no future audio leaves the device.

## TTS — read-aloud

**Not in alpha.** Voice OUTPUT (Yorik reading replies back to you) is on the roadmap for v0.2 via Piper. Today the voice loop is one-way: speech → text → response (text on screen).

## Troubleshooting

- **First voice turn is slow**: Whisper loads its model on first use (~5–15s). Stays warm after.
- **No transcript / empty result**: too short. Yorik requires at least ~1 second of audio.
- **Wrong language**: Whisper auto-detects. Settings → Voice → set a fallback language if detection is unreliable for your accent.
