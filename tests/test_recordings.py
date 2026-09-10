"""Recordings: chunk upload, pipeline (mocked models), participant-only
visibility, the three skills, retention."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import numpy as np
import pytest

from tests.conftest import login_client, seed_user


@pytest.fixture
def rec(monkeypatch, tmp_path):
    """Recordings module with the models replaced by deterministic fakes:
    audio decodes to 10 s of 'signal', the diariser returns two clusters,
    cluster 0 is Beate's enrolled voice, transcription echoes the window."""
    from backend import recordings as R
    monkeypatch.setattr(R, "ROOT", tmp_path / "recordings")
    monkeypatch.setattr(R, "decode_audio", lambda path: np.ones(10 * R.SAMPLE_RATE, dtype=np.float32))
    monkeypatch.setattr(R, "diarize", lambda audio: [
        {"start": 0.0, "end": 2.0, "cluster": 0},
        {"start": 2.2, "end": 4.0, "cluster": 1},
        {"start": 4.3, "end": 5.0, "cluster": 1},   # same speaker, gap < 1 s → one turn
        {"start": 6.0, "end": 8.0, "cluster": 0},
        {"start": 8.1, "end": 8.2, "cluster": 2},   # too short, dropped
    ])
    monkeypatch.setattr(R, "transcribe_segment", lambda audio: f"{len(audio) // R.SAMPLE_RATE}s")
    calls = {"identify": []}

    def fake_identify(audio, segs, candidates):
        calls["identify"].append(list(candidates))
        return {0: {"user_id": calls["beate"], "name": "Beate", "similarity": 0.8}} if calls.get("beate") in candidates else {}
    monkeypatch.setattr(R, "identify_clusters", fake_identify)
    monkeypatch.setattr(R, "schedule_processing", lambda rid: R._process_safe(rid))
    monkeypatch.setattr(R, "AUTO_REPORT_KINDS", ())     # reports have their own tests
    return calls


def _upload(client, rid, seq, data=b"webm-bytes"):
    return client.post(f"/api/recordings/{rid}/chunk", data={"seq": str(seq)},
                       files={"audio": (f"c{seq}.webm", data, "audio/webm")})


def test_record_process_and_participant_visibility(fresh_app, rec):
    from backend import recordings as R
    from backend import notifications as N
    client, dirk = login_client(fresh_app, role="admin", name="Dirk")
    with R.get_conn() as conn:                                     # labels follow the recorder's language
        conn.execute("UPDATE user_profiles SET language = 'de' WHERE id = ?", (dirk,)); conn.commit()
    beate = seed_user(name="Beate", role="member", password="pytestpw123")
    kid = seed_user(name="Kid", role="restricted", password="pytestpw123")
    other_admin = seed_user(name="Other Admin", role="admin", password="pytestpw123")
    rec["beate"] = beate

    r = client.post("/api/recordings", json={"title": "Abendessen", "kind": "dinner", "participants": [beate, kid]})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["status"] == "recording" and [p["name"] for p in r.json()["participants"]] == ["Beate", "Kid"]
    assert client.post("/api/recordings", json={"participants": ["00000000-0000-0000-0000-000000000000"]}).status_code == 400

    assert _upload(client, rid, 0).json()["chunks"] == 1
    assert _upload(client, rid, 1).json()["chunks"] == 2
    assert client.post(f"/api/recordings/{rid}/finish", json={"duration_s": 10}).status_code == 200
    assert (R.rec_dir(rid) / "audio.webm").read_bytes() == b"webm-byteswebm-bytes"
    assert not list(R.rec_dir(rid).glob("chunk-*"))
    assert _upload(client, rid, 2).status_code == 409           # no more audio after finish

    row = client.get(f"/api/recordings/{rid}").json()
    assert row["status"] == "done" and row["duration_s"] == 10.0 and row["audio_available"] is True
    t = client.get(f"/api/recordings/{rid}/transcript").json()
    speakers = [s["speaker"] for s in t["segments"]]
    assert speakers == ["Beate", "Sprecher 1", "Beate"]           # merged turn, short one dropped, Beate identified
    assert t["segments"][0]["user_id"] == beate and t["segments"][1]["user_id"] is None
    assert t["segments"][1]["start_s"] == 2.2 and t["segments"][1]["end_s"] == 5.0
    assert t["text"].startswith("[00:00] Beate: 2s\n[00:02] Sprecher 1: 1s 0s")
    assert set(rec["identify"][0]) == {beate, kid, dirk}          # only the people at the table are candidates

    # everyone at the table got a bell entry; nobody else
    assert [n["title"] for n in N.list_for_user(beate)] == ["Abendessen: transcript ready"]
    assert N.list_for_user(other_admin) == []

    # participants see it (member and restricted), a non-participant admin does not
    from backend import auth_sessions
    from fastapi.testclient import TestClient
    for uid, expect in ((beate, 200), (kid, 200), (other_admin, 404)):
        c = TestClient(fresh_app)
        c.cookies.set(auth_sessions.COOKIE_NAME, auth_sessions.create_session(uid, user_agent="t", ip="127.0.0.1"))
        assert c.get(f"/api/recordings/{rid}").status_code == expect, uid
        assert c.get(f"/api/recordings/{rid}/transcript").status_code == expect
        assert c.get(f"/api/recordings/{rid}/audio").status_code == (200 if expect == 200 else 404)
        assert (c.get("/api/recordings").json()["recordings"] != []) == (expect == 200)
        assert c.delete(f"/api/recordings/{rid}").status_code == (403 if expect == 200 else 404)
    assert client.delete(f"/api/recordings/{rid}").json() == {"ok": True}
    assert client.get(f"/api/recordings/{rid}").status_code == 404
    assert not R.rec_dir(rid).exists()


