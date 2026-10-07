"""The index runs on a schedule and the embedding service sleeps between
runs (Dirk 2026-10-07: no model holding RAM around the clock on small
PCs). No docker in tests: the controller is stubbed."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Europe/Berlin")


@pytest.fixture
def house(fresh_app, monkeypatch):
    from tests.conftest import login_client
    from backend import search_index as si
    monkeypatch.setenv("YORIK_TZ", "Europe/Berlin")
    monkeypatch.setattr(si, "EMBED_URL", "")          # whatever this box's config.env says: no service here
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    return {"dirk": (dirk_c, dirk), "beate": (beate_c, beate)}


def test_next_run_follows_the_schedule(house, monkeypatch):
    from backend import search_index as si
    from backend.household_settings import set_setting
    now = datetime(2026, 10, 7, 14, 20, tzinfo=TZ)
    assert si.schedule() == "continuous" and si.next_run(now) is None           # bundled: continuous
    set_setting(si.SETTING_SCHEDULE, "hourly")
    assert si.next_run(now) == datetime(2026, 10, 7, 15, 0, tzinfo=TZ)
    set_setting(si.SETTING_SCHEDULE, "nightly")
    assert si.run_at() == "03:00" and si.next_run(now) == datetime(2026, 10, 8, 3, 0, tzinfo=TZ)
    set_setting(si.SETTING_AT, "23:15")
    assert si.next_run(now) == datetime(2026, 10, 7, 23, 15, tzinfo=TZ)
    set_setting(si.SETTING_AT, "nonsense")
    assert si.run_at() == "03:00"
    # the service defaults to nightly, the bundled embedder to continuous
    set_setting(si.SETTING_SCHEDULE, "")
    monkeypatch.setattr(si, "EMBED_URL", "http://127.0.0.1:8093/v1")
    assert si.schedule() == "nightly" and si.keep_loaded() == "while_used"


def test_settings_api_validates_and_only_admins_change(house):
    dirk_c, _ = house["dirk"]; beate_c, _ = house["beate"]
    assert beate_c.put("/api/search/index", json={"schedule": "hourly"}).status_code == 403
    assert dirk_c.put("/api/search/index", json={"schedule": "weekly"}).status_code == 400
    assert dirk_c.put("/api/search/index", json={"at": "25:99"}).status_code == 400
    assert dirk_c.put("/api/search/index", json={"keep": "never"}).status_code == 400
    st = dirk_c.put("/api/search/index", json={"schedule": "nightly", "at": "02:30", "keep": "always"}).json()
    assert st["schedule"] == "nightly" and st["at"] == "02:30" and st["keep"] == "always"
    assert st["next_run"] and st["next_run"][11:16] == "02:30" and "pending" in st
    assert dirk_c.post("/api/search/index/run").json() == {"ok": True}
    assert dirk_c.get("/api/search/index").json()["service"]["state"] == "unmanaged"   # no service configured


def test_sleeping_service_is_woken_not_waited_for(house, monkeypatch):
    """A search while the service sleeps stays keyword-only and starts it
    in the background; a sweep with nothing to do leaves it asleep; a
    scheduled window starts it and stops it after."""
    from backend import search_index as si, search_embedder as se
    monkeypatch.setattr(si, "EMBED_URL", "http://127.0.0.1:1/v1")           # nothing listens
    monkeypatch.setattr(se, "docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(se, "reachable", lambda: False)
    started = []
    monkeypatch.setattr(se, "start", lambda wait=True: started.append(("start", wait)) or False)
    monkeypatch.setattr(se, "start_in_background", lambda: started.append(("background", None)))
    stopped = []
    monkeypatch.setattr(se, "stop", lambda: stopped.append(True) or True)
    assert si.embed_query("wo ist die rechnung") is None
    assert started == [("background", None)]
    started.clear()
    # nothing pending: the sweep does not wake the service
    monkeypatch.setattr(si, "pending", lambda: 0)
    assert si.sweep() == {}
    assert started == []
    # a window (scheduled run) starts it, and the start failing marks every source
    out = si.sweep(window=True)
    assert started == [("start", True)] and all(n == -1 for n in out.values())
    # with the service up, a window ends with a stop (while_used, nightly)
    from backend.household_settings import set_setting
    set_setting(si.SETTING_SCHEDULE, "nightly")
    monkeypatch.setattr(se, "reachable", lambda: True)
    monkeypatch.setattr(si, "_sweep_sources", lambda out: out)
    si.sweep(window=True)
    assert stopped == [True]
    stopped.clear()
    set_setting(si.SETTING_KEEP, "always")
    si.sweep(window=True)
    assert stopped == []


def test_idle_stop_and_bundled_unload(house, monkeypatch):
    from backend import search_embedder as se
    from backend.embedders import local
    import time
    monkeypatch.setattr(se, "docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(se, "reachable", lambda: True)
    stopped = []
    monkeypatch.setattr(se, "stop", lambda: stopped.append(True) or True)
    se.touch()
    assert se.maybe_stop_idle("while_used") is False                  # just used
    monkeypatch.setattr(se, "_last_used", time.time() - se.IDLE_STOP_S - 1)
    assert se.maybe_stop_idle("always") is False and stopped == []
    assert se.maybe_stop_idle("while_used") is True and stopped == [True]
    se.sweeping(True)
    monkeypatch.setattr(se, "_last_used", time.time() - se.IDLE_STOP_S - 1)
    assert se.maybe_stop_idle("while_used") is False                  # never during a pass
    se.sweeping(False)
    # the bundled model: unloaded after idle, nothing loaded → nothing to do
    assert local.maybe_unload() is False
    monkeypatch.setattr(local, "_session", object())
    monkeypatch.setattr(local, "_last_used", time.time() - local.IDLE_UNLOAD_S - 1)
    assert local.maybe_unload() is True and local.loaded() is False
