# Yorik as the household's speech server

Yorik runs speech-to-text and text-to-speech on the CPU of the box it is
installed on. Since September 2026 it also serves both to other programs
over OpenAI's audio API shape, so every device in the house uses one
engine and one configuration, and laptops carry no model at all.

| Endpoint | Does | Engine |
|---|---|---|
| `POST /v1/audio/transcriptions` | audio file → text | Parakeet (sherpa-onnx), or whatever Settings → Speech-to-text says |
| `POST /v1/audio/speech` | text → audio | Supertonic 3 |

Auth is a personal API token (`Authorization: Bearer yk_…`, Settings →
You → API tokens) or a browser session. The `model` field is accepted and
ignored.

## Transcriptions

```
curl -s http://yorik.local:8000/v1/audio/transcriptions -H "Authorization: Bearer yk_…" -F file=@note.webm -F response_format=text
```

Multipart fields: `file` (any container ffmpeg reads: webm, ogg, m4a,
mp3, wav; up to 25 MB), optional `language`, `response_format` = `json`
(default, `{text, language}`), `text`, or `verbose_json`.

Parakeet does not detect the language. The German variant reports `de`,
the multilingual one the household default, unless the caller passes
`language`.

## Speech

```
curl -s http://yorik.local:8000/v1/audio/speech -H "Authorization: Bearer yk_…" -H "Content-Type: application/json" -d '{"input":"Guten Morgen","language":"de"}' -o out.wav
```

JSON fields: `input` (≤ 4096 chars), optional `voice` (`M1`…`M5`,
`F1`…`F5`; anything else, including OpenAI's `alloy`, means the
household's voice for that language), optional `language`, and
`response_format` = `wav` (default), `pcm` (24 kHz, 16-bit, mono,
headerless, what OpenAI clients expect), `mp3`, `opus`, `flac`.

## Clients

**Dictate** (desktop dictation): Settings → Provider → *Yorik (home
server)*, paste the Yorik URL and a token. The local Parakeet stays
available as an offline fallback.

**Hermes**: two command providers in `~/.hermes/config.yaml`, using the
helper scripts in `~/.hermes/bin/`:

```yaml
stt:
  provider: yorik
  providers:
    yorik:
      type: command
      command: "~/.hermes/bin/yorik-stt {input_path} {output_path} {format}"
tts:
  provider: yorik
  providers:
    yorik:
      type: command
      format: wav
      command: "~/.hermes/bin/yorik-tts {text_path} {output_path} {format} {voice}"
      voice: M1
```

The scripts read `YORIK_URL` and `YORIK_TOKEN` from `~/.hermes/yorik.env`.

**Anything else** that has an "OpenAI-compatible" audio setting: base URL
`http://yorik.local:8000/v1`, API key = the Yorik token.

## Engines and models

- STT: `backend/stt_parakeet.py`, models in `data/stt/` (~600 MB, one
  per variant), 4 CPU threads, ~100 ms per utterance.
- TTS: `backend/tts.py`, Supertonic 3 in `data/voices/`, 44.1 kHz.
- Speaker identification (who is talking at the kiosk): WeSpeaker CAM++
  through sherpa-onnx, `data/speaker_model/`.
- Semantic search embeddings: MiniLM ONNX, `data/embed/`.

None of these need a GPU or torch. Whisper is an optional extra
(`pip install openai-whisper` in the venv) and then selectable again in
Settings.
