"""Recurring payments found by code (chat test #7, 2026-09-28: the model
guessed payees and never asked for Hetzner)."""

from __future__ import annotations

from tests.conftest import seed_user


def test_subscriptions_are_found_groceries_and_one_offs_are_not(fresh_app):
    from backend import recurring, spaces
    from backend.database import get_conn
    from backend.skills.registry import Registry, SkillContext
    uid = seed_user(name="Dirk", role="admin", email="d@example.com")
    spaces.ensure_workspace_exists(uid, "Dirk")
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO bank_accounts (owner_user_id, space_id, display_name, bank_url, blz, login_name, "
                           "credential_key) VALUES (?, NULL, 'Giro', 'https://example.invalid', '0', 'x', 'u') RETURNING id",
                           (uid,)).fetchone()["id"]
        rows = [
            ("2026-07-07", -44.49, "VISA HETZNER ONLINE GMBH", ""), ("2026-08-06", -74.49, "VISA HETZNER ONLINE GMBH", ""),
            ("2026-09-08", -74.49, "VISA HETZNER ONLINE GMBH", ""),                       # upgraded, then steady
            ("2026-07-21", -107.10, "VISA ANTHROPIC* CLAUDE SUB", ""), ("2026-08-19", -107.10, "VISA ANTHROPIC* CLAUDE SUB", ""),
            ("2026-08-26", -130.82, "VISA ANTHROPIC* CLAUDE SUB", ""),                    # an extra booking
            ("2026-07-03", -17.26, "PENNY", ""), ("2026-08-02", -28.18, "PENNY", ""), ("2026-09-01", -53.42, "PENNY", ""),
            ("2026-08-25", -23.80, "VISA ANTHROPIC", ""),                                 # once
            ("2026-08-06", -0.51, "Kleingeld Plus - Sparen", "Aus Kauf74,49. beiHETZNER"),
            ("2026-08-06", -0.51, "Kleingeld Plus - Sparen", "Aus Kauf74,49. beiHETZNER"),
            ("2026-09-08", -0.51, "Kleingeld Plus - Sparen", "Aus Kauf74,49. beiHETZNER"),
            ("2026-07-01", 2500.0, "Arbeitgeber", "Gehalt"), ("2026-08-01", 2500.0, "Arbeitgeber", "Gehalt"),
        ]
        for n, (d, a, cp, pu) in enumerate(rows):
            conn.execute("INSERT INTO bank_transactions (account_id, booking_date, amount, counterparty, purpose, category, "
                         "dedup_hash) VALUES (?, ?, ?, ?, ?, 'Abos', ?)", (acc, d, a, cp, pu, f"h{n}"))
        conn.commit()
    out = recurring.find(SkillContext(Registry(), role="admin", user_id=uid), until="2026-09-28")
    got = {p["payee"]: (p["rhythm"], p["regular_amount"], p["per_month"]) for p in out["recurring"]}
    assert got == {"Anthropic* Claude Sub": ("monthly", "107,10 €", "107,10 €"),
                   "Hetzner Online Gmbh": ("monthly", "74,49 €", "74,49 €")}
    assert out["per_month_total"] == "181,59 €" and out["bank_records_from"] == "2026-07-01"


def test_payee_key():
    from backend.recurring import payee_key
    assert payee_key("VISA HETZNER ONLINE GMBH   ") == "HETZNER ONLINE GMBH"
    assert payee_key("SEPA Vodafone GmbH 1234567") == "VODAFONE GMBH"


def test_the_skill_says_since_when(fresh_app):
    import asyncio
    from backend.skills.recurring_payments.skill import execute
    from backend.skills.registry import Registry, SkillContext
    uid = seed_user(name="Beate", role="member", email="b@example.com")
    out = asyncio.run(execute(SkillContext(Registry(), role="member", user_id=uid)))
    assert out["recurring"] == [] and "no bank bookings for this person" in out["_llm_hint"]


def test_new_descriptions_fit_in_95_characters():
    """The description is shown on every turn (Dirk 2026-09-28: max 95)."""
    import re
    from pathlib import Path
    for name in ("payments_to", "recurring_payments"):
        text = Path(f"backend/skills/{name}/skill.md").read_text()
        desc = re.search(r"^description: (.*)$", text, re.M).group(1).strip().strip('"')
        assert len(desc) <= 95, (name, len(desc))