def test_restricted_cannot_start_and_failures_are_recorded(fresh_app, rec, monkeypatch):
    from backend import recordings as R
    kid_client, _ = login_client(fresh_app, role="restricted", name="Kid")
    assert kid_client.post("/api/recordings", json={}).status_code == 403

    client, _ = login_client(fresh_app, role="member", name="Beate2")
    rid = client.post("/api/recordings", json={}).json()["id"]
    assert client.post(f"/api/recordings/{rid}/finish").status_code == 400     # nothing uploaded
    _upload(client, rid, 0)
    monkeypatch.setattr(R, "diarize", lambda audio: (_ for _ in ()).throw(RuntimeError("model missing")))
    client.post(f"/api/recordings/{rid}/finish")
    row = client.get(f"/api/recordings/{rid}").json()
    assert row["status"] == "failed" and "model missing" in row["error"]
    # a failed recording can be finished again once the problem is fixed
    monkeypatch.setattr(R, "diarize", lambda audio: [{"start": 0.0, "end": 3.0, "cluster": 0}])
    assert client.post(f"/api/recordings/{rid}/finish").json()["status"] == "done"


def test_skills_start_finish_status(fresh_app, rec):
    from backend import recordings as R
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.start_recording.skill import execute as start
    from backend.skills.finish_recording.skill import execute as finish
    from backend.skills.recording_status.skill import execute as status
    from backend.ui_tools import _pending_ui_actions
    dirk = seed_user(name="Dirk", role="admin", password="pytestpw123", language="de")
    beate = seed_user(name="Beate", role="member", password="pytestpw123")
    rec["beate"] = beate
    ctx = SkillContext(Registry(), role="admin", user_id=dirk, conversation_id="c1")

    def run(coro):
        """asyncio.run copies the context, so read the ui_actions inside."""
        async def _w():
            _pending_ui_actions.set([])
            out = await coro
            return out, _pending_ui_actions.get()
        return asyncio.run(_w())

    assert asyncio.run(status(ctx))["status"] == "none"
    out, actions = run(start(ctx, kind="dinner", participants="Beate und Onkel Karl"))
    rid = out["recording_id"]
    assert [p["name"] for p in out["participants"]] == ["Beate"] and out["unknown"] == ["Onkel Karl"]
    assert out["title"].startswith("Dinner ")
    assert actions[-1] == {"type": "start_recording", "recording_id": rid, "title": out["title"]}
    assert asyncio.run(start(ctx))["already_running"] is True
    assert asyncio.run(status(ctx))["status"] == "recording"

    # from chat, finishing only asks the device to stop; the device finishes
    R.add_chunk(rid, 0, b"x")
    out, actions = run(finish(ctx))
    assert out["stop_requested"] is True and R.public(R._row(rid))["stop_requested"] is True
    assert actions[-1] == {"type": "stop_recording", "recording_id": rid}
    # over MCP (token source) the audio is complete on the server → finish for real
    tctx = SkillContext(Registry(), role="admin", user_id=dirk, conversation_id="c2", source="token:hermes")
    assert asyncio.run(finish(tctx))["status"] == "done"
    assert asyncio.run(finish(ctx))["status"] == "none"

    st = asyncio.run(status(ctx))
    assert st["status"] == "done" and st["speakers"] == ["Beate", "Sprecher 1"] and "[00:00] Beate:" in st["transcript"]
    bctx = SkillContext(Registry(), role="member", user_id=beate, conversation_id="c3")
    assert asyncio.run(status(bctx, recording_id=rid))["turns"] == 3
    stranger = SkillContext(Registry(), role="admin", user_id=seed_user(name="Stranger", role="admin"), conversation_id="c4")
    with pytest.raises(ValueError):
        asyncio.run(status(stranger, recording_id=rid))


def test_retention_deletes_audio_but_keeps_transcript(fresh_app, rec):
    from backend import recordings as R
    client, _ = login_client(fresh_app, role="member", name="Beate3")
    rid = client.post("/api/recordings", json={}).json()["id"]
    _upload(client, rid, 0)
    client.post(f"/api/recordings/{rid}/finish")
    stale = client.post("/api/recordings", json={}).json()["id"]          # started, never finished
    with R.get_conn() as conn:
        conn.execute("UPDATE recordings SET started_at = ? WHERE id = ?",
                     ((datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"), stale))
        conn.commit()
    assert R.purge_audio(datetime.now()) == 1                              # only the stale one
    assert R.purge_audio(datetime.now() + timedelta(days=R.RETENTION_DAYS + 1)) == 1
    row = client.get(f"/api/recordings/{rid}").json()
    assert row["audio_available"] is False and row["status"] == "done"
    assert client.get(f"/api/recordings/{rid}/audio").status_code == 410
    assert len(client.get(f"/api/recordings/{rid}/transcript").json()["segments"]) == 3
    assert client.get(f"/api/recordings/{stale}").json()["status"] == "failed"


def test_label_and_merge_helpers():
    from backend import recordings as R
    segs = [{"start": 0, "end": 1, "cluster": 5}, {"start": 1.5, "end": 2, "cluster": 3}, {"start": 2.2, "end": 3, "cluster": 5}]
    labels = R._label_clusters(segs, {3: {"user_id": "u", "name": "Dirk"}})
    assert labels[5]["label"].endswith(" 1") and labels[3] == {"label": "Dirk", "user_id": "u"}
    turns = R._merge_turns(segs, labels)
    assert [t["label"] for t in turns] == [labels[5]["label"], "Dirk", labels[5]["label"]]
