# SPDX-License-Identifier: MIT
# Ported (controller + dataclasses) from NousResearch/hermes-agent
# agent/tool_guardrails.py (MIT, https://github.com/NousResearch/hermes-agent).
# Adapted: Yorik tool classifications + failure-detection patterns. The
# core control-flow (signature dedup, exact_failure / same_tool_failure /
# no_progress counters with warn/halt thresholds) is preserved.
"""Per-turn tool-call loop guardrails.

Generalises the special-case ``_deletes_this_turn`` throttle from Phase
1 into a real loop-detection system. Three categories of unhealthy
patterns are detected:

- **exact_failure**: the same ``(tool_name, args)`` returned an error N
  times → the model is hammering an identical bad call.
- **same_tool_failure**: a tool failed N times this turn regardless of
  args → the model is stuck retrying the same broken path with tweaks.
- **no_progress** (idempotent tools only): the same read-only call
  returned the same result N times → the model already has the data
  and is wasting iterations.

For each category there's a *warn* threshold (default ON — adds an
advisory line to the tool result so the model self-corrects) and a
*halt/block* threshold (default OFF — explicit opt-in via
``YORIK_GUARDRAILS_HARD_STOP=1``; refuses execution and surfaces a
structured error so the loop can decide whether to break).

Stateless apart from per-turn dicts on the controller. Construct once
per /api/ask (loop does this), call ``before_call`` + ``after_call``
around each dispatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# Yorik tool classifications
# ---------------------------------------------------------------------------
#
# IDEMPOTENT: read-only, safe to call repeatedly. If the model calls one of
# these twice with the same args and the second result is identical, it's a
# waste — emit a warning.
#
# MUTATING:  side-effects on the DB / external systems. Don't treat as
# idempotent (the same call can legitimately produce different results).

IDEMPOTENT_TOOL_NAMES: frozenset[str] = frozenset({
    # Our tools
    "show_calendar",
    "list_calendar_layouts",
    "list_skills",
    "list_connectors",
    "list_apps",
    "search_documents",
    # Phase 5+: web search / extract / MCP read tools
    "web_search",
    "web_extract",
})

MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    # use_skill itself is mutating when the inner skill is in audit.MUTATION_SKILLS.
    # The controller can't know that without inspecting args, so list use_skill here
    # as mutating; the no-progress detection won't fire for it.
    "use_skill",
    "install_connector",
    "trigger_connector",
    # run_sql: SELECTs are idempotent but the same name can also do mutations.
    # Treat as mutating for guardrail purposes — the gate already refuses raw
    # INSERT/UPDATE/DELETE on protected tables.
    "run_sql",
})


# ---------------------------------------------------------------------------
# Config + decision dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardrailConfig:
    """Thresholds for per-turn tool-call loop detection.

    Warnings ON by default; never prevent tool execution. Hard stops OFF
    by default — opt-in via env ``YORIK_GUARDRAILS_HARD_STOP=1`` because
    they will refuse calls and the loop has to surface that to the LLM.
    """
    warnings_enabled: bool = True
    hard_stop_enabled: bool = False

    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5

    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8

    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5

    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools:   frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)

    @classmethod
    def from_env(cls) -> "GuardrailConfig":
        """Build a config from environment overrides. All keys optional."""
        def _bool(key: str, default: bool) -> bool:
            raw = os.getenv(key, "").strip().lower()
            if raw in {"1", "true", "yes", "on"}: return True
            if raw in {"0", "false", "no", "off"}: return False
            return default

        def _int(key: str, default: int) -> int:
            try:
                v = int(os.getenv(key, ""))
            except (TypeError, ValueError):
                return default
            return v if v >= 1 else default

        return cls(
            warnings_enabled=_bool("YORIK_GUARDRAILS_WARN", True),
            hard_stop_enabled=_bool("YORIK_GUARDRAILS_HARD_STOP", False),
            exact_failure_warn_after=_int("YORIK_GUARDRAILS_EXACT_FAIL_WARN", 2),
            exact_failure_block_after=_int("YORIK_GUARDRAILS_EXACT_FAIL_BLOCK", 5),
            same_tool_failure_warn_after=_int("YORIK_GUARDRAILS_SAME_TOOL_WARN", 3),
            same_tool_failure_halt_after=_int("YORIK_GUARDRAILS_SAME_TOOL_HALT", 8),
            no_progress_warn_after=_int("YORIK_GUARDRAILS_NO_PROGRESS_WARN", 2),
            no_progress_block_after=_int("YORIK_GUARDRAILS_NO_PROGRESS_BLOCK", 5),
        )


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable identity for (tool_name, canonical args)."""
    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        return cls(
            tool_name=tool_name,
            args_hash=_sha256(_canonical_args(args or {})),
        )


