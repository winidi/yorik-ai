"""Tests for the compute_group_price skill.

Pure-math skill — no LLM, no DB. The point of the skill exists
precisely because LLM math on German decimal commas is unreliable, so
the contract here is: any reasonable input shape works and the total is
exact to the cent.
"""

from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def execute():
    from backend.skills.compute_group_price.skill import execute
    return execute


@pytest.fixture(autouse=True)
def _stub_ui_append(monkeypatch):
    """Don't try to talk to the real UI sink from a unit test."""
    captured: list[dict] = []
    import backend.ui_tools as ui
    monkeypatch.setattr(ui, "_append", lambda card: captured.append(card))
    return captured


def test_simple_eur_adds_up(execute):
    res = asyncio.run(execute(
        ctx=None,
        items=[
            {"label": "Erwachsen", "unit_eur": 5.5, "count": 2},
            {"label": "Kind",      "unit_eur": 3.0, "count": 3},
        ],
    ))
    assert res["ok"] is True
    assert res["total_eur"] == 20.00
    assert res["total_count"] == 5


def test_german_decimal_comma_strings(execute):
    """The skill exists because LLMs mangle '5,90' as a number. The
    parser must handle it."""
    res = asyncio.run(execute(
        ctx=None,
        items=[
            {"label": "Familienkarte", "unit_eur": "12,50", "count": 1},
            {"label": "Kind",          "unit_eur": "3,70",  "count": 2},
        ],
    ))
    assert res["total_eur"] == pytest.approx(19.90, abs=0.005)


def test_currency_symbol_stripped(execute):
    res = asyncio.run(execute(
        ctx=None,
        items=[{"label": "Eintritt", "unit_eur": "€ 8,00", "count": 1}],
    ))
    assert res["total_eur"] == 8.00


def test_thousands_separator_with_decimal_comma(execute):
    """'1.234,56' must parse as 1234.56 (German style), not 1.23456."""
    res = asyncio.run(execute(
        ctx=None,
        items=[{"label": "Schaden", "unit_eur": "1.234,56", "count": 1}],
    ))
    assert res["total_eur"] == 1234.56


def test_zero_count_skipped(execute):
    res = asyncio.run(execute(
        ctx=None,
        items=[
            {"label": "Erwachsen", "unit_eur": 5,    "count": 0},
            {"label": "Kind",      "unit_eur": 3.0,  "count": 2},
        ],
    ))
    assert res["total_eur"] == 6.00
    assert res["total_count"] == 2
    assert all(li["label"] != "Erwachsen" for li in res["line_items"])


def test_empty_items_returns_helpful_hint(execute):
    res = asyncio.run(execute(ctx=None, items=[]))
    assert res["ok"] is False
    assert "compute_group_price" in res["_llm_hint"]
    assert res["total_eur"] == 0.0


def test_non_list_items_returns_helpful_hint(execute):
    res = asyncio.run(execute(ctx=None, items="not a list"))  # type: ignore[arg-type]
    assert res["ok"] is False


def test_alias_field_names_accepted(execute):
    """LLM sometimes emits 'price'/'amount'/'qty' instead of canonical
    names. The parser is liberal — these aliases must work."""
    res = asyncio.run(execute(
        ctx=None,
        items=[
            {"label": "A", "price":  4.0, "qty": 1},
            {"label": "B", "amount": 2.5, "count": 2},
        ],
    ))
    assert res["total_eur"] == 9.00


def test_emits_price_summary_card(execute, _stub_ui_append):
    asyncio.run(execute(
        ctx=None,
        items=[{"label": "X", "unit_eur": 1.0, "count": 1}],
        title="Stadtbad",
        source_url="https://example.org/preise",
    ))
    assert len(_stub_ui_append) == 1
    card = _stub_ui_append[0]
    assert card["type"] == "price_summary"
    assert card["title"] == "Stadtbad"
    assert card["source_url"] == "https://example.org/preise"
    assert card["total_eur"] == 1.00


def test_llm_hint_carries_source_url(execute):
    res = asyncio.run(execute(
        ctx=None,
        items=[{"label": "A", "unit_eur": 5.0, "count": 1}],
        source_url="https://example.org/x",
    ))
    assert "example.org" in res["_llm_hint"]
    assert res["_llm_hint"].startswith("shown_to_user:")
