"""A day's briefing snapshot is one person's day. It used to be one
row per date, taken as the first admin and served to everyone who
asked for a past date (audit docs/audits/2026-09-22-berechtigungen.md,
2.8)."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from tests.conftest import login_client


def test_snapshots_are_kept_and_served_per_person(fresh_app, monkeypatch):
    from backend import briefing_snapshots as BS, briefings as B
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    async def fake_run(template_id, user_id, role, for_date=None, **kw):
        return {"template": template_id, "sections": [{"title": f"Tag von {user_id}"}]}
    monkeypatch.setattr(B, "run_briefing", fake_run)

    asyncio.run(BS.capture_snapshot("day-recap", yesterday, user_id=dirk, role="platform_admin"))
    assert BS.get_snapshot("day-recap", yesterday, dirk)["sections"][0]["title"] == f"Tag von {dirk}"
    assert BS.get_snapshot("day-recap", yesterday, beate) is None                  # not hers
    assert BS.list_snapshot_dates(dirk) == [yesterday] and BS.list_snapshot_dates(beate) == []
    # re-capturing the same day overwrites, and Beate's own is separate
    asyncio.run(BS.capture_snapshot("day-recap", yesterday, user_id=dirk, role="platform_admin"))
    asyncio.run(BS.capture_snapshot("day-recap", yesterday, user_id=beate, role="member"))
    assert BS.get_snapshot("day-recap", yesterday, beate)["sections"][0]["title"] == f"Tag von {beate}"

    # the routes: Beate asks for yesterday and gets her own recap, and only her dates
    r = beate_c.get("/api/briefings/day?period=yesterday").json()
    assert r["sections"][0]["title"] == f"Tag von {beate}" and r["target_date"] == yesterday
    assert beate_c.get("/api/briefings/snapshots/dates").json() == {"dates": [yesterday]}
    # the scheduler snapshots every enabled person, not "the admin"
    assert {uid for uid, _ in BS._people()} >= {dirk, beate}
