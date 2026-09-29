"""Where a backup reads from. The classic probe called itself forever
(since 2026-09-26), and in the Docker stack photos and documents were
never in the backup: they live in the Immich / Paperless volumes."""

import os

from backend import backup as B


def test_probe_asks_docker_on_a_classic_install(monkeypatch):
    monkeypatch.delenv("YORIK_RUNTIME", raising=False)
    assert B._probe_argv("supabase-db") == ["docker", "inspect", "-f", "{{.State.Running}}", "supabase-db"]
    monkeypatch.setenv("YORIK_RUNTIME", "docker")
    assert B._probe_argv("supabase-db") == ["echo", "true"]


def test_media_dirs_follow_the_docker_mounts(monkeypatch, tmp_path):
    monkeypatch.delenv("YORIK_PHOTOS_DIR", raising=False)
    assert B._media_dir("YORIK_PHOTOS_DIR", tmp_path / "classic") == tmp_path / "classic"
    monkeypatch.setenv("YORIK_PHOTOS_DIR", "/media/photos")
    assert str(B._media_dir("YORIK_PHOTOS_DIR", tmp_path / "classic")) == "/media/photos"


def test_one_unreadable_file_does_not_cost_the_other_photos(tmp_path):
    src = tmp_path / "library"
    (src / "2026").mkdir(parents=True)
    (src / "2026" / "a.jpg").write_bytes(b"a")
    locked = src / "2026" / "b.jpg"
    locked.write_bytes(b"b")
    os.chmod(locked, 0)
    try:
        B._copy_tree(src, tmp_path / "out")
    finally:
        os.chmod(locked, 0o644)
    assert (tmp_path / "out" / "2026" / "a.jpg").read_bytes() == b"a"
