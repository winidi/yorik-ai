"""Yorik's own agent loop — Phase 0 scaffolding.

This package is being built out per the masterplan to replace Vanna 2.0
(archived upstream). The plan is in the design doc — short version:
roll our own ~1,800-LOC loop, port ~120 LOC verbatim from
NousResearch/hermes-agent under MIT, ship feature parity for Yorik's
needs in ~8.5 dev-days, plug architecture for web search, MCP, memory,
streaming.

Phase 0 (this commit): skeleton + verbatim ports of the defensive
primitives Hermes already battle-tested:

- `budget.py`             — IterationBudget
- `retry.py`              — jittered_backoff
- `sanitize.py`           — surrogates + tool-call repair + strict-API stripping
- `prompt_caching.py`     — Anthropic cache_control (no-op for Qwen)
- `providers/web_search/` — ABC + registry (no backends yet)

Phases 1-4 build the loop itself + cut over from Vanna.
Phases 5-8 add web search, MCP, streaming, etc.
"""