@dataclass(frozen=True)
class GuardrailDecision:
    """Returned by before_call and after_call.

    ``action``:
      - ``allow``: proceed normally.
      - ``warn``: proceed but the loop should append the ``message`` to
        the tool-result text so the model self-corrects on the next turn.
      - ``block``: refuse this dispatch; loop should append a synthetic
        tool result built from ``message`` so the LLM sees the refusal.
      - ``halt``: same as block but signals "stop the entire loop after
        appending the synthetic result"; loop breaks out.
    """
    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: Optional[ToolCallSignature] = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}


# ---------------------------------------------------------------------------
# Failure detection — Yorik patterns
# ---------------------------------------------------------------------------

# Our tools surface failures as text that starts with one of these markers.
# The GatedRunSqlTool prepends "SQL REJECTED"; the new Tool.dispatch
# error path prepends "ERROR:"; raised exceptions get the same treatment.
# Matched case-insensitively so a bare "Error: …" or "FAILED:" from an
# adapter still trips the detector.
_FAILURE_PREFIX_RE = re.compile(
    r"^\s*(error\b|failed\b|sql rejected\b|exception\b)", re.IGNORECASE,
)
_FAILURE_KEY_RE = re.compile(r'"error"|"failed"|"unrepairable"', re.IGNORECASE)


