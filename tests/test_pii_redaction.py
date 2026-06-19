"""PII-redaction policy tests for backend.skills._web_helpers.

The rule we enforce: only MULTI-WORD phrases get redacted. Single-word
generic terms (towns like 'Hannover', common surnames like 'Müller')
survive so real queries don't get nuked. See _pii_phrases() docstring
for the rationale — and docs/PRIVACY.md for the user-facing version.

These tests pin both the redaction policy (what gets removed) AND the
non-redaction policy (what must NOT get removed, because over-redaction
breaks legitimate queries).
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def seeded_user(fresh_app):
    """Insert a user with a full name + street and two multi-word
    contacts. Returns the user_id."""
    from backend.database import DEFAULT_DB_PATH, conn_ctx
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO user_profiles "
            "(name, email, role, voice_id, first_name, last_name, address_street) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Hans Becker", "hans@example.com", "admin", "vid",
             "Hans", "Becker", "Hauptstrasse 12"),
        )
        uid = cur.lastrowid
        for display, status in [
            ("Hausverwaltung Müller GmbH", "active"),
            ("Lena Hoffmann",              "active"),
            ("Marco",                      "active"),
        ]:
            conn.execute(
                "INSERT INTO contacts (display_name, status, kind) VALUES (?, ?, ?)",
                (display, status, "person"),
            )
        conn.commit()
    return uid


def test_full_name_phrase_is_redacted(seeded_user):
    from backend.skills._web_helpers import redact_pii
    q = "Steuerberater für Hans Becker in Hannover gesucht"
    out, removed = redact_pii(q, seeded_user)
    assert "Hans Becker" not in out
    assert "Hannover" in out
    assert "Steuerberater" in out
    assert "Hans Becker" in removed


def test_single_first_name_is_kept(seeded_user):
    """'Anna' alone could be a band, a town, a question — too generic
    to redact safely. Single-word forms must survive."""
    from backend.skills._web_helpers import redact_pii
    q = "Geschenk für Hans 30 Geburtstag"
    out, removed = redact_pii(q, seeded_user)
    assert "Hans" in out
    assert removed == []


def test_single_word_contact_is_kept(seeded_user):
    """Marco is a single-word contact — must NOT be redacted, otherwise
    a thousand-contact address book would nuke nearly any query."""
    from backend.skills._web_helpers import redact_pii
    q = "Pizza für Marco bestellen"
    out, removed = redact_pii(q, seeded_user)
    assert "Marco" in out
    assert removed == []


def test_multi_word_contact_phrase_is_redacted(seeded_user):
    from backend.skills._web_helpers import redact_pii
    q = "Brief an Hausverwaltung Müller GmbH wegen Heizung"
    out, removed = redact_pii(q, seeded_user)
    assert "Hausverwaltung Müller GmbH" not in out
    assert "Heizung" in out
    assert "Hausverwaltung Müller GmbH" in removed


def test_street_address_phrase_is_redacted(seeded_user):
    from backend.skills._web_helpers import redact_pii
    q = "Pizza Lieferung Hauptstrasse 12"
    out, removed = redact_pii(q, seeded_user)
    assert "Hauptstrasse 12" not in out
    assert "Pizza" in out
    assert "Hauptstrasse 12" in removed


def test_case_insensitive_phrase_match(seeded_user):
    from backend.skills._web_helpers import redact_pii
    q = "HANS BECKER Steuererklärung"
    out, _ = redact_pii(q, seeded_user)
    assert "HANS BECKER" not in out and "Hans Becker" not in out
    assert "Steuererklärung" in out


def test_extra_whitespace_between_phrase_words_still_matches(seeded_user):
    """'Hans  Becker' (double space) should still be redacted — phrase
    matcher allows internal whitespace flexibility."""
    from backend.skills._web_helpers import redact_pii
    q = "Mail von Hans  Becker beantworten"
    out, removed = redact_pii(q, seeded_user)
    assert "Hans  Becker" not in out
    assert "Hans Becker" in removed


def test_no_phrases_for_unknown_user_does_not_crash(fresh_app):
    """Calling redact_pii with a non-existent user_id should return the
    query unchanged, no exception."""
    from backend.skills._web_helpers import redact_pii
    out, removed = redact_pii("anything goes here", 999_999)
    assert out == "anything goes here"
    assert removed == []
