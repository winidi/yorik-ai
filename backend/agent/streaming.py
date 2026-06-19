"""Sentence-boundary streaming + event types for the agent loop.

Two pieces:

1. **Stream event types** — ``TextDelta``, ``ToolCallStart``, ``ToolCallEnd``,
   ``FinalResult`` — what :func:`backend.agent.loop.ask_stream` yields
   so a caller (the voice TTS pipeline, a chat-page SSE endpoint, etc.)
   can drive real UI without reaching into OpenAI SDK internals.

2. **Sentence chunker** — buffers text deltas and yields complete
   sentences on punctuation boundaries (``.``, ``?``, ``!``, German
   ``...``). Voice TTS calls feed each sentence to the synthesizer
   independently so the user hears the first sentence ~1s after the
   model starts generating instead of waiting for the full response.

The standard accumulator helper :func:`consume_stream` turns the raw
LlmClient.chat_stream generator into our event types — so loop code
never sees openai SDK ChatCompletionChunk objects.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Iterator, List, Optional

from .messages import repair_tool_call_arguments

logger = logging.getLogger("yorik.agent.streaming")


# ---------------------------------------------------------------------------
# Event types yielded by ask_stream() (and by consume_stream() at LLM level)
# ---------------------------------------------------------------------------


@dataclass
class TextDelta:
    """An incremental chunk of assistant text."""
    text: str


@dataclass
class SentenceComplete:
    """A complete sentence ready to ship to TTS."""
    text: str
    index: int  # 0-based position in the response


@dataclass
class ToolCallStart:
    """An assistant tool-call is being constructed."""
    id: str
    name: str


@dataclass
class ToolCallReady:
    """All arguments accumulated; the loop can dispatch the call now."""
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ToolResultEvent:
    """A tool's execute() result is back."""
    id: str
    name: str
    result_for_llm: str
    ui_actions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class IterationStart:
    """A new LLM turn begins."""
    n: int


@dataclass
class FinalResult:
    """Loop is done. Contains the same dict that ask() returns."""
    response: Dict[str, Any]


# ---------------------------------------------------------------------------
# OpenAI chunk → typed-event accumulator
# ---------------------------------------------------------------------------


