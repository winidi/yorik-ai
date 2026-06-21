"""Yorik 1.0 suggestion engine.

Analyses incoming messages in the context of everything Yorik knows
about the sender, emits typed suggestions (draft_reply, propose_
meeting_slot, ...) with evidence-backed reasoning.

Architecture is plugin-first: yorik-core itself ships as the first
"plugin" — every retriever, suggestion type, and trigger registers
through the same contract that a future third-party addon would use.
The dispatch path doesn't know which is which.

See backend/suggestions/registry.py for the contract.
"""
