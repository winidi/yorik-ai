---
title: Connecting / changing the LLM
nav_app: settings
nav_query:
  tab: llm
summary: How to point Yorik at your LLM — local (Ollama, llama-swap, LM Studio, vLLM) or cloud (OpenAI, Anthropic via litellm). Detect button, API key field, restart-free swap.
---

# Connecting / changing the LLM

Yorik talks to any OpenAI-compatible chat-completions endpoint. Local runs the most natural — your prompts never leave the machine — but cloud endpoints work too if you have an API key.

## The fast path — local LLM auto-detect

Settings → **LLM**. Click **Scan now**.

Yorik probes five common ports in parallel (~1 second total):

- `:11434` Ollama
- `:8080` llama-swap
- `:1234` LM Studio
- `:8001` vLLM
- `:8081` llama.cpp server

Reachable endpoints show with the model list. Click the model you want → done. Change takes effect on the next chat turn, no restart needed.

## Manual entry

Don't have a local LLM yet? Fastest path is Ollama:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen3.5-9b
```

Then in **Settings → LLM**:

1. **Endpoint URL**: `http://127.0.0.1:11434/v1`
2. Click **Test** — green check means it's reachable.
3. **Model**: pick from the dropdown (populated by the test).
4. **Save + apply**.

Yorik picks up on the next chat turn.

## Cloud LLM (OpenAI / Anthropic via litellm / others)

Same flow, plus the **API key** field:

1. **Endpoint URL**: your provider's OpenAI-compatible base URL (ending in `/v1`).
2. **API key**: paste it in. Stored encrypted with Fernet (`data/.credential_key`, mode 0600) — never in logs, never in `config.env`.
3. **Test** sends a Bearer-authenticated probe to `/models`.
4. Pick a model from the dropdown.
5. **Save + apply**.

The key persists across restarts. To clear it: click **Clear** next to the input.

**Privacy heads-up**: when you point Yorik at a cloud endpoint, every chat prompt + the user's data Yorik sends as context (events, contacts, documents) leaves your machine for the model provider. The local-first promise pauses for cloud LLM use. The Settings panel warns you about this explicitly.

## Model choice — quick guide

| Model size | Good for | Latency on CPU | Latency on GPU |
|---|---|---|---|
| 3B (Llama 3.2 3B, Qwen 2.5 3B) | Quick replies, basic tasks | 2–5s | <1s |
| 7–9B (Qwen 3.5 9B recommended) | Daily driver, tool use | 5–10s | 1–3s |
| 13–32B (Mixtral 8x7B, Qwen 32B) | Best quality | 30s+ | 5–10s |

Yorik is tuned for Qwen 3.5 9B (and the MTP variant). Other tool-calling chat models work; very small (<3B) models tend to invent tool arguments.

## What `Detect` doesn't find

- Endpoints on non-standard ports (you'll need manual entry)
- Cloud providers (no port to probe — Detect is local only)
- LM Studio in "server mode disabled" state (start the server first)
- Anything behind auth — Detect doesn't send the API key

## Changing models later

Settings → LLM → **Detect** or **Test** at any time. The change applies on the next chat turn. No restart.
