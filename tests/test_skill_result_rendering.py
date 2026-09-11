"""What the LLM reads after a skill ran: hint + data, capped unless the
skill asks for its whole payload."""

from backend.ui_tools import render_skill_result, FULL_OUTPUT_MAX_CHARS


def test_hint_plus_data_is_capped_unless_full_output():
    big = {"_llm_hint": "Draft the day.", "context": {"open_tasks": [{"title": f"Task {i}"} for i in range(200)]},
           "agent_candidates": [{"title": "Video 9 schneiden"}]}
    short = render_skill_result(big)
    assert short.startswith("Draft the day.") and "(truncated)" in short and "Video 9" not in short
    full = render_skill_result({**big, "_full_output": True})
    assert full.startswith("Draft the day.") and "Video 9 schneiden" in full and "_full_output" not in full
    assert len(full) <= FULL_OUTPUT_MAX_CHARS + 200
    assert render_skill_result({"_llm_hint": "Say ok."}) == "Say ok."
    assert render_skill_result({"a": 1}) == "{'a': 1}"
    assert render_skill_result("x" * 900).endswith("…(truncated)")