def classify_tool_failure(tool_name: str, result: Optional[str]) -> bool:
    """Return True if a tool's ``result_for_llm`` text indicates failure.

    Used by ``after_call`` when the loop doesn't pass an explicit
    ``failed=`` flag. Yorik-specific patterns; safe default is False.
    """
    if not result:
        return False
    if _FAILURE_PREFIX_RE.match(result):
        return True
    if _FAILURE_KEY_RE.search(result[:500]):
        return True
    return False


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class GuardrailController:
    """Per-turn controller for repeated failed / non-progressing tool calls.

    One instance per /api/ask. Reset is automatic on construction; the
    loop just constructs a fresh one each turn.
    """

    def __init__(self, config: Optional[GuardrailConfig] = None) -> None:
        self.config = config or GuardrailConfig()
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: Optional[GuardrailDecision] = None

    @property
    def halt_decision(self) -> Optional[GuardrailDecision]:
        """If non-None, the loop should break after the current iteration."""
        return self._halt_decision

    # ------------------------------------------------------------------
    # Hooks the loop calls
    # ------------------------------------------------------------------

    def before_call(
        self,
        tool_name: str,
        args: Optional[Mapping[str, Any]],
    ) -> GuardrailDecision:
        """Check BEFORE executing a tool. Returns ``allow`` or ``block``."""
        signature = ToolCallSignature.from_call(tool_name, args)
        if not self.config.hard_stop_enabled:
            return GuardrailDecision(tool_name=tool_name, signature=signature)

        exact = self._exact_failure_counts.get(signature, 0)
        if exact >= self.config.exact_failure_block_after:
            decision = GuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: the same call failed {exact} times "
                    "with identical arguments. Change strategy — different args, "
                    "different tool, or explain the blocker instead of retrying."
                ),
                tool_name=tool_name,
                count=exact,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        if self._is_idempotent(tool_name):
            record = self._no_progress.get(signature)
            if record is not None and record[1] >= self.config.no_progress_block_after:
                decision = GuardrailDecision(
                    action="block",
                    code="idempotent_no_progress_block",
                    message=(
                        f"Blocked {tool_name}: this read-only call returned the "
                        f"same result {record[1]} times. Use the result already "
                        "provided or change the query."
                    ),
                    tool_name=tool_name,
                    count=record[1],
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

        return GuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Optional[Mapping[str, Any]],
        result: Optional[str],
        *,
        failed: Optional[bool] = None,
    ) -> GuardrailDecision:
        """Update counters AFTER a tool dispatch. Returns one of
        ``allow``/``warn``/``halt``."""
        signature = ToolCallSignature.from_call(tool_name, args)
        if failed is None:
            failed = classify_tool_failure(tool_name, result)

        if failed:
            return self._on_failure(tool_name, signature)
        return self._on_success(tool_name, signature, result)

    # ------------------------------------------------------------------
    # Failure / success paths
    # ------------------------------------------------------------------

    def _on_failure(self, tool_name: str, signature: ToolCallSignature) -> GuardrailDecision:
        exact = self._exact_failure_counts.get(signature, 0) + 1
        self._exact_failure_counts[signature] = exact
        self._no_progress.pop(signature, None)

        same = self._same_tool_failure_counts.get(tool_name, 0) + 1
        self._same_tool_failure_counts[tool_name] = same

        # Hard halts first (only if opted in).
        if self.config.hard_stop_enabled and same >= self.config.same_tool_failure_halt_after:
            decision = GuardrailDecision(
                action="halt",
                code="same_tool_failure_halt",
                message=(
                    f"Stopped {tool_name}: failed {same} times this turn. "
                    "Stop retrying — choose a different approach or tool."
                ),
                tool_name=tool_name,
                count=same,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        # Warning thresholds (default ON, never prevent execution).
        if self.config.warnings_enabled and exact >= self.config.exact_failure_warn_after:
            return GuardrailDecision(
                action="warn",
                code="repeated_exact_failure_warning",
                message=(
                    f"{tool_name} has failed {exact} times with identical arguments. "
                    "This looks like a loop; change your strategy instead of retrying unchanged."
                ),
                tool_name=tool_name,
                count=exact,
                signature=signature,
            )
        if self.config.warnings_enabled and same >= self.config.same_tool_failure_warn_after:
            return GuardrailDecision(
                action="warn",
                code="same_tool_failure_warning",
                message=(
                    f"{tool_name} has failed {same} times this turn (with various arguments). "
                    "Diagnose before retrying: read the latest error, verify your assumptions, "
                    "or pick a different tool."
                ),
                tool_name=tool_name,
                count=same,
                signature=signature,
            )

        return GuardrailDecision(tool_name=tool_name, count=exact, signature=signature)

    def _on_success(
        self,
        tool_name: str,
        signature: ToolCallSignature,
        result: Optional[str],
    ) -> GuardrailDecision:
        # Reset failure counters for this signature/tool on success.
        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)

        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return GuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat = 1
        if previous is not None and previous[0] == result_hash:
            repeat = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat)

        if self.config.warnings_enabled and repeat >= self.config.no_progress_warn_after:
            return GuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat} times. "
                    "Use the result already provided or change the query — "
                    "don't repeat the same call."
                ),
                tool_name=tool_name,
                count=repeat,
                signature=signature,
            )

        return GuardrailDecision(tool_name=tool_name, count=repeat, signature=signature)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools


# ---------------------------------------------------------------------------
# Loop-side helpers for stitching decisions into messages
# ---------------------------------------------------------------------------


def synthetic_tool_result(decision: GuardrailDecision) -> str:
    """Build a structured tool-result text for a blocked/halted call.

    The loop appends this in place of the real tool result when the
    guardrail refused execution. The LLM sees a clear error and can
    self-correct.
    """
    return json.dumps(
        {"error": decision.message, "guardrail_code": decision.code},
        ensure_ascii=False,
    )


def append_guidance(result_text: str, decision: GuardrailDecision) -> str:
    """Append a warn/halt guidance line to the real tool-result text.

    Used in the warn path so the model self-corrects without losing the
    actual result. No-op for ``allow`` / no message.
    """
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result_text
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    suffix = (
        f"\n\n[{label}: {decision.code}; count={decision.count}; {decision.message}]"
    )
    return (result_text or "") + suffix


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def _canonical_args(args: Mapping[str, Any]) -> str:
    return json.dumps(
        args, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )


def _result_hash(result: Optional[str]) -> str:
    if not result:
        return _sha256("")
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return _sha256(result)
    try:
        canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), default=str)
    except TypeError:
        canonical = str(parsed)
    return _sha256(canonical)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "GuardrailConfig",
    "ToolCallSignature",
    "GuardrailDecision",
    "GuardrailController",
    "classify_tool_failure",
    "synthetic_tool_result",
    "append_guidance",
    "IDEMPOTENT_TOOL_NAMES",
    "MUTATING_TOOL_NAMES",
]
