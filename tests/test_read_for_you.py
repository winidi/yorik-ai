"""The first invoke of a skill in a turn no longer bounces off a
"read skill_view first" wall: the loop reads the manifest itself and
runs the call when the arguments fit (2026-10-16)."""
from __future__ import annotations


def test_fitting_arguments_run_at_once():
    from backend.agent.loop import _auto_read
    ok, text = _auto_read("read_document", {"doc_id": 1})
    assert ok and text == ""


def test_a_guessed_argument_gets_the_manifest_in_one_message():
    from backend.agent.loop import _auto_read, _AUTO_READ_PREFIX, _has_seen_skill_view_for, _prune_recent_skill_view
    ok, text = _auto_read("read_document", {"id": 1})
    assert not ok
    assert text.startswith(_AUTO_READ_PREFIX.format(name="read_document"))
    assert "unknown argument(s): id" in text and "missing required argument(s): doc_id" in text
    assert '"inputs"' in text and "NOT RUN" in text
    msgs = [{"role": "user", "content": "zeig mir doc 1"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
             "function": {"name": "invoke_skill", "arguments": "{\"name\": \"read_document\", \"args\": {\"id\": 1}}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "invoke_skill", "content": text}]
    # the read-for-you message counts as the manifest being read this turn …
    assert _has_seen_skill_view_for(msgs, "read_document") is True
    assert _has_seen_skill_view_for(msgs, "find_person") is False
    # … and is shortened after the skill ran, like a real skill_view
    reclaimed = _prune_recent_skill_view(msgs, "read_document")
    assert reclaimed > 0 and msgs[2]["content"].startswith(_AUTO_READ_PREFIX.format(name="read_document"))
    assert _has_seen_skill_view_for(msgs, "read_document") is True


def test_unknown_and_disabled_skills_are_named():
    from backend.agent.loop import _auto_read
    ok, text = _auto_read("find_document", {"query": "x"})
    assert not ok and "No skill named 'find_document'" in text


def test_a_claimed_search_without_a_lookup_is_sent_back(fresh_app):
    from backend.agent import grounding as g
    turn = [{"role": "user", "content": "schreib dem vermieter"}]
    v = g.check("In den Kontakten habe ich keinen Vermieter gefunden.", turn, [])
    assert not v.ok and v.missing == [g.CLAIMED_LOOKUP]
    note = g.nudge_message(v.missing)[1]["content"]
    assert "no lookup ran" in note and "find_person" in note
    # with a tool answer in the turn the same sentence is fine
    turn2 = turn + [{"role": "tool", "tool_call_id": "t", "name": "invoke_skill", "content": "{\"results\": []}"}]
    assert g.check("In den Kontakten habe ich keinen Vermieter gefunden.", turn2, []).ok
    # plain answers are untouched
    assert g.check("Der Entwurf ist bereit.", turn, []).ok
    assert "nicht wirklich nachgesehen" in g.fallback_text([g.CLAIMED_LOOKUP], "de", "Keinen gefunden.")
