"""The loop brake stops by default (2026-09-29): the same read-only call
with the same answer is warned about the second time and refused the
third, instead of running 28 steps on a slow machine."""

from backend.agent.guardrails import GuardrailConfig, GuardrailController


def test_the_third_identical_read_is_refused(monkeypatch):
    monkeypatch.delenv("YORIK_GUARDRAILS_HARD_STOP", raising=False)
    g = GuardrailController(GuardrailConfig.from_env())
    args = {"name": "check_calendar"}
    actions = []
    for _ in range(4):
        pre = g.before_call("skill_view", args)
        actions.append(pre.action)
        if pre.action in ("block", "halt"):
            break
        g.after_call("skill_view", args, "same manual text")
    assert actions[-1] in ("block", "halt") and len(actions) <= 4


def test_the_stop_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("YORIK_GUARDRAILS_HARD_STOP", "0")
    g = GuardrailController(GuardrailConfig.from_env())
    for _ in range(6):
        assert g.before_call("skill_view", {"name": "x"}).action not in ("block", "halt")
        g.after_call("skill_view", {"name": "x"}, "same")
