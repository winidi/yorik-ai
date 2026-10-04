---
title: Voice — speaking to Yorik
nav_app: settings
nav_query:
  tab: signin
summary: Voice input transcribed on your own machine (Parakeet, or Whisper) — dictation in the chat composer, your voice on the family wall (Settings › Sign-in & devices), speech engines in Settings › AI, spoken replies.
---

# Voice — speaking to Yorik

Yorik turns speech into text on your own machine (Parakeet by default, Whisper where Parakeet doesn't run). Two voice paths:

1. **Voice button anywhere** (the global FAB at the bottom-right): records → transcribes → sends to chat → executes. Fastest "speak to act" flow.
2. **Mic icon in the chat composer**: records → transcribes → drops text into the input field. You review + edit before sending. "Speak to dictate."

The two paths are different on purpose. Don't conflate them.

## Your voice on the family wall

**Settings › Sign-in & devices › Your voice on the family wall**. Each person can record a short voice sample (ten seconds of speaking). Only the family wall tablet listens for voices: when you talk to it, it knows it's you without your PIN. Phones and computers never try to recognise who is speaking. Skip this if you have no wall tablet.

The same sample also helps Recordings put names on the speakers (see `recordings`).

Enrollment is optional. Without it, voice still works — the wall just asks for your PIN.

## Choosing the speech engine

An admin picks the engine under **Settings › AI › Speech recognition**:

- **Parakeet, on this computer** *(recommended)* — runs on this computer in well under a second, no graphics card or internet needed. A fresh install downloads it.
- **Whisper, on this computer** — older and slower; kept for computers where Parakeet doesn't run. When Whisper is the engine, the same card lets you pick its model size (bigger is more accurate, slower and needs more RAM).
- **Groq (cloud)** — very fast and good with German. The audio is sent to Groq; you need a key from <https://console.groq.com/keys>.
- **Another cloud service** — OpenAI, ElevenLabs or your own server, anything that speaks OpenAI's `/v1/audio/transcriptions` shape. You provide the address, model name and key.

If a cloud engine is unreachable, returns an error or times out, Yorik transcribes that one request on this computer instead. A network blip never blocks a voice query.

Audio uploaded to a cloud engine is processed by the provider under their terms; Yorik does not retain a copy beyond the in-flight request. Switch back to an engine on this computer and no future audio leaves the device.

Recordings (dinners and meetings) always use Parakeet on this computer, whatever engine is picked here.

**Instant confirmation**: under **Settings › Assistant & alerts › Voice** each person can switch on a short spoken "on it" that Yorik says the moment it hears you, before the answer.

## Spoken replies

When you talk through the voice button, Yorik answers out loud as well as on screen. The voice is generated on this computer; the model downloads on first use.

## Troubleshooting

- **First voice turn is slow**: the speech model loads on first use (a few seconds, longer for Whisper). It stays warm after.
- **No transcript / empty result**: too short. Yorik requires at least ~1 second of audio.
- **Wrong language**: the language is detected from the audio; there is no setting to force one. If it keeps guessing wrong, an admin can try another engine under Settings › AI › Speech recognition.
