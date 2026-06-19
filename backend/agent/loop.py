"""The agent loop — one /api/ask turn end-to-end.

Async entry point :func:`ask` does:

1. **Cache lookup.** If the user's phrase has been seen 3+ times and the
   cached SQL is a pure SELECT, replay the cached answer without calling
   the LLM. ~50ms instead of ~2s.

2. **Reset per-turn audit state.** Zero out the ContextVars that the
   tool dispatch will write (last SQL, mutation-skill flag, delete
   counter).

3. **Build the message list.** System prompt + prior turns from the
   ``agent_conversations`` table + the current user message.

4. **IterationBudget loop** (default cap 8). On each iteration:
     - Sanitize → ``llm.chat(...)`` with the registered tools.
     - If the assistant emitted ``content`` and no ``tool_calls``: done.
     - If ``tool_calls``: dispatch each via the registry, append the
       tool message, continue.
     - If both content and tool_calls: stash the interim content as a
       "thinking" line, dispatch tools, continue.

5. **Persist the updated conversation.** Append user + assistant +
   tool messages to ``agent_conversations``.

6. **Cache-save** the result, gated on ``mutation_skill_invoked()`` so
   we never freeze a "Hab den Termin verschoben" reply.

7. **Return a dict** matching the shape that ``vanna_agent.ask_async``
   returns — drop-in compatible with every /api/ask consumer.

What this loop deliberately doesn't do (yet — see masterplan phases):
- Streaming token output (Phase 7).
- Subagent delegation (Phase 8).
- Anthropic prompt caching (the function exists in ``prompt_caching.py``
  but is wired in only when an Anthropic provider is added).
- Tool-loop guardrails beyond the per-skill ContextVar throttles
  (Phase 2 ports Hermes's signature-based duplicate-call detection).
"""

from __future__ import annotations

import asyncio
import time
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import audit, cache
from .budget import IterationBudget
from .context import RequestContext, ToolContext, User
from . import conversation_io
from .guardrails import (
    GuardrailConfig,
    GuardrailController,
    append_guidance,
    synthetic_tool_result,
)
from .llm import LlmClient
from .messages import (
    assistant_message,
    has_tool_calls,
    parse_tool_call_args,
    system_message,
    tool_message,
    user_message,
)
from .tools import ToolRegistry

log = logging.getLogger("yorik.agent.loop")


# Default budget — old vanna_agent loop capped at 12; bumped to 16 in
# 2026-05 after the 27B model regularly hit the cap on compose+confirm
# and "correct my answer" flows where each correction costs 2-3 extra
# tool calls. Raised to 28 in 2026-06 alongside skill_view pruning:
# with the pruning logic below, each iteration processes roughly the
# same-sized context (no stale manuals stacking up), so per-turn cost
# grows linearly with iterations rather than quadratically. That makes
# the previous 16 cap unnecessarily conservative — the new headroom lets
# the model read a manual before each first-time-this-turn invoke
# without exhausting the budget on multi-step Compose pipelines.
# Override via env.
MAX_ITERATIONS = int(os.getenv("YORIK_AGENT_MAX_ITERATIONS", "28"))


_LOCALE_LANG_NAME = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pl": "Polish", "nl": "Dutch", "pt": "Portuguese",
}


def _enrich_template_picked(message: str, user_language: str = "") -> str:
    """If `message` starts with `[template_picked id=X]`, append two
    template-scoped blocks so the next LLM turn carries everything it
    needs without a `view_compose_template` round trip:

      1. The template's `llm_hints` (per-template lore: how to fill the
         body, placeholder behavior, worked examples).
      2. A language-pin when the template's `locale` disagrees with the
         user's profile `language`. A German-profile user who picks
         `invoice-en` made an explicit cross-language choice — the
         draft and the surrounding chat should follow the *template's*
         language for that conversation, not the profile default.

    No-op for non-template-picked messages, unknown ids, or templates
    that carry neither `llm_hints` nor a divergent `locale`.

    Keeps template-specific guidance (and now language authority) next
    to the template definition instead of in skill.md, so adding a
    marketplace template doesn't require touching the skill prompt.
    """
    import re
    m = re.match(r"\[template_picked\s+id=([^\s\]]+)\]", message)
    if not m:
        return message
    tid = m.group(1).strip()
    try:
        from backend.compose import templates as _tpl
        t = _tpl.get(tid)
    except Exception:  # noqa: BLE001
        return message

    hints = (t.get("llm_hints") or "").strip()

    # Language pin: template `locale` may be `en`, `en-US`, `de-DE`, etc.
    # We compare on the 2-letter prefix. Profile language is also 2-letter.
    locale = (t.get("locale") or "").strip().lower()
    tmpl_lang = locale.split("-")[0] if locale else ""
    user_lang = (user_language or "").strip().lower().split("-")[0]
    lang_pin = ""
    if tmpl_lang and user_lang and tmpl_lang != user_lang:
        name = _LOCALE_LANG_NAME.get(tmpl_lang, tmpl_lang.upper())
        lang_pin = (
            f"- LANGUAGE OVERRIDE — RESPOND IN {name.upper()} ONLY. The user "
            f"picked a {name}-locale template (locale={locale}), which "
            f"explicitly overrides the user's profile language `{user_lang}`. "
            f"Every chat reply about this draft, every confirmation "
            f"question, every arg label you echo back — all in {name}. "
            f"Do NOT mix languages, do NOT fall back to {user_lang.upper()} "
            f"because the user's profile is set that way. The template's "
            f"locale is the authority while this draft is in flight."
        )

    if not hints and not lang_pin:
        return message

    block_lines = [f"# Template-specific guidance for {tid}:"]
    if hints:
        block_lines.append(hints)
    if lang_pin:
        block_lines.append(lang_pin)
    return message + "\n\n" + "\n".join(block_lines)


