"""Grounding check: hard values and quotes must come from tool results
(chat test 2026-09-26/27 — Mama's "account number" invented twice)."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import seed_user
from tests.test_loop_harness import _FakeLlm, _tool_call


class _WaTool:
    """Returns a WhatsApp message like whatsapp_read, with a raw result."""
    name = "wa_tool"
    description = "reads"
    json_schema = {"type": "object", "properties": {}}

    async def execute(self, ctx, args):
        from backend.agent.tools import ToolResult
        raw = {"chat": "Mama Nowa", "chat_jid": "4917@s.whatsapp.net",
               "messages": [{"id": 1, "when": "Fr 09.01.2026 21:51", "who": "Mama",
                             "text": "DE32500105175422716331"},
                            {"id": 2, "when": "Mi 01.07.2026 10:19", "who": "Mama",
                             "text": "Ich musste nach einem Betrugsanruf das Konto wechseln"}]}
        return ToolResult(result_for_llm=json.dumps(raw, ensure_ascii=False),
                          metadata={"skill": "whatsapp_read", "raw": raw})


def _run(fake, *, user_id, language="de"):
    from backend.agent import loop
    from backend.agent.context import User
    from backend.agent.tools import ToolRegistry
    reg = ToolRegistry()
    reg.register(_WaTool())
    user = User(id=user_id, role="admin", language=language, name=None)
    return asyncio.run(loop.ask("welche kontonummer hat mama geschickt?", user=user, registry=reg, llm=fake,
                                system_prompt="You are a test butler."))


@pytest.fixture
def uid(fresh_app):
    return seed_user(name="Max", role="admin", email="max@example.com")


def test_invented_value_is_sent_back_once(uid):
    fake = _FakeLlm([
        {"role": "assistant", "content": "Die Nummer ist DE44 5002 0500 4500 4500 03."},
        {"role": "assistant", "content": "Das habe ich nicht gefunden."},
    ])
    out = _run(fake, user_id=uid)
    assert out["response"] == "Das habe ich nicht gefunden."
    nudge = fake.calls[1][-1]
    # the note comes as the result of a check, not from the person (2026-09-27)
    assert nudge["role"] == "tool" and fake.calls[1][-2]["tool_calls"][0]["function"]["name"] == "check_facts"
    nudge = nudge["content"]
    assert nudge.startswith("[check] Not in any tool result: DE44 5002 0500 4500 4500 03.")
    assert "compute it with calculate" in nudge


def test_invented_twice_gives_the_honest_fallback(uid):
    fake = _FakeLlm([
        {"role": "assistant", "content": "Die Nummer ist 1234567890."},
        {"role": "assistant", "content": "Doch, es ist 1234567890."},
    ])
    out = _run(fake, user_id=uid)
    assert out["response"].startswith("Doch, es ist 1234567890 *(nicht belegt)*.")
    assert "in keiner Quelle gefunden" in out["response"]


def test_backed_value_and_quote_pass_with_sources(uid):
    fake = _FakeLlm([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("wa_tool", "{}")]},
        {"role": "assistant", "content": (
            "Mama schickte dir die DE32 5001 0517 5422 7163 31. Später schrieb sie:\n"
            "> „Ich musste nach einem Betrugsanruf das Konto wechseln“")},
    ])
    out = _run(fake, user_id=uid)
    assert "DE32 5001 0517 5422 7163 31" in out["response"]
    sources = [a for a in out["ui_actions"] if a.get("type") == "sources"]
    labels = [it["label"] for it in sources[0]["items"]]
    assert labels[0] == "WhatsApp · Mama Nowa · von Mama · Fr 09.01.2026 21:51"
    assert sources[0]["items"][0]["link"] == "/r/whatsapp?chat=4917@s.whatsapp.net"


def test_nudge_is_not_shown_in_the_chat(fresh_app):
    """The note to the model is stored with the conversation but the
    chat view leaves it out."""
    from tests.conftest import login_client
    client, uid = login_client(fresh_app, role="admin", name="Max", email="m@example.com")
    fake = _FakeLlm([
        {"role": "assistant", "content": "Die Nummer ist 1234567890."},
        {"role": "assistant", "content": "Das habe ich nicht gefunden."},
    ])
    out = _run(fake, user_id=uid)
    r = client.get(f"/api/conversations/{out['conversation_id']}")
    assert r.status_code == 200, r.text
    texts = [m.get("content") for m in r.json()["messages"]]
    assert not any((t or "").startswith("[check]") for t in texts)
    assert "Die Nummer ist 1234567890." not in texts


def test_plain_answers_are_untouched(uid):
    fake = _FakeLlm([{"role": "assistant", "content": "Morgen hast du zwei Termine – siehe unten."}])
    assert _run(fake, user_id=uid)["response"] == "Morgen hast du zwei Termine – siehe unten."


class _StreamLlm:
    """chat_stream yields the scripted texts word by word."""
    base_url = "http://fake-llm.local/v1"
    model = "fake-9b"

    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = []

    def chat_stream(self, messages, tools=None, **_kw):
        from types import SimpleNamespace as NS
        self.calls.append(list(messages))
        text = self._texts.pop(0) if self._texts else "ok"
        for i, word in enumerate(text.split(" ")):
            piece = word if i == 0 else " " + word
            yield NS(choices=[NS(delta=NS(content=piece, tool_calls=None), finish_reason=None)])
        yield NS(choices=[NS(delta=None, finish_reason="stop")])

    def chat(self, messages, tools=None, **_kw):
        return {"role": "assistant", "content": "", "_usage": None, "_finish_reason": "stop"}


def _stream(fake, uid):
    from backend.agent import loop, streaming
    from backend.agent.context import User
    from backend.agent.tools import ToolRegistry

    async def run():
        deltas, final = [], None
        async for ev in loop.ask_stream("welche kontonummer?", user=User(id=uid, role="admin", language="de"),
                                        registry=ToolRegistry(), llm=fake, system_prompt="test"):
            if isinstance(ev, streaming.TextDelta):
                deltas.append(ev.text)
            elif isinstance(ev, streaming.FinalResult):
                final = ev.response
        return "".join(deltas), final
    return asyncio.run(run())


def test_stream_holds_back_and_withdraws_an_invented_iban(uid):
    fake = _StreamLlm(["Die Nummer von Mama ist DE44 5002 0500 4500 4500 03.",
                       "Das habe ich nicht gefunden."])
    shown, final = _stream(fake, uid)
    assert "DE44" not in shown                      # never reached the screen or the speaker
    assert final["response"] == "Das habe ich nicht gefunden."


def test_stream_plain_text_still_streams(uid):
    fake = _StreamLlm(["Morgen hast du zwei Termine."])
    shown, final = _stream(fake, uid)
    assert shown == "Morgen hast du zwei Termine." and final["response"] == shown


def test_chip_names_the_user_as_sender():
    from backend.agent.grounding import check
    raw = [("whatsapp_read", {"chat": "Mama Nowa", "chat_jid": "4917@s.whatsapp.net",
                              "messages": [{"id": 5, "when": "Do 24.09.2026 19:39", "who": "ich",
                                            "text": "DE85500105175438012374"}]})]
    v = check("Die Nummer ist DE85 5001 0517 5438 0123 74.", [], raw)
    assert v.ok and v.sources[0]["label"] == "WhatsApp · Mama Nowa · von dir · Do 24.09.2026 19:39"


def test_fallback_drops_an_unbacked_quote_and_keeps_the_rest():
    from backend.agent.grounding import fallback_text
    out = fallback_text(["„Lass uns ins Kino gehen“"], "de",
                        "Jan hat sich gemeldet:\n> „Lass uns ins Kino gehen“\nEr klang gut gelaunt.")
    assert "Kino" not in out and "Jan hat sich gemeldet:" in out and "Er klang gut gelaunt." in out


def test_question_searches_everything_first(fresh_app, monkeypatch):
    """Prefetch: a question puts the universal search hits in front of
    the model before it chooses a tool (Dirk: "bei generellen suchen
    überall suchen")."""
    from backend import search_index
    from backend.agent.prefetch import should_search, HEADER
    from backend.database import get_conn
    from tests.test_search_foundation import _embed
    monkeypatch.setattr(search_index, "embed_many", _embed)
    monkeypatch.setattr(search_index, "EMBED_URL", "")
    uid = seed_user(name="Max", role="admin", email="max@example.com")
    jid = "491770000000@s.whatsapp.net"
    with get_conn() as conn:
        conn.execute("INSERT INTO wa_chats (jid, name, is_group, owner_user_id) VALUES (?, 'Mama Nowa', 0, ?)", (jid, uid))
        conn.execute("INSERT INTO wa_messages (msg_id, chat_jid, from_me, push_name, timestamp, text, owner_user_id) "
                     "VALUES ('m1', ?, 0, 'Mama', 1767990000, 'DE32500105175422716331', ?)", (jid, uid))
        conn.commit()
    search_index.sweep()
    assert should_search("welche kontonummer hat mama geschickt?")
    assert not should_search("trag das bitte ein") and not should_search("ja genau die")
    # agenda questions keep their own path with cards (e2e 2026-09-27)
    assert not should_search("Was muss ich heute noch machen?")
    assert not should_search("was hab ich am montag alles für termine?")
    fake = _FakeLlm([{"role": "assistant", "content": "Mama schickte DE32 5001 0517 5422 7163 31."}])
    from backend.agent import loop
    from backend.agent.context import User
    from backend.agent.tools import ToolRegistry
    out = asyncio.run(loop.ask("welche kontonummer hat mama bei whatsapp geschickt?",
                               user=User(id=uid, role="admin", language="de"), registry=ToolRegistry(),
                               llm=fake, system_prompt="test"))
    first_prompt = fake.calls[0]
    assert first_prompt[-1]["role"] == "tool" and first_prompt[-1]["content"].startswith(HEADER)
    assert "DE32 5001 0517 5422 7163 31" in out["response"]          # backed by the prefetch hit


def test_quote_with_a_remark_after_it_and_a_dash_between_pieces():
    """Rerun 2026-09-27: „€214.20 paid on … – Max plan - 20x“ (bezahlt mit
    Mastercard) was dropped although both pieces stand in the receipt."""
    from backend.agent.grounding import check
    receipt = [{"role": "tool", "content": "€214.20 paid on September 24, 2026\nItems\nMax plan - 20x 1 €180.00"}]
    ok = check("> „€214.20 paid on September 24, 2026 – Max plan - 20x“ (bezahlt mit Mastercard)", receipt)
    assert ok.ok, ok.missing
    assert not check("> „Max plan - 20x – €214.20 paid on September 24, 2026“", receipt).ok     # wrong order
    assert not check("> „€214.20 paid on September 24, 2026 – yearly plan“", receipt).ok        # invented piece
    # the model closed the line with a second mark after its remark
    assert check("> „€214.20 paid on September 24, 2026“ (bezahlt mit Mastercard)“", receipt).ok


def test_the_answer_does_not_talk_about_the_check(uid):
    """Rerun 2026-09-27: "Hinweis: Die Summe „366,44 €" taucht in keinem
    Tool-Ergebnis auf" reached Mama."""
    fake = _FakeLlm([
        {"role": "assistant", "content": "Zusammen 366,44 €."},
        {"role": "assistant", "content": "Das sind drei Zahlungen. Hinweis: Die Summe 366,44 € taucht in keinem "
                                         "Tool-Ergebnis auf. Soll ich genauer schauen?"},
    ])
    out = _run(fake, user_id=uid)
    assert "Tool-Ergebnis" not in out["response"]
    assert out["response"].startswith("Das sind drei Zahlungen. Soll ich genauer schauen?")


def test_two_dropped_quote_lines_leave_one_note_and_no_empty_quote_lines():
    from backend.agent.grounding import fallback_text
    answer = "Er schrieb:\n\n> Hey Dirk\n> \n> erste Zeile erfunden\n> \n> zweite Zeile erfunden\n> \n> Grüße\n\nNoch was?"
    out = fallback_text(["„erste Zeile erfunden“", "„zweite Zeile erfunden“"], "de", answer)
    assert out.count("_(Zitat nicht belegt, weggelassen)_") == 1
    assert "> Hey Dirk\n_(Zitat nicht belegt, weggelassen)_\n> Grüße" in out


OLIVER = ("Hey Dirk :)\n\n\nwas meinst du konkret mit „monetär\"?\n\n\nDie Akademie ist kostenlos - es gibt lediglich "
          "weitere Inhalte (wie\nVorlagen für die Datenschutzerklärung, Live Calls, WhatsApp Zugabg zu mir),\n"
          "die allerdings nur für Leute freigeschaltet sind, die sich über meinen\nAffiliate Link für HighLevel anmelden.")


def test_a_quote_written_almost_right_is_copied_from_the_mail():
    """2026-09-28: the model wrote "Zugang" for Oliver's "Zugabg"; the
    quote was dropped. Now the mail's own words stand there."""
    from backend.agent.grounding import check
    mail = [{"role": "tool", "content": json.dumps({"message": {"body_text": OLIVER}}, ensure_ascii=False)}]
    answer = ("Er schrieb:\n> „Die Akademie ist kostenlos – es gibt lediglich weitere Inhalte (wie Vorlagen für die "
              "Datenschutzerklärung, Live Calls, WhatsApp Zugang zu mir)“\nMehr nicht.")
    v = check(answer, mail)
    assert v.ok, v.missing
    assert "WhatsApp Zugabg zu mir)" in v.text and "Zugang" not in v.text
    assert v.text.startswith("Er schrieb:\n> „Die Akademie ist kostenlos - es gibt lediglich weitere Inhalte (wie Vorlagen")


def test_a_changed_number_or_a_different_passage_is_not_copied():
    from backend.agent.grounding import copy_from_sources
    src = ["Invoice amount 551,07 EUR. The amount of the invoice will be credited to your customer account."]
    assert copy_from_sources("Invoice amount 551,08 EUR. The amount of the invoice", src) is None
    assert copy_from_sources("Die Rechnung ist schon lange bezahlt worden", src) is None
    assert copy_from_sources("Invoice amount 551,07 EUR. The amount of the invoce", src) == \
        "Invoice amount 551,07 EUR. The amount of the invoice"


def test_weekday_abbreviations_and_weekday_after_the_date():
    from datetime import date
    from backend.agent.grounding import fix_weekdays
    today = date(2026, 9, 28)
    assert fix_weekdays("am 22. September (Sa)", today) == "am 22. September (Di)"
    assert fix_weekdays("So, 10.10.", today) == "Sa, 10.10."
    assert fix_weekdays("So ist das 10.10.", today) == "So ist das 10.10."
