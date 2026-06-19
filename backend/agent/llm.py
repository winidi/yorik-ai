"""OpenAI-compatible LLM client wrapper.

This is the only file in ``backend/agent/`` that knows about the openai
SDK. Everything else operates on plain dicts. Reasons:

- If we add a second provider (Claude, local Mistral with a different
  tool-call dialect), it's a new sibling file, not a rewrite.
- Test fakes can plug in without importing openai.
- Lazy SDK import keeps cold-start fast (the SDK pulls ~240ms on first
  `import openai`).

The wrapper:

1. **Forces Qwen ``enable_thinking: false``** on every request. Without
   this, Qwen3 burns its token budget on reasoning tokens and the tool-
   call loop stalls. Ported from QwenLlmService in vanna_agent.py.
2. **Surrogate-scrubs messages before send** (lone surrogates from
   clipboard paste crash the SDK's UTF-8 encode).
3. **Retries transient failures with jittered backoff** (rate limits,
   connection drops, brief 5xx).
4. **Returns a normalised assistant message dict**, not a Pydantic
   response object. The loop never sees SDK types.

What's deferred (per masterplan):
- Streaming (Phase 7) — hook is in place, currently no-op.
- Anthropic / Codex Responses adapters — sibling files when needed.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from .messages import normalize_assistant_response, prepare_for_send
from .retry import jittered_backoff

logger = logging.getLogger("yorik.agent.llm")


# Errors we'll back off and retry on. We swallow imports so the wrapper
# stays importable in tests that mock the SDK out entirely.
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 524}


# ───────────────────────── OpenRouter routing fix ──────────────────────
# OpenRouter is an aggregator: one model name (e.g. `qwen/qwen3.5-9b`)
# is served by several downstream providers (SiliconFlow, DeepInfra,
# Venice, Together, …) whose tool-calling implementations vary widely.
# Verified 2026-06-14 by hitting `qwen/qwen3.5-9b` against each one with
# the same Yorik-shaped prompt + tools array:
#
#   SiliconFlow  → clean OpenAI tool_calls = [invoke_skill(...)]
#   Venice       → clean OpenAI tool_calls = [invoke_skill(...)]
#   DeepInfra    → (rate-limited mid-probe, but bf16/serious vendor)
#   Together     → text content `<function=search_documents>…</function>`
#                  with EMPTY tool_calls array — their chat template
#                  emits XML-style function syntax that OpenRouter
#                  passes through without normalising into the OpenAI
#                  tools shape. From Yorik's loop perspective the model
#                  "didn't call anything," and chat falls flat.
#
# Hermes works on OpenRouter because it attaches `extra_body["provider"]`
# on every request and steers routing to downstreams that honour the
# parameters it's passing. Yorik historically sent none of that, so
# OpenRouter picked whichever provider was cheapest/least-loaded.
#
# Fix: inject `provider.require_parameters: true` + `allow_fallbacks:
# false` whenever the base_url targets OpenRouter. Effect:
#   1. OpenRouter only routes to downstreams that advertise the
#      parameters we send (we send `tools`, so tools-claiming providers
#      only).
#   2. If the chosen provider fails / is unavailable, error out instead
#      of silently degrading to a less-capable one.
#
# Local llama.cpp / vLLM / Ollama / Anthropic don't read
# `extra_body.provider` at all — it's a key in the HTTP body that
# OpenRouter parses and everyone else ignores. So this change is a
# pure no-op for every non-OpenRouter base_url.
_OPENROUTER_PROVIDER_PREFS = {
    "require_parameters": True,
    "allow_fallbacks":    False,
}


def _is_openrouter_base_url(base_url: Optional[str]) -> bool:
    """True if base_url targets the OpenRouter aggregator. Empty / None
    → False (local servers are the default)."""
    return "openrouter.ai" in (base_url or "").lower()


class LlmClient:
    """Thin async-friendly wrapper around openai.OpenAI for Yorik.

    Construction is cheap (no network); the openai SDK is imported
    lazily on first chat call.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str = "not-used",
        max_retries: int = 3,
        request_timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.max_retries = max_retries
        self.request_timeout = request_timeout
        self._sdk_client: Any = None  # lazily built

    # ------------------------------------------------------------------
    # Lazy SDK client
    # ------------------------------------------------------------------

    def _ensure_client(self) -> Any:
        if self._sdk_client is not None:
            return self._sdk_client
        # Import here (lazy) — the openai SDK is ~240ms of imports.
        from openai import OpenAI  # type: ignore

        self._sdk_client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.request_timeout,
        )
        return self._sdk_client

    # ------------------------------------------------------------------
    # The one entry point the loop uses
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tool_choice: Optional[Any] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run one chat completion. Returns a normalised assistant message dict
        ({role, content, tool_calls?}) plus a ``usage`` field when available.

        Mutates ``messages`` in place to scrub surrogates (cheap, one pass).
        """
        prepare_for_send(messages)  # surrogate scrub

        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        # Qwen3 reasoning OFF — see masterplan + QwenLlmService origin.
        # Two parallel mechanisms because the upstream depends on it:
        #   * `chat_template_kwargs.enable_thinking: false` — what llama.cpp
        #     + vLLM look at when rendering Qwen's chat template.
        #   * `reasoning_effort: "none"` — what Ollama's OpenAI-compatible
        #     /v1/chat/completions endpoint actually honors. Ollama
        #     IGNORES the native `think` field on the compat endpoint,
        #     so without this, the R7 fine-tune produces 25-46% empty
        #     answers on longer prompts. No-op on other backends.
        # extra_body merges with whatever the caller passed; idempotent.
        existing_extra = (extra or {}).get("extra_body") or {}
        extra_body = {
            **existing_extra,
            "chat_template_kwargs": {
                **(existing_extra.get("chat_template_kwargs") or {}),
                "enable_thinking": False,
            },
            "reasoning_effort": existing_extra.get("reasoning_effort", "none"),
        }
        # OpenRouter-only: steer routing to tool-capable downstreams.
        # No-op for every other base_url. setdefault preserves any
        # caller-supplied provider override (tests, advanced configs).
        if _is_openrouter_base_url(self.base_url):
            extra_body.setdefault("provider", dict(_OPENROUTER_PROVIDER_PREFS))
        payload["extra_body"] = extra_body
        # Carry through any other extras (e.g. metadata, user, etc.)
        for k, v in (extra or {}).items():
            if k != "extra_body":
                payload[k] = v

        client = self._ensure_client()
        last_exc: Optional[BaseException] = None
        call_started = time.monotonic()
        for attempt in range(1, self.max_retries + 2):  # max_retries=3 → 4 attempts
            try:
                resp = client.chat.completions.create(**payload, stream=False)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not _is_retryable(exc) or attempt > self.max_retries:
                    dur = int((time.monotonic() - call_started) * 1000)
                    logger.warning(
                        "LLM call failed (attempt %d/%d, not retrying, %dms): %s",
                        attempt, self.max_retries + 1, dur, _exc_summary(exc),
                        extra={"upstream": "llm", "model": payload.get("model"),
                               "duration_ms": dur, "status": "error",
                               "attempts": attempt},
                    )
                    raise
                delay = jittered_backoff(attempt)
                logger.info(
                    "LLM call transient failure (attempt %d/%d) — backing off %.1fs: %s",
                    attempt, self.max_retries + 1, delay, _exc_summary(exc),
                    extra={"upstream": "llm", "model": payload.get("model"),
                           "status": "retry", "attempt": attempt},
                )
                time.sleep(delay)
        else:  # pragma: no cover — break/raise above always exits the loop
            raise last_exc  # type: ignore[misc]

        dur_ms = int((time.monotonic() - call_started) * 1000)
        usage = _extract_usage(resp)
        logger.info(
            "LLM call ok: model=%s (%dms, tokens=%s)",
            payload.get("model"), dur_ms,
            (usage or {}).get("total_tokens"),
            extra={"upstream": "llm", "model": payload.get("model"),
                   "duration_ms": dur_ms, "status": "ok",
                   "tokens_in": (usage or {}).get("prompt_tokens"),
                   "tokens_out": (usage or {}).get("completion_tokens")},
        )

        if not resp.choices:
            return {"role": "assistant", "content": "", "_usage": usage}

        choice = resp.choices[0]
        msg = normalize_assistant_response(choice.message)
        msg["_usage"] = usage
        msg["_finish_reason"] = getattr(choice, "finish_reason", None)
        return msg

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Streaming (Phase 7) — yields raw OpenAI deltas
    # ------------------------------------------------------------------

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tool_choice: Optional[Any] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        """Stream a chat completion. Generator yielding OpenAI chunk objects.

        The caller is responsible for accumulating deltas into a final
        message (use :func:`backend.agent.streaming.consume_stream` for
        the standard accumulation + sentence chunking).

        Same Qwen ``enable_thinking:false`` injection as ``chat()``; same
        surrogate scrubbing on outgoing messages.

        Yields:
            openai SDK ChatCompletionChunk objects. Each may carry:
              - ``choices[0].delta.content``: incremental text
              - ``choices[0].delta.tool_calls``: partial tool-call info
              - ``choices[0].finish_reason``: terminal reason (stop, tool_calls, ...)
        """
        prepare_for_send(messages)

        payload: Dict[str, Any] = {
            "model":    model or self.model,
            "messages": messages,
            "stream":   True,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        existing_extra = (extra or {}).get("extra_body") or {}
        extra_body = {
            **existing_extra,
            "chat_template_kwargs": {
                **(existing_extra.get("chat_template_kwargs") or {}),
                "enable_thinking": False,
            },
            # Mirror chat() — also disable Ollama's thinking on the
            # OpenAI-compat endpoint. See chat() comment for why.
            "reasoning_effort": existing_extra.get("reasoning_effort", "none"),
        }
        # Mirror chat(): OpenRouter-only provider preferences.
        if _is_openrouter_base_url(self.base_url):
            extra_body.setdefault("provider", dict(_OPENROUTER_PROVIDER_PREFS))
        payload["extra_body"] = extra_body
        for k, v in (extra or {}).items():
            if k != "extra_body":
                payload[k] = v

        client = self._ensure_client()
        # Stream creation can transient-fail too — single attempt for now;
        # if we need retries on stream init, wrap in the same backoff
        # pattern as chat().
        stream = client.chat.completions.create(**payload)
        for chunk in stream:
            yield chunk

    def close(self) -> None:
        """Close the underlying SDK client if it was built."""
        if self._sdk_client is None:
            return
        try:
            close = getattr(self._sdk_client, "close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001
            pass
        self._sdk_client = None

    def rebuild(self, *, base_url: Optional[str] = None, model: Optional[str] = None) -> None:
        """Swap base_url / model at runtime — e.g. Settings → LLM picker.

        Drops the cached SDK client so the next chat() picks up the new
        endpoint without a process restart. Idempotent.
        """
        if base_url is not None:
            self.base_url = base_url
        if model is not None:
            self.model = model
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_retryable(exc: BaseException) -> bool:
    """Return True for transient errors worth a backoff retry."""
    # openai SDK exception hierarchy: APIConnectionError, APITimeoutError,
    # RateLimitError, InternalServerError — all transient. We detect by
    # class name to avoid hard-importing the SDK exception module (which
    # changes shape across versions).
    name = type(exc).__name__
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError",
                "InternalServerError", "APIError"}:
        return True
    # Status-code based fallback for less-typed exceptions
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and status in _RETRYABLE_STATUS:
        return True
    return False


def _exc_summary(exc: BaseException) -> str:
    """Short one-line summary of an exception for logs."""
    msg = str(exc)
    if len(msg) > 200:
        msg = msg[:200] + "…"
    return f"{type(exc).__name__}: {msg}"


def _extract_usage(resp: Any) -> Optional[Dict[str, int]]:
    """Pull prompt/completion/total tokens out of an openai response."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    out: Dict[str, int] = {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = getattr(usage, k, None)
        if v is not None:
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                pass
    return out or None


__all__ = ["LlmClient"]