async def ask(
    message: str,
    *,
    user: User,
    registry: ToolRegistry,
    llm: LlmClient,
    system_prompt: str,
    conversation_id: Optional[str] = None,
    identified_name: Optional[str] = None,
    max_iterations: Optional[int] = None,
    include_trace: bool = False,
    force_first_tool_call: bool = False,
    progress_callback: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run one /api/ask turn.

    Args:
        message: The user's message text.
        user: User dataclass with id + role + language.
        registry: ToolRegistry pre-populated with the tools this user can call.
        llm: LlmClient instance (Qwen-tuned by default).
        system_prompt: The pre-rendered system prompt. The caller is
            responsible for assembling it (today's date, weekday table,
            language hint, etc.) — keeps the loop unaware of Yorik-specific
            prompt content.
        conversation_id: Thread id. Mints one if not supplied.
        identified_name: Voice-ID display name (for the response envelope).
        max_iterations: Override the default budget for this call.
        include_trace: If True, attach an ``agent_trace`` dict to the
            response with per-iteration timing + per-tool-call details.
            Used by the "Dev mode" toggle in Settings — adds ~1-3KB to
            the response payload, no LLM cost.

    Returns:
        Dict with the same keys ``vanna_agent.ask_async`` returns:
        ``response`` (str), ``sql_used`` (str | None), ``rows_preview``
        (list | None), ``ui_actions`` (list), ``conversation_id`` (str),
        ``from_cache`` (bool), ``components`` (list, debug), and on cache
        hits also ``cache_hits`` (int). Plus ``agent_trace`` when
        ``include_trace=True``.
    """
    role = (user.role or "admin").lower().strip()
    user_language = (user.language or "en").lower().strip()
    conversation_id = conversation_id or uuid.uuid4().hex
    turn_t0 = time.perf_counter() if include_trace else 0.0
    trace_iterations: List[Dict[str, Any]] = [] if include_trace else []

    # 2) Reset per-turn audit state ────────────────────────────────────
    audit.reset_turn()

    # 3) Build message list ────────────────────────────────────────────
    history = conversation_io.load_messages(conversation_id, role)
    # Per-conversation entity ledger — see entity_ledger.py. Concatenated
    # onto the main system prompt so the LLM resolves "the appointment
    # I just made" / "make it friendlier" against a compact, explicit
    # list of ids+labels instead of fishing through raw tool_results.
    # Sent as ONE system message (not two) — Qwen3.5's strict jinja
    # template raises "System message must be at the beginning" when
    # a second system message follows the first. The ledger has its
    # own clear "RECENT ENTITIES …" header so the model still sees a
    # self-contained section.
    from . import entity_ledger as _ledger_mod
    ledger = conversation_io.load_ledger(conversation_id, role)
    ledger_block = _ledger_mod.render_for_llm(ledger)
    sys_content = system_prompt
    if ledger_block:
        sys_content = system_prompt + "\n\n" + ledger_block
    messages: List[Dict[str, Any]] = [system_message(sys_content)]
    messages.extend(history)
    messages.append(user_message(_enrich_template_picked(message, user.language)))
    tools_schema = registry.schemas()

    # 4) Iteration-budget loop ─────────────────────────────────────────
    budget = IterationBudget(max_iterations or MAX_ITERATIONS)
    guardrails = GuardrailController(GuardrailConfig.from_env())
    request_ctx = RequestContext(
        user=user,
        conversation_id=conversation_id,
        language=user_language,
        identified_name=identified_name,
        message=message,
    )
    ui_actions: List[Dict[str, Any]] = []
    components_seen: List[str] = []  # debug trail
    interim_text_parts: List[str] = []  # "thinking" content interleaved with tool calls
    final_text: Optional[str] = None
    iteration = 0
    halted_by_guardrail = False

    # Best-effort progress emitter: if the caller wired a callback (the
    # /api/ask/stream SSE endpoint does), we push a tiny dict per event
    # (iter_start, tool_start, tool_done) so the UI can show real-time
    # status. Failures in the callback are swallowed — the agent loop
    # must keep going even if the client disconnected.
    async def _emit(phase: str, **fields: Any) -> None:
        if progress_callback is None:
            return
        try:
            evt = {"phase": phase, "iteration": iteration, **fields}
            import inspect as _ins
            if _ins.iscoroutinefunction(progress_callback):
                await progress_callback(evt)
            else:
                progress_callback(evt)
        except Exception:
            pass

    while budget.consume():
        iteration += 1
        await _emit("iter_start")
        iter_t0 = time.perf_counter() if include_trace else 0.0
        iter_tool_calls: List[Dict[str, Any]] = []
        # Sanitize is folded into llm.chat (prepare_for_send).
        # Run the LLM in a thread — the openai SDK call is sync; we don't
        # want to block the asyncio loop.
        llm_t0 = time.perf_counter() if include_trace else 0.0
        # When the caller marked this turn as "action-required" (e.g.
        # the Compose inline chat — the user just asked to MODIFY the
        # draft, the LLM should not narrate intent), force a tool call
        # on iteration 1. Subsequent iterations can choose freely
        # (ambiguity asks, clarifying questions etc. are legit there).
        force_tool = (
            force_first_tool_call
            and iteration == 1
            and bool(tools_schema)
        )
        assistant_msg = await asyncio.to_thread(
            llm.chat,
            conversation_io.sanitize_for_llm(messages),
            tools_schema,
            **({"tool_choice": "required"} if force_tool else {}),
        )
        # Belt + braces: some local LLMs (qwen3 especially under load)
        # ignore tool_choice="required" and still return content-only.
        # If iter 1 was supposed to call a tool but didn't, inject a
        # blunt synthetic-user reminder and retry ONCE. After that we
        # give up — the model genuinely doesn't know what to do.
        if force_tool and not has_tool_calls(assistant_msg):
            log.info(
                "force_first_tool_call: model returned content-only despite "
                "tool_choice=required — injecting retry nudge",
            )
            narrated = (assistant_msg.get("content") or "")[:200]
            messages.append({
                "role": "user",
                "content": (
                    "[system reminder] You replied with text but did not call "
                    "any tool. The user asked you to take an action; you must "
                    "call a tool now, not narrate. Your previous reply was: "
                    f"\"{narrated}\". Re-attempt — pick the right tool from "
                    "the available list and call it. If you genuinely need "
                    "more info from the user, ask a SPECIFIC question (not "
                    "\"I will find X\")."
                ),
            })
            assistant_msg = await asyncio.to_thread(
                llm.chat,
                conversation_io.sanitize_for_llm(messages),
                tools_schema, tool_choice="required",
            )
        llm_duration = (time.perf_counter() - llm_t0) if include_trace else 0.0
        components_seen.append("AssistantMessage")
        # Drop the usage/finish_reason side-channel before persisting.
        usage = assistant_msg.pop("_usage", None)
        finish = assistant_msg.pop("_finish_reason", None)
        log.debug(
            "iter %d: finish=%s tool_calls=%d usage=%s",
            iteration, finish, len(assistant_msg.get("tool_calls") or []), usage,
        )

        # Cloud-LLM defensive retry: some hosted backends occasionally
        # return finish_reason=stop with empty content and zero tool_calls
        # — the model generates a handful of EOS-ish tokens and gives up
        # before producing a user-visible reply. Verified 2026-06-14
        # against openrouter.ai routing qwen/qwen3.5-9b to SiliconFlow
        # in a multi-hit search_documents scenario; the failure is
        # non-deterministic (~1-in-N) and a single retry with the same
        # messages list resolves it most of the time. Local llama.cpp
        # serving Qwen has not been observed to hit this in extensive
        # testing — the retry path is effectively a cloud-only safety
        # net. force_first_tool_call has its own retry pattern (above);
        # this is the same shape for the empty-stop case after a
        # successful tool call.
        _is_empty_stop = (
            not has_tool_calls(assistant_msg)
            and not (assistant_msg.get("content") or "").strip()
        )
        if _is_empty_stop:
            log.info(
                "iter %d: empty response (finish=%s usage=%s) — retrying once",
                iteration, finish, usage,
            )
            try:
                retry_msg = await asyncio.to_thread(
                    llm.chat,
                    conversation_io.sanitize_for_llm(messages),
                    tools_schema,
                )
                retry_usage = retry_msg.pop("_usage", None)
                retry_finish = retry_msg.pop("_finish_reason", None)
                retry_has_calls = has_tool_calls(retry_msg)
                retry_has_text = bool((retry_msg.get("content") or "").strip())
                if retry_has_calls or retry_has_text:
                    log.info(
                        "iter %d: empty-response retry produced content "
                        "(finish=%s usage=%s)", iteration, retry_finish, retry_usage,
                    )
                    assistant_msg = retry_msg
                    usage = retry_usage
                    finish = retry_finish
                else:
                    log.warning(
                        "iter %d: empty-response retry also returned empty "
                        "(finish=%s) — falling through to empty final_text",
                        iteration, retry_finish,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "iter %d: empty-response retry failed: %s — keeping "
                    "original empty message", iteration,
                    f"{type(exc).__name__}: {exc}"[:200],
                )

        messages.append(assistant_msg)

        if not has_tool_calls(assistant_msg):
            # The model spoke its final answer.
            final_text = assistant_msg.get("content") or ""
            if include_trace:
                trace_iterations.append({
                    "n":            iteration,
                    "llm_s":        round(llm_duration, 3),
                    "duration_s":   round(time.perf_counter() - iter_t0, 3),
                    "tool_calls":   [],
                    "final":        True,
                    "content_len":  len(final_text or ""),
                    "usage":        usage,
                })
            break

        # Model wants to call tools. Stash interim content (some models
        # emit "Let me think..." before tool_calls) and dispatch each call.
        content = assistant_msg.get("content")
        if content:
            interim_text_parts.append(str(content))

        tool_ctx = ToolContext(
            request=request_ctx,
            iteration=iteration,
            conversation_so_far=list(messages),
        )
        for tc in assistant_msg["tool_calls"]:
            name = (tc.get("function") or {}).get("name") or ""
            args = parse_tool_call_args(tc)

            # Guardrail pre-check (hard-stop only — default OFF).
            pre = guardrails.before_call(name, args)
            if not pre.allows_execution:
                # Synthesize a tool-result row so the LLM sees the refusal.
                components_seen.append(f"GuardrailBlock:{name}")
                if include_trace:
                    iter_tool_calls.append({
                        "name":    name,
                        "args":    _truncate_args(args),
                        "blocked": True,
                        "result":  pre.message[:200] if hasattr(pre, "message") else "blocked",
                        "duration_s": 0.0,
                    })
                messages.append(
                    tool_message(tc["id"], name, synthetic_tool_result(pre))
                )
                if pre.action == "halt" or guardrails.halt_decision is not None:
                    halted_by_guardrail = True
                    break
                continue

            # Mandatory skill_view-before-invoke. The index in the
            # system prompt shows only `name — description` (no args);
            # the model literally can't construct correct args without
            # reading the manifest first. Reject the invoke synthetically
            # and prompt the model to read.
            if (
                name == "invoke_skill"
                and isinstance(args, dict)
                and isinstance(args.get("name"), str)
                and not _has_seen_skill_view_for(messages, args["name"])
            ):
                hint = _INVOKE_NEEDS_READ_HINT.format(name=args["name"])
                messages.append(tool_message(tc["id"], name, hint))
                if include_trace:
                    iter_tool_calls.append({
                        "name":   name,
                        "args":   _truncate_args(args),
                        "rejected_read_first": True,
                        "duration_s": 0.0,
                    })
                continue

            # Inline audit — captures SQL, mutation flag, delete counter.
            audit.record_tool_call(name, args)
            # Progress event BEFORE dispatch so the UI's status line
            # updates immediately. Args are truncated like the trace
            # rows — keeps the SSE payload small for network-bound
            # clients.
            await _emit("tool_start", tool=name,
                         args=_truncate_args(args) if isinstance(args, dict) else {})
            _tool_started = time.perf_counter()
            tool_t0 = _tool_started if include_trace else 0.0
            result = await registry.dispatch(name, args, tool_ctx)
            _tool_dt = round(time.perf_counter() - _tool_started, 3)
            tool_dt = (time.perf_counter() - tool_t0) if include_trace else 0.0
            await _emit("tool_done", tool=name, duration_s=_tool_dt)
            components_seen.append(f"ToolResult:{name}")
            if result.ui_actions:
                # Dedup by (type, primary_id) so a skill called twice in one
                # turn (e.g. compose_draft create-then-update with the same
                # existing_draft_id) doesn't render two cards.
                _ID_KEYS = ("draft_id", "event_id", "task_id", "contact_id",
                            "asset_id", "document_id")
                _seen = {(a.get("type"),
                          next((a[k] for k in _ID_KEYS if k in a), None))
                         for a in ui_actions}
                for a in result.ui_actions:
                    key = (a.get("type"),
                           next((a[k] for k in _ID_KEYS if k in a), None))
                    if key[1] is not None and key in _seen:
                        continue
                    _seen.add(key)
                    ui_actions.append(a)
                # Update the entity ledger so the NEXT turn's system
                # message shows the things this turn created/listed.
                _ledger_mod.absorb(ledger, result.ui_actions)

            # Guardrail post-check — may upgrade the result with a
            # warning suffix or set the halt flag for next iteration.
            post = guardrails.after_call(name, args, result.result_for_llm)
            llm_text = append_guidance(result.result_for_llm, post)
            messages.append(tool_message(tc["id"], name, llm_text))
            # Pruning: any invoke_skill call (success or failure) marks
            # the matching skill_view (read earlier in this turn) as
            # no longer load-bearing — its manual is replaced with a
            # short placeholder so downstream iterations don't carry
            # the prose. ToolResult intentionally has no success flag
            # (see tools.py:53); on a soft failure the error message
            # in result_for_llm is still in the assistant's view for
            # the next iteration, so retries don't need the manual.
            if (
                name == "invoke_skill"
                and isinstance(args, dict)
                and isinstance(args.get("name"), str)
            ):
                _prune_recent_skill_view(messages, args["name"])
            if include_trace:
                iter_tool_calls.append({
                    "name":       name,
                    "args":       _truncate_args(args),
                    "result":     _truncate_result(result.result_for_llm),
                    "ui_actions": [a.get("type") for a in (result.ui_actions or [])],
                    "duration_s": round(tool_dt, 3),
                })
            if post.should_halt:
                halted_by_guardrail = True
                break

        if include_trace:
            trace_iterations.append({
                "n":          iteration,
                "llm_s":      round(llm_duration, 3),
                "duration_s": round(time.perf_counter() - iter_t0, 3),
                "tool_calls": iter_tool_calls,
                "usage":      usage,
            })

        # If the guardrail set a halt during the tool-batch, break the
        # outer loop too — the next LLM turn would just re-attempt the
        # blocked path.
        if halted_by_guardrail:
            log.info(
                "loop halted by guardrail %s at iter %d",
                guardrails.halt_decision.code if guardrails.halt_decision else "?",
                iteration,
            )
            # Emit a final "stop, change strategy" message instead of
            # leaving the assistant on a partial tool_call turn.
            final_text = (
                "(I stopped because a tool was looping. Tell me what you actually want "
                "and I'll try a different approach.)"
            )
            break
    else:
        # Budget exhausted with tool_calls still pending.
        log.warning(
            "iteration budget (%d) exhausted for conversation %s — returning what we have",
            budget.max_total, conversation_id,
        )
        final_text = (
            "(I ran out of iterations before finishing. Try splitting the request into "
            "smaller asks.)"
        )

    # 5) Persist updated conversation ──────────────────────────────────
    # Stash this turn's photos / documents / ui_actions onto the FINAL
    # assistant message's `metadata` so a reload of /api/conversations/{id}
    # surfaces them as `message.photos` / `.documents` / `.ui_actions` —
    # otherwise the chat UI loses the photo grid on refresh (the bug
    # reported as "fotos sind nach reload weg"). The GET endpoint
    # already hoists `metadata.{photos,documents,ui_actions}` to top-
    # level keys, so this is the only writer that needs to know.
    if ui_actions and isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant":
        photos    = [p for a in ui_actions if isinstance(a.get("photos"),    list) for p in a["photos"]]
        documents = [d for a in ui_actions if isinstance(a.get("documents"), list) for d in a["documents"]]
        # Filter the ui_actions list itself to the non-photo / non-document
        # entries so we don't double-store the same payloads.
        other_actions = [
            a for a in ui_actions
            if not isinstance(a.get("photos"), list)
            and not isinstance(a.get("documents"), list)
        ]
        meta = dict(messages[-1].get("metadata") or {})
        if photos:        meta["photos"]     = photos
        if documents:     meta["documents"]  = documents
        if other_actions: meta["ui_actions"] = other_actions
        if meta:
            messages[-1]["metadata"] = meta

    # Thin tool trace — same shape as the streaming path. Built from
    # the messages list (no extra LLM work) so the chat UI's
    # always-on "what tools ran" hint shows up on regenerate
    # responses too, not just freshly-streamed ones.
    tool_trace = _build_turn_tool_trace(messages)
    if tool_trace and isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant":
        meta = dict(messages[-1].get("metadata") or {})
        meta["tool_trace"] = tool_trace
        messages[-1]["metadata"] = meta

    # Strip redundant prose enumeration when a list-carrying card already
    # carries the same items. Mutates messages[-1]['content'] and updates
    # final_text so the API response payload matches the persisted message.
    if (
        ui_actions
        and isinstance(messages[-1], dict)
        and messages[-1].get("role") == "assistant"
    ):
        original_text = messages[-1].get("content") or ""
        stripped = _strip_redundant_enumeration(original_text, ui_actions)
        if stripped != original_text:
            messages[-1]["content"] = stripped
            final_text = stripped

    conversation_io.save_messages(
        conversation_id, role, user.id, messages,
    )
    # Persist the ledger AFTER save_messages — the row must exist first
    # in case this was the conversation's first turn.
    conversation_io.save_ledger(conversation_id, ledger)

    # Auto-title (fire-and-forget) — same trigger as the streaming
    # path so a conversation gets a real title regardless of which
    # endpoint produced its turns.
    try:
        asyncio.create_task(_maybe_generate_title(
            conversation_id=conversation_id, role=role,
            messages=messages, llm=llm,
        ))
    except RuntimeError:
        # No running event loop (sync caller via asyncio.run) — title
        # generation is best-effort, skip silently.
        pass
    # 5b) Persist the per-turn trace at the FINAL assistant message
    #     position so a reload of /api/conversations/{id} hydrates the
    #     Debug pane on every past turn. No-op when dev_mode is off
    #     (trace_iterations is empty so the trace dict isn't built).
    if include_trace:
        # Final assistant message is the last item in `messages` IF the
        # loop ended naturally with a content reply. If it ended on
        # tool_calls (budget exhausted, guardrail halt), the last item
        # is still an assistant message — store there anyway so the user
        # can see what happened.
        final_idx = len(messages) - 1
        total_tool_calls = sum(len(it.get("tool_calls") or []) for it in trace_iterations)
        conversation_io.save_message_trace(
            conversation_id, final_idx,
            {
                "from_cache":       False,
                "total_iterations": len(trace_iterations),
                "total_tool_calls": total_tool_calls,
                "total_duration_s": round(time.perf_counter() - turn_t0, 3),
                "iterations":       trace_iterations,
                "halted":           halted_by_guardrail,
            },
        )

    # 6) Build response envelope ───────────────────────────────────────
    response_text = final_text or "\n\n".join(interim_text_parts) or "(no response)"
    sql_used = audit.last_sql()
    response = {
        "response":        response_text,
        "sql_used":        sql_used,
        "rows_preview":    _rows_from_last_sql(sql_used),
        "ui_actions":      ui_actions,
        "tool_trace":      tool_trace,
        "conversation_id": conversation_id,
        "from_cache":      False,
        "components":      components_seen,
    }
    if include_trace:
        total_tool_calls = sum(len(it.get("tool_calls") or []) for it in trace_iterations)
        response["agent_trace"] = {
            "from_cache":       False,
            "total_iterations": len(trace_iterations),
            "total_tool_calls": total_tool_calls,
            "total_duration_s": round(time.perf_counter() - turn_t0, 3),
            "iterations":       trace_iterations,
            "halted":           halted_by_guardrail,
        }

    return response


# ---------------------------------------------------------------------------
# Trace helpers — truncate long args/results so the trace payload stays small
# ---------------------------------------------------------------------------

_TRACE_ARG_CHARS   = 400  # per-tool args, truncated for the wire
_TRACE_RESULT_CHARS = 600  # per-tool result text


def _truncate_args(args: Any) -> Dict[str, Any]:
    """Make a per-arg trimmed copy of a tool-call args dict."""
    if not isinstance(args, dict):
        return {"_raw": str(args)[:_TRACE_ARG_CHARS]}
    out: Dict[str, Any] = {}
    for k, v in args.items():
        sv = v if isinstance(v, (int, float, bool, type(None))) else str(v)
        if isinstance(sv, str) and len(sv) > _TRACE_ARG_CHARS:
            sv = sv[:_TRACE_ARG_CHARS] + "…"
        out[k] = sv
    return out


def _truncate_result(text: Any) -> str:
    s = str(text or "")
    if len(s) > _TRACE_RESULT_CHARS:
        return s[:_TRACE_RESULT_CHARS] + f"… [+{len(s) - _TRACE_RESULT_CHARS} chars]"
    return s


# ---------------------------------------------------------------------------
# Redundant-enumeration strip — when the assistant prose recites items
# already carried by a UI card emitted in the same turn, replace the
# prose with a tight framing sentence so the user doesn't see the same
# list twice (once in markdown, once as the interactive card).
# ---------------------------------------------------------------------------

# Cards whose payload is itself the enumeration the prose risks duplicating.
# Map: ui_action `type` → (payload key carrying the rows, German framing,
# English framing). New picker cards added here gain free dedupe.
_LIST_CARRYING_CARDS: Dict[str, tuple] = {
    "contact_picker":  ("contacts",  "Welcher Kontakt?",      "Which contact?"),
    "contacts_found":  ("contacts",  "Hier sind die Kontakte.", "Here are the contacts."),
    "template_picker": ("templates", "Welche Vorlage?",       "Which template?"),
    "photos_found":    ("photos",    "Hier sind die Fotos.",  "Here are the photos."),
    "tasks_found":     ("tasks",     "Hier sind die Aufgaben.", "Here are the tasks."),
    "events_found":    ("events",    "Hier sind die Termine.", "Here are the events."),
    "documents_found": ("documents", "Hier sind die Dokumente.", "Here are the documents."),
}


def _strip_redundant_enumeration(
    text: str, ui_actions: List[Dict[str, Any]]
) -> str:
    """Detect prose that enumerates items already shown in a UI card and
    replace it with a one-line framing string. No-op when conditions
    don't hold — conservative on purpose, false positives cost a sentence
    of real prose, false negatives just leave the original duplication.
    """
    if not text or not ui_actions:
        return text

    import re

    # 1) Collect names/titles from any list-carrying card this turn.
    card_names: set[str] = set()
    matched_card_type: Optional[str] = None
    for a in ui_actions:
        if not isinstance(a, dict):
            continue
        ctype = a.get("type")
        spec = _LIST_CARRYING_CARDS.get(ctype) if ctype else None
        if not spec:
            continue
        items_key, _de, _en = spec
        rows = a.get(items_key) or []
        if not isinstance(rows, list):
            continue
        for it in rows:
            if not isinstance(it, dict):
                continue
            for k in ("display_name", "name", "title", "label", "id"):
                v = it.get(k)
                if v is not None and str(v).strip():
                    card_names.add(str(v).strip().lower())
        if rows:
            matched_card_type = ctype

    if not card_names or not matched_card_type:
        return text

    # 2) Detect bulleted / numbered list lines in the prose (≥2 items).
    list_line_re = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$", re.MULTILINE)
    list_lines = list_line_re.findall(text)
    if len(list_lines) < 2:
        return text

    # 3) Confirm overlap: do ≥2 list lines mention a card item by name?
    overlap = 0
    for line in list_lines:
        line_lower = line.lower()
        if any(name in line_lower for name in card_names):
            overlap += 1
            if overlap >= 2:
                break
    if overlap < 2:
        return text

    # 4) Replace with the canned framing in the conversation's language.
    _, de_msg, en_msg = _LIST_CARRYING_CARDS[matched_card_type]
    is_german = bool(re.search(
        r"\b(der|die|das|ein|eine|ich|du|wir|ist|sind|hier|welcher|welche|welches|für|nicht|noch)\b",
        text.lower(),
    ))
    return de_msg if is_german else en_msg


# ---------------------------------------------------------------------------
# Streaming variant — Phase 7. Yields events as the loop runs.
# ---------------------------------------------------------------------------


async def ask_stream(
    message: str,
    *,
    user: User,
    registry: ToolRegistry,
    llm: LlmClient,
    system_prompt: str,
    conversation_id: Optional[str] = None,
    identified_name: Optional[str] = None,
    max_iterations: Optional[int] = None,
    voice_mode: bool = False,
):
    """Streaming variant of :func:`ask`. Async generator that yields events.

    Designed for the voice TTS pipeline (sentence-by-sentence audio
    synthesis as the response forms) and for chat-page SSE. Same dict
    shape returned at the end via a final ``FinalResult`` event so
    consumers can persist + emit ui_actions.

    Event types yielded (defined in ``backend.agent.streaming``):
      - ``IterationStart(n)``: new LLM turn begins
      - ``TextDelta(text)``: incremental assistant text
      - ``ToolCallStart(id, name)``: model is constructing a tool call
      - ``ToolCallReady(id, name, arguments)``: ready to dispatch
      - ``ToolResultEvent(id, name, result_for_llm, ui_actions)``: tool returned
      - ``FinalResult(response)``: loop done; ``response`` is the same
        dict that ``ask()`` returns

    Cache hits short-circuit to a single ``FinalResult`` (no streaming —
    we already have the frozen text).
    """
    from . import streaming as _stream

    role = (user.role or "admin").lower().strip()
    user_language = (user.language or "en").lower().strip()
    conversation_id = conversation_id or uuid.uuid4().hex

    # 2) Reset per-turn state
    audit.reset_turn()

    # 3) Build messages — see the block in ask() for why the ledger is
    # concatenated into the main system prompt instead of sent as a
    # second system message.
    history = conversation_io.load_messages(conversation_id, role)
    from . import entity_ledger as _ledger_mod
    ledger = conversation_io.load_ledger(conversation_id, role)
    ledger_block = _ledger_mod.render_for_llm(ledger)
    sys_content = system_prompt
    if ledger_block:
        sys_content = system_prompt + "\n\n" + ledger_block
    messages: List[Dict[str, Any]] = [system_message(sys_content)]
    messages.extend(history)
    messages.append(user_message(_enrich_template_picked(message, user.language)))
    tools_schema = registry.schemas()

    # 4) Iteration loop
    budget = IterationBudget(max_iterations or MAX_ITERATIONS)
    guardrails = GuardrailController(GuardrailConfig.from_env())
    request_ctx = RequestContext(
        user=user, conversation_id=conversation_id,
        language=user_language, identified_name=identified_name, message=message,
    )
    ui_actions: List[Dict[str, Any]] = []
    iteration = 0
    halted = False
    final_text: Optional[str] = None

    while budget.consume():
        iteration += 1
        yield _stream.IterationStart(n=iteration)

        # Real streaming: pump chunks from the sync openai SDK iterator
        # into an asyncio.Queue via a worker thread. The accumulator runs
        # *inline* in the asyncio loop, one chunk at a time, so each
        # TextDelta yields BEFORE the next chunk is awaited. That's what
        # gives sub-second first-sentence latency for TTS.
        import threading
        queue: asyncio.Queue = asyncio.Queue()
        loop_ = asyncio.get_event_loop()

        def _pump():
            try:
                for chunk in llm.chat_stream(
                    conversation_io.sanitize_for_llm(messages), tools_schema,
                ):
                    loop_.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
            except Exception as exc:  # noqa: BLE001
                loop_.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                loop_.call_soon_threadsafe(queue.put_nowait, ("done", None))

        threading.Thread(target=_pump, name="llm-stream-pump", daemon=True).start()

        # Inline accumulator state — same logic as consume_stream() but
        # we drive it ourselves so we can yield events between chunks.
        content_parts: List[str] = []
        tc_acc: Dict[int, Dict[str, Any]] = {}
        started_tool_indices: set = set()
        finish_reason: Optional[str] = None
        last_err: Optional[BaseException] = None
        ready_calls: List[_stream.ToolCallReady] = []

        while True:
            kind, payload = await queue.get()
            if kind == "error":
                last_err = payload
                break
            if kind == "done":
                break
            # kind == "chunk": process inline + yield events immediately
            chunk = payload
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None)
            fr = getattr(choice, "finish_reason", None)
            if fr:
                finish_reason = fr
            if delta is None:
                continue
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                yield _stream.TextDelta(text=text)
            for tc in (getattr(delta, "tool_calls", None) or []):
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
                if idx not in started_tool_indices and slot["function"]["name"]:
                    started_tool_indices.add(idx)
                    yield _stream.ToolCallStart(
                        id=slot["id"] or f"call_{idx}",
                        name=slot["function"]["name"],
                    )

        if last_err:
            raise last_err

        # Stream finished — finalize tool_calls (repair JSON, emit Ready
        # events) and build the assistant message.
        import json as _json
        normalised_calls: List[Dict[str, Any]] = []
        from .messages import repair_tool_call_arguments
        for idx in sorted(tc_acc):
            slot = tc_acc[idx]
            raw_args = slot["function"]["arguments"] or "{}"
            repaired = repair_tool_call_arguments(
                raw_args, tool_name=slot["function"]["name"] or "?",
            )
            tc_dict = {
                "id":   slot["id"] or f"call_{idx}",
                "type": slot["type"] or "function",
                "function": {
                    "name":      slot["function"]["name"],
                    "arguments": repaired,
                },
            }
            normalised_calls.append(tc_dict)
            try:
                parsed_args = _json.loads(repaired) if repaired else {}
                if not isinstance(parsed_args, dict):
                    parsed_args = {}
            except _json.JSONDecodeError:
                parsed_args = {}
            ready = _stream.ToolCallReady(
                id=tc_dict["id"], name=tc_dict["function"]["name"],
                arguments=parsed_args,
            )
            ready_calls.append(ready)
            yield ready

        assistant_msg = {
            "role":    "assistant",
            "content": "".join(content_parts) or None,
        }
        if normalised_calls:
            assistant_msg["tool_calls"] = normalised_calls

        # Strip side-channel fields before persisting
        assistant_msg.pop("_finish_reason", None)
        messages.append(assistant_msg)

        # Cloud-LLM defensive retry — mirror of the non-streaming path
        # (fix 15b8b34). After the stream finishes, if the assistant has
        # zero tool_calls AND empty content (the silent-stop signature
        # observed against openrouter.ai/qwen3.5-9b → SiliconFlow), make
        # one additional non-streaming chat() call with the same messages
        # list. chat() over chat_stream() is intentional: re-pumping a
        # fresh stream is significantly more machinery for what is a
        # rare failure mode, and the SSE client tolerates a single
        # delayed TextDelta after the empty stream as long as content
        # eventually appears. If the retry produces content/tool_calls
        # we emit them as synthetic stream events so the rest of the
        # streaming loop (dispatch + further iterations) sees the same
        # shape it would have if the stream had produced them directly.
        # Local llama.cpp serving Qwen has not been observed to hit this;
        # the detection check is cheap (strip-and-bool) so the local
        # happy path is unchanged.
        if (
            not has_tool_calls(assistant_msg)
            and not (assistant_msg.get("content") or "").strip()
        ):
            log.info(
                "stream iter %d: empty response (finish=%s) — retrying once via chat()",
                iteration, finish_reason,
            )
            try:
                # Retry against the message list WITHOUT the empty
                # assistant — we'll replace messages[-1] with the result.
                retry_msg = await asyncio.to_thread(
                    llm.chat,
                    conversation_io.sanitize_for_llm(messages[:-1]),
                    tools_schema,
                )
                retry_msg.pop("_usage", None)
                retry_finish = retry_msg.pop("_finish_reason", None)
                retry_has_calls = has_tool_calls(retry_msg)
                retry_text = (retry_msg.get("content") or "")
                if retry_has_calls or retry_text.strip():
                    log.info(
                        "stream iter %d: retry produced content "
                        "(text_len=%d tool_calls=%d finish=%s)",
                        iteration, len(retry_text),
                        len(retry_msg.get("tool_calls") or []),
                        retry_finish,
                    )
                    # Swap the empty assistant for the retry result so
                    # the rest of the loop sees the recovered shape.
                    messages[-1] = retry_msg
                    assistant_msg = retry_msg
                    # Emit recovered text as a single TextDelta. The
                    # SSE client renders this exactly like any other
                    # streamed chunk — just delivered after a brief
                    # pause instead of incrementally.
                    if retry_text:
                        yield _stream.TextDelta(text=retry_text)
                    # Synthesize ToolCallStart + ToolCallReady for any
                    # recovered tool_calls so the dispatch loop below
                    # processes them identically to a streamed batch.
                    if retry_has_calls:
                        for tc in retry_msg.get("tool_calls") or []:
                            tc_id = tc.get("id") or "call_retry"
                            fn = tc.get("function") or {}
                            tc_name = fn.get("name") or "?"
                            yield _stream.ToolCallStart(id=tc_id, name=tc_name)
                            try:
                                parsed = _json.loads(fn.get("arguments") or "{}")
                                if not isinstance(parsed, dict):
                                    parsed = {}
                            except _json.JSONDecodeError:
                                parsed = {}
                            ready = _stream.ToolCallReady(
                                id=tc_id, name=tc_name, arguments=parsed,
                            )
                            ready_calls.append(ready)
                            yield ready
                else:
                    log.warning(
                        "stream iter %d: empty-response retry also returned "
                        "empty (finish=%s) — falling through to empty final_text",
                        iteration, retry_finish,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "stream iter %d: empty-response retry failed: %s — "
                    "keeping original empty assistant message", iteration,
                    f"{type(exc).__name__}: {exc}"[:200],
                )

        if not has_tool_calls(assistant_msg):
            # Final answer — content was already streamed via TextDelta events.
            final_text = assistant_msg.get("content") or ""
            break

        # Dispatch each ready tool call
        tool_ctx = ToolContext(
            request=request_ctx, iteration=iteration,
            conversation_so_far=list(messages),
        )
        for ready in ready_calls:
            pre = guardrails.before_call(ready.name, ready.arguments)
            if not pre.allows_execution:
                messages.append(tool_message(ready.id, ready.name, synthetic_tool_result(pre)))
                if pre.action == "halt" or guardrails.halt_decision is not None:
                    halted = True
                    break
                continue
            # Mandatory skill_view-before-invoke (same rule as the
            # non-streaming path). Reject the invoke synthetically
            # and prompt the model to read the manifest first.
            if (
                ready.name == "invoke_skill"
                and isinstance(ready.arguments, dict)
                and isinstance(ready.arguments.get("name"), str)
                and not _has_seen_skill_view_for(messages, ready.arguments["name"])
            ):
                hint = _INVOKE_NEEDS_READ_HINT.format(name=ready.arguments["name"])
                yield _stream.ToolResultEvent(
                    id=ready.id, name=ready.name,
                    result_for_llm=hint, ui_actions=[],
                )
                messages.append(tool_message(ready.id, ready.name, hint))
                continue
            audit.record_tool_call(ready.name, ready.arguments)
            result = await registry.dispatch(ready.name, ready.arguments, tool_ctx)
            if result.ui_actions:
                ui_actions.extend(result.ui_actions)
                _ledger_mod.absorb(ledger, result.ui_actions)
            yield _stream.ToolResultEvent(
                id=ready.id, name=ready.name,
                result_for_llm=result.result_for_llm, ui_actions=result.ui_actions,
            )
            post = guardrails.after_call(ready.name, ready.arguments, result.result_for_llm)
            llm_text = append_guidance(result.result_for_llm, post)
            # Voice-only brevity nudge — appended INSIDE the tool
            # message content. A mid-conversation system message gets
            # collapsed/ignored by 9B local models, but a postscript
            # on the tool result is treated as part of the data the
            # model just received and reliably moves response length
            # down to one or two sentences. Browser /chat never sets
            # voice_mode, so /chat responses keep their full detail.
            if voice_mode:
                llm_text = (
                    llm_text.rstrip() +
                    "\n\n---\n"
                    "[REPLY INSTRUCTIONS — VOICE MODE]\n"
                    "Your next reply will be read OUT LOUD on a kitchen\n"
                    "tablet. ONE short sentence is ideal, TWO is the absolute\n"
                    "maximum. NO lists. NO bullets. NO markdown. Speak numbers\n"
                    "and dates naturally ('tomorrow at three', not "
                    "'2026-06-11 15:00'). If the user needs detail, end with a\n"
                    "single pointer sentence ('open the calendar to see the\n"
                    "rest') — do NOT dump the detail itself."
                )
            messages.append(tool_message(ready.id, ready.name, llm_text))
            # Pruning: same as the non-streaming path. After any
            # invoke_skill call, replace the matching skill_view's
            # manual prose with a short placeholder so it doesn't
            # bloat downstream LLM calls in this turn.
            if (
                ready.name == "invoke_skill"
                and isinstance(ready.arguments, dict)
                and isinstance(ready.arguments.get("name"), str)
            ):
                _prune_recent_skill_view(messages, ready.arguments["name"])
            if post.should_halt:
                halted = True
                break

        if halted:
            final_text = (
                "(I stopped because a tool was looping. Tell me what you actually want "
                "and I'll try a different approach.)"
            )
            break
    else:
        final_text = (
            "(I ran out of iterations before finishing. Try splitting the request into "
            "smaller asks.)"
        )

    # 5) Persist + yield final result
    # Mirror the non-streaming path: stash this turn's photos / documents /
    # ui_actions onto the FINAL assistant message's `metadata`, so reloading
    # /api/conversations/{id} surfaces them as `message.photos` /
    # `.documents` / `.ui_actions`. Without this, the chat loses every
    # document card, photo grid, and picker as soon as the user navigates
    # away and back. (The GET endpoint already hoists metadata.{…} to
    # top-level keys.)
    if ui_actions and isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant":
        photos    = [p for a in ui_actions if isinstance(a.get("photos"),    list) for p in a["photos"]]
        documents = [d for a in ui_actions if isinstance(a.get("documents"), list) for d in a["documents"]]
        other_actions = [
            a for a in ui_actions
            if not isinstance(a.get("photos"), list)
            and not isinstance(a.get("documents"), list)
        ]
        meta = dict(messages[-1].get("metadata") or {})
        if photos:        meta["photos"]     = photos
        if documents:     meta["documents"]  = documents
        if other_actions: meta["ui_actions"] = other_actions
        if meta:
            messages[-1]["metadata"] = meta

    # Thin tool trace — pair up tool_calls with their tool messages so
    # the chat UI can show a one-liner ("find_contact · add_event")
    # on every assistant bubble. Distinct from the full timing-rich
    # `agent_trace` (still dev-mode gated): this is on by default
    # because trust depends on the user being able to see what
    # tools ran for any given answer.
    tool_trace = _build_turn_tool_trace(messages)
    if tool_trace and isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant":
        meta = dict(messages[-1].get("metadata") or {})
        meta["tool_trace"] = tool_trace
        messages[-1]["metadata"] = meta

    # Strip redundant prose enumeration before persisting. Live-streamed
    # text already reached the user; this cleans the DB so reloads /
    # conversation switches don't show the duplicate list. Also updates
    # final_text so the FinalResult event below carries the cleaned text.
    if (
        ui_actions
        and isinstance(messages[-1], dict)
        and messages[-1].get("role") == "assistant"
    ):
        original_text = messages[-1].get("content") or ""
        stripped = _strip_redundant_enumeration(original_text, ui_actions)
        if stripped != original_text:
            messages[-1]["content"] = stripped
            final_text = stripped

    conversation_io.save_messages(conversation_id, role, user.id, messages)
    conversation_io.save_ledger(conversation_id, ledger)

    # Fire-and-forget LLM title generation when the conversation has
    # reached its second assistant turn and still has no title. Doesn't
    # block FinalResult — the sidebar picks up the new title on its
    # next refetch (which the chat fires right after the turn ends).
    asyncio.create_task(_maybe_generate_title(
        conversation_id=conversation_id, role=role,
        messages=messages, llm=llm,
    ))

    response_text = final_text or "(no response)"
    sql_used = audit.last_sql()
    response_dict = {
        "response":        response_text,
        "sql_used":        sql_used,
        "rows_preview":    _rows_from_last_sql(sql_used),
        "ui_actions":      ui_actions,
        "tool_trace":      tool_trace,
        "conversation_id": conversation_id,
        "from_cache":      False,
        "components":      [],  # streaming consumers see real-time events instead
    }
    yield _stream.FinalResult(response=response_dict)


# ─── Mandatory skill_view-before-invoke enforcement ─────────────────
# Pure-Hermes architecture: the model MUST call skill_view(name) before
# invoke_skill(name) for each distinct skill, once per turn. The index
# in the system prompt shows only `name — description` (no args list),
# so the model literally can't construct a valid call without reading
# the manifest first. This eliminates the "speculatively-fill all visible
# args" failure mode (the find_photo person+people+query bug).
#
# Reading is cheap: see _prune_recent_skill_view below for the auto-prune
# after the matching invoke succeeds. So the cycle is:
#
#   iter N:   skill_view(X)        → manifest in context (~1-4 KB)
#   iter N+1: invoke_skill(X, ...) → call runs with correct args; the
#                                    skill_view at iter N is pruned to
#                                    a 100-char placeholder
#
# An invoke_skill without a preceding skill_view in the same turn is
# rejected with a short directive; the model retries after reading.
#
# Wording note (2026-06-14): the original directive opened with the word
# "REJECTED — …". The hosted Qwen 3.5 9B on OpenRouter / SiliconFlow
# treats that word as a terminal "task impossible" signal: seeing it in
# its own prior tool messages, it gives up and emits finish=stop with
# empty content on subsequent turns. Local llama.cpp serving Qwen
# overrides the prior fine (testing across compose / find_photo /
# search_documents / daily-overview flows showed correct REJECTED →
# skill_view → invoke_skill recovery in every case). Reworded to drop
# the poison word while keeping the same instructional content. Same
# token cost, same retry directive, same dispatch flow — only the
# opener swaps from "REJECTED — call skill_view(…)" to "Read
# skill_view(…)". Local behaviour stays identical; cloud Qwen no
# longer sees the poison token in its conversation history.
_INVOKE_NEEDS_READ_HINT = (
    "Read skill_view({name!r}) first to learn the args + rules, then "
    "re-call invoke_skill. The skill index in the system prompt shows "
    "only name + description; the per-arg rules live in the manifest "
    "(auto-pruned from your context after this invoke succeeds, so "
    "reading is cheap)."
)


def _has_seen_skill_view_for(
    messages: List[Dict[str, Any]],
    target_skill: str,
) -> bool:
    """True if a skill_view(name=target_skill) tool_call appeared in this
    turn's message history. "This turn" = all messages since the most
    recent user message (the loop starts each turn with messages already
    holding prior conversation history — we only care about the current
    turn's reads).

    A pruned skill_view STILL counts as seen — the placeholder begins
    with a known prefix that the pruning function writes.
    """
    import json as _json
    target = (target_skill or "").strip()
    if not target:
        return False
    # Find the most recent user message — anchor for "this turn".
    cutoff = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            cutoff = i
            break
    for m in messages[cutoff:]:
        if not isinstance(m, dict): continue
        if m.get("role") != "assistant": continue
        for tc in (m.get("tool_calls") or []):
            fn = (tc.get("function") or {})
            if fn.get("name") != "skill_view": continue
            raw_args = fn.get("arguments") or "{}"
            try:
                parsed = _json.loads(raw_args)
            except Exception:
                continue
            if not isinstance(parsed, dict): continue
            if parsed.get("name") == target:
                return True
    return False


# ─── skill_view post-invoke pruning ─────────────────────────────────
# A skill_view returns 1–4 KB of manifest prose (when_to_use bullets,
# per-arg rules, examples). Pre-invoke that prose is load-bearing: the
# model needs the rules to call invoke_skill with the right args. After
# the matching invoke_skill succeeds, the prose is just dead weight —
# it still sits in the messages list and gets re-fed to the LLM on
# every subsequent iteration of the turn, growing the per-call context
# without informing any new decisions.
#
# This function finds the most recent skill_view(name=X) tool message
# in `messages` and replaces its content with a short placeholder.
# Tool_call/tool_result pairing is preserved (we mutate the content,
# not the message itself), so the OpenAI API still accepts the array.
#
# Effect: with pruning, "read manual → invoke skill" becomes affordable
# for compose pipelines that previously exhausted the 16-iteration cap
# on manual reads alone. The cap is raised to 28 in tandem.
_PRUNE_PLACEHOLDER = (
    "(skill_view manifest pruned after successful invoke — "
    "the rules were used; re-call skill_view if you need to re-read.)"
)


def _prune_recent_skill_view(
    messages: List[Dict[str, Any]],
    target_skill: str,
) -> int:
    """After a successful invoke_skill(target_skill), find the most recent
    skill_view(name=target_skill) tool result and replace its content with
    a short placeholder. Returns bytes reclaimed (0 if no match found or
    the read was already pruned).

    The match check walks the messages list backwards looking for tool
    messages with name='skill_view', then verifies via the preceding
    assistant message's tool_calls that the skill_view was for the
    target skill (the tool args carry the skill name as a JSON string).
    """
    import json as _json
    target = (target_skill or "").strip()
    if not target:
        return 0

    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not isinstance(m, dict): continue
        if m.get("role") != "tool": continue
        if m.get("name") != "skill_view": continue
        content = m.get("content")
        if not isinstance(content, str): continue
        if content.startswith("(skill_view manifest pruned"): continue
        tc_id = m.get("tool_call_id")
        if not tc_id: continue

        # Walk backward from this tool message to find the assistant
        # that emitted the matching tool_call — stop at the first
        # assistant message we encounter (interleaved tool messages
        # belong to a different call in the same batch).
        for j in range(i - 1, -1, -1):
            am = messages[j]
            if not isinstance(am, dict) or am.get("role") != "assistant":
                continue
            tcs = am.get("tool_calls") or []
            matching = next((t for t in tcs if t.get("id") == tc_id), None)
            if matching is None:
                break
            raw_args = (matching.get("function") or {}).get("arguments") or "{}"
            try:
                parsed = _json.loads(raw_args)
            except Exception:
                parsed = {}
            sv_name = parsed.get("name") if isinstance(parsed, dict) else None
            if sv_name == target:
                original_len = len(content)
                m["content"] = _PRUNE_PLACEHOLDER
                return max(0, original_len - len(_PRUNE_PLACEHOLDER))
            break  # tc_id matched but wrong skill — stop walking
    return 0


def _build_turn_tool_trace(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair the assistant's tool_calls with the matching tool replies
    since the most recent user message.

    Output shape (one entry per call):
        {"name": "find_contact",
         "args": {"query": "Hans"},
         "result": "Found 3 contacts: ..." (truncated to 400 chars)}

    Used by both ``ask_stream`` (attached to the final assistant
    message's metadata) and the GET /api/conversations/{id} endpoint
    (so reloads carry the trace too). Lightweight — no timings, no
    iteration breakdown; the dev-mode ``agent_trace`` still owns that.
    """
    import json as _json
    # Find the index of the most recent user message — anything before
    # that belongs to a different turn.
    cutoff = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            cutoff = i + 1
            break
    relevant = messages[cutoff:]
    entries: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    for m in relevant:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in (m.get("tool_calls") or []):
                fn = (tc.get("function") or {})
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed = _json.loads(raw_args) if raw_args else {}
                    if not isinstance(parsed, dict):
                        parsed = {"_raw": raw_args}
                except (ValueError, TypeError):
                    parsed = {"_raw": raw_args[:200]}
                entry = {"name": fn.get("name") or "?", "args": parsed, "result": None}
                entries.append(entry)
                if tc.get("id"):
                    by_id[tc["id"]] = entry
        elif m.get("role") == "tool":
            entry = by_id.get(m.get("tool_call_id"))
            if entry is not None:
                txt = m.get("content") or ""
                if isinstance(txt, str):
                    entry["result"] = txt if len(txt) <= 400 else txt[:400] + "…"
    return entries


async def _maybe_generate_title(
    *, conversation_id: str, role: str,
    messages: List[Dict[str, Any]], llm: LlmClient,
) -> None:
    """Ask the LLM for a 2–5 word title once a conversation has matured
    enough to have a topic. No-op when the conversation already has a
    title or hasn't reached its second assistant reply yet.

    Wrapped in a broad try/except — title generation must never break
    the actual turn it's piggybacking on.
    """
    try:
        # Trigger: at least 2 user messages + 2 assistant messages with
        # content. Avoids titling a one-shot "/help" or a single greeting.
        n_user = sum(
            1 for m in messages
            if isinstance(m, dict) and m.get("role") == "user"
        )
        n_assistant_with_content = sum(
            1 for m in messages
            if isinstance(m, dict)
            and m.get("role") == "assistant"
            and (m.get("content") or "").strip()
        )
        if n_user < 2 or n_assistant_with_content < 2:
            return
        from ..database import conn_ctx, DEFAULT_DB_PATH
        with conn_ctx(DEFAULT_DB_PATH) as conn:
            row = conn.execute(
                "SELECT title FROM agent_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if row and (row["title"] or "").strip():
            return  # already titled — don't churn

        # Build a tiny prompt out of the first user message + first
        # assistant content + the latest user message. Enough signal,
        # tiny token cost.
        first_user = next(
            (m for m in messages if isinstance(m, dict) and m.get("role") == "user"),
            None,
        )
        first_assistant = next(
            (m for m in messages
             if isinstance(m, dict)
             and m.get("role") == "assistant"
             and (m.get("content") or "").strip()),
            None,
        )
        last_user = next(
            (m for m in reversed(messages)
             if isinstance(m, dict) and m.get("role") == "user"),
            None,
        )
        excerpts: List[str] = []
        for label, m in (("user", first_user), ("assistant", first_assistant),
                         ("user_latest", last_user)):
            if not m:
                continue
            text = (m.get("content") or "").strip()
            if not text:
                continue
            excerpts.append(f"[{label}] {text[:300]}")
        if not excerpts:
            return

        system_msg = (
            "Generate a 2–5 word descriptive title for this conversation "
            "in the user's language. Output ONLY the title — no quotes, "
            "no punctuation at the end, no commentary, no code fences. "
            "Examples: 'Hannover-Trip planen', 'Brief an Hausverwaltung', "
            "'Steuerunterlagen Q1', 'Dinner with Sarah'."
        )
        user_msg = "Conversation excerpts:\n" + "\n".join(excerpts) + "\n\nTitle:"

        resp = await asyncio.to_thread(
            llm.chat,
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            None,                # tools
            max_tokens=32,
            temperature=0.2,
        )
        raw = ((resp or {}).get("content") or "").strip()
        # Strip leading/trailing quotes + trailing periods that the
        # model sometimes adds despite the instructions.
        title = raw.strip('"\'""„«»‹›').rstrip(". ").strip()
        if not title:
            return
        # Hard cap so a chatty model can't fill the sidebar with a
        # whole sentence.
        if len(title) > 64:
            title = title[:63].rstrip() + "…"

        from ..database import conn_ctx as _conn_ctx, DEFAULT_DB_PATH as _DB
        with _conn_ctx(_DB) as conn:
            conn.execute(
                "UPDATE agent_conversations SET title = ? WHERE id = ?",
                (title, conversation_id),
            )
        log.info("auto-titled conversation %s: %r", conversation_id, title)
    except Exception as exc:  # noqa: BLE001
        log.debug("title generation skipped for %s: %s", conversation_id, exc)


def _rows_from_last_sql(sql: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    """Materialize rows_preview for the response envelope.

    The legacy ``ask_async`` returned rows from cache or from the live
    SQL path. With the new loop the LLM already saw the rows in its
    tool result; we just re-run the SELECT here to expose them in the
    API response shape so the chat UI's debug pane keeps working.
    """
    if not sql or not cache.is_safe_to_cache(sql):
        return None
    try:
        return cache.execute_cached_sql(sql)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Error wrapper — matches ask_async's error-dict shape
# ---------------------------------------------------------------------------


def error_response(
    exc: BaseException,
    *,
    conversation_id: Optional[str],
    llm: LlmClient,
) -> Dict[str, Any]:
    """Build the error envelope ``ask_async`` returns when the loop fails.

    Same shape callers already handle, so the new loop can be dropped
    into ``/api/ask`` without changing the route.
    """
    return {
        "response": (
            f"Agent loop failed: {type(exc).__name__}: {exc}. "
            f"Check that the LLM endpoint at {llm.base_url} is reachable "
            f"and serving model '{llm.model}'."
        ),
        "sql_used":        None,
        "rows_preview":    None,
        "ui_actions":      [],
        "from_cache":      False,
        "error":           True,
        "conversation_id": conversation_id,
        "timestamp":       datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    }


__all__ = ["ask", "ask_stream", "error_response", "MAX_ITERATIONS"]
