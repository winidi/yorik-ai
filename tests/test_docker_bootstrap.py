"""First start of the Docker install: the three connections run side by
side (the model download no longer waits for the archive, Immich's
admin is claimed as soon as Immich answers), and Home learns how far
the model download is."""

import json

from tests.conftest import login_client


def test_steps_run_side_by_side_and_one_failure_stops_none(monkeypatch):
    import threading
    from backend import docker_bootstrap as B
    started, gate = [], threading.Barrier(3, timeout=5)

    def step(name, fail=False):
        def f():
            started.append(name)
            gate.wait()                      # only passes if all three run at once
            if fail:
                raise RuntimeError("boom")
        f.__name__ = name
        return f
    monkeypatch.setattr(B, "immich", step("immich", fail=True))
    monkeypatch.setattr(B, "ollama", step("ollama"))
    monkeypatch.setattr(B, "paperless", step("paperless"))
    B.main()
    assert sorted(started) == ["immich", "ollama", "paperless"]


def test_pull_lines_become_a_percentage():
    from backend.docker_bootstrap import _percent
    assert _percent('{"status":"pulling abc","completed":250,"total":1000}') == 25
    assert _percent('{"status":"verifying sha256 digest"}') is None
    assert _percent("not json") is None


def test_home_sees_the_download(fresh_app):
    from backend.docker_bootstrap import _progress
    client, _ = login_client(fresh_app, role="admin", name="Anna")
    _progress("qwen:9b", "downloading", 42)
    assert client.get("/api/system/status").json()["llm"]["download"]["percent"] == 42
    _progress("qwen:9b", "ready", 100)
    assert "download" not in client.get("/api/system/status").json()["llm"]