def consume_stream(
    chunks: Iterator[Any],
) -> Generator[Any, None, Dict[str, Any]]:
    """Turn an openai ChatCompletionChunk iterator into typed events.

    Yields ``TextDelta``, ``ToolCallStart``, ``ToolCallReady`` as the
    stream progresses. At the end (when the iterator finishes) returns
    a fully-assembled assistant message dict ready to append to the
    conversation::

        {"role": "assistant", "content": "...", "tool_calls": [...]}

    Use as::

        gen = consume_stream(llm.chat_stream(messages, tools))
        try:
            while True:
                event = next(gen)
                # ... handle event ...
        except StopIteration as stop:
            assistant_msg = stop.value

    The ``arguments`` on each tool_call has already been run through
    :func:`repair_tool_call_arguments` so callers can ``json.loads`` it
    safely.
    """
    content_parts: List[str] = []
    # tool_calls accumulator: {index → {id, type, function: {name, arguments}}}
    tc_acc: Dict[int, Dict[str, Any]] = {}
    started_tool_indices: set = set()

    finish_reason: Optional[str] = None

    for chunk in chunks:
        if not getattr(chunk, "choices", None):
            continue
        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            # final chunk often has empty delta + finish_reason set
            finish_reason = getattr(choice, "finish_reason", finish_reason)
            continue

        # Text delta
        text = getattr(delta, "content", None)
        if text:
            content_parts.append(text)
            yield TextDelta(text=text)

        # Tool-call delta — partial id/name/arguments per index
        delta_tc = getattr(delta, "tool_calls", None) or []
        for tc in delta_tc:
            idx = getattr(tc, "index", 0)
            slot = tc_acc.setdefault(idx, {
                "id":   "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            tc_id = getattr(tc, "id", None)
            if tc_id:
                slot["id"] = tc_id
            fn = getattr(tc, "function", None)
            if fn is not None:
                fn_name = getattr(fn, "name", None)
                if fn_name:
                    slot["function"]["name"] = fn_name
                fn_args = getattr(fn, "arguments", None)
                if fn_args:
                    slot["function"]["arguments"] += fn_args
            # First time we see this tool_call → emit a ToolCallStart
            if idx not in started_tool_indices and slot["function"]["name"]:
                started_tool_indices.add(idx)
                yield ToolCallStart(id=slot["id"] or f"call_{idx}",
                                    name=slot["function"]["name"])

        # Track finish_reason as it appears
        fr = getattr(choice, "finish_reason", None)
        if fr:
            finish_reason = fr

    # Stream done. Emit ToolCallReady for each accumulated tool call
    # (with repaired JSON arguments) and build the assistant message.
    normalised_calls: List[Dict[str, Any]] = []
    for idx in sorted(tc_acc):
        slot = tc_acc[idx]
        raw_args = slot["function"]["arguments"] or "{}"
        repaired = repair_tool_call_arguments(raw_args, tool_name=slot["function"]["name"] or "?")
        normalised_calls.append({
            "id":   slot["id"] or f"call_{idx}",
            "type": slot["type"] or "function",
            "function": {
                "name":      slot["function"]["name"],
                "arguments": repaired,
            },
        })
        # Yield a typed ReadyEvent so the loop can dispatch.
        import json as _json
        try:
            parsed_args = _json.loads(repaired) if repaired else {}
            if not isinstance(parsed_args, dict):
                parsed_args = {}
        except _json.JSONDecodeError:
            parsed_args = {}
        yield ToolCallReady(
            id=slot["id"] or f"call_{idx}",
            name=slot["function"]["name"],
            arguments=parsed_args,
        )

    final_msg: Dict[str, Any] = {
        "role":          "assistant",
        "content":       "".join(content_parts) or None,
        "_finish_reason": finish_reason,
    }
    if normalised_calls:
        final_msg["tool_calls"] = normalised_calls
    return final_msg


# ---------------------------------------------------------------------------
# Sentence chunker — buffers TextDeltas, yields complete sentences
# ---------------------------------------------------------------------------

# Boundary characters that end a sentence. Includes German + English.
# Excludes "...\n" because ellipsis-then-newline is a list item marker
# more often than a sentence boundary in chat replies.
_SENTENCE_END_RE = re.compile(r'([.!?](?:\s+|$)|[!?。!？]+(?:\s+|$))', re.UNICODE)


class SentenceChunker:
    """Stateful chunker — feed text deltas, get complete sentences out.

    Voice TTS pipeline calls::

        chunker = SentenceChunker(min_chars=20)
        async for ev in ask_stream(...):
            if isinstance(ev, TextDelta):
                for sent in chunker.feed(ev.text):
                    yield sent     # ship to TTS immediately
        for sent in chunker.flush():
            yield sent             # ship the trailing fragment

    ``min_chars`` prevents emitting a "Hi." as a sentence (TTS overhead
    > the actual audio). Below that threshold the sentence is held in
    the buffer until either more text arrives or flush() is called.
    """

    def __init__(self, min_chars: int = 20) -> None:
        self.min_chars = min_chars
        self._buf: str = ""
        self._emitted = 0

    def feed(self, text: str) -> List[str]:
        """Append text. Returns a list of complete sentences ready to emit.

        Short sentences are accumulated across boundaries so a chain like
        "Hi. Bye." doesn't ship two two-character chunks to TTS — they
        merge into "Hi. Bye." and that ships if min_chars is satisfied,
        otherwise they wait for more text on the next feed() call.
        """
        if not text:
            return []
        self._buf += text
        out: List[str] = []
        pending = ""
        while True:
            m = _SENTENCE_END_RE.search(self._buf)
            if not m:
                break
            end = m.end()
            chunk = self._buf[:end].strip()
            self._buf = self._buf[end:]
            candidate = (pending + " " + chunk).strip() if pending else chunk
            if len(candidate) >= self.min_chars:
                out.append(candidate)
                self._emitted += 1
                pending = ""
            else:
                # Hold this short sentence — merge with the next one.
                pending = candidate
        # Anything still under-threshold goes back to the buffer to
        # merge with the next feed() call.
        if pending:
            self._buf = pending + ((" " + self._buf) if self._buf else "")
        return out

    def flush(self) -> List[str]:
        """Emit whatever is left in the buffer as the trailing sentence."""
        if not self._buf.strip():
            return []
        last = self._buf.strip()
        self._buf = ""
        self._emitted += 1
        return [last]

    @property
    def emitted_count(self) -> int:
        return self._emitted


__all__ = [
    "TextDelta",
    "SentenceComplete",
    "ToolCallStart",
    "ToolCallReady",
    "ToolResultEvent",
    "IterationStart",
    "FinalResult",
    "consume_stream",
    "SentenceChunker",
]
