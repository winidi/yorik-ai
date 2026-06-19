"""
Storage relocation — move-and-symlink the heavy data dirs onto an
external SSD so a laptop's tiny internal disk doesn't choke when the
photo library grows past 100 GB.

Why move-and-symlink and not configurable paths everywhere:
  Yorik's data layout is referenced in ~30 places: env vars in
  config.env, bind mounts in docker-compose.yml, hard-coded
  data/<subdir> in a few skills + the backup scripts. Touching all
  of them risks subtle breakage and forces every install to learn
  about the new env. With symlinks, the world keeps thinking the
  paths are still under data/ — the kernel transparently redirects
  reads + writes to the SSD.

Which subtrees get relocated:
  data/immich/library/   — photo originals (the big one)

  Only photos are relocatable today. data/documents/ (Yorik's local
  raw-upload pile) was historically in this list but moved poorly in
  practice — the dir is small, hot, and tightly coupled to
  data/documents.db (the vector index on the internal disk), so the
  move bought little and added a failure mode. Keeping it on the
  internal disk alongside the index.

What STAYS internal (always):
  data/family.db         — operational DB, ~30 MB, hot
  data/documents.db      — vector index, hot, must stay near family.db
  data/.credential_key   — Fernet key, must stay with the DB
  data/voices/           — read-only TTS models, ~500 MB fixed
  data/logs/             — small, rotated
  data/backups/          — small, by design
  data/speaker_model/    — small
  data/paperless/        — OCR'd PDFs + scans + the Paperless DB; intentionally
                           NOT relocatable. Paperless writes lots of small files
                           and links them to its Postgres metadata, so external
                           USB/exFAT is fragile (OCR latency, inode quirks, the
                           db can't be on the same external slot anyway). Lives
                           with the internal disk regardless of the SSD setup.

Failure behaviour:
  - Q2 policy: REFUSE to start when a relocated symlink is dangling
    (SSD pulled, not yet mounted, permissions broken). The startup
    check raises a loud error with the exact path that's broken.
    Photos can't function safely without their storage, and silently
    writing to a wrong path is worse than a hard stop.
  - See assert_storage_ready() — called from main.py startup.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("yorik.storage")


# Subtree paths RELATIVE to the project's data dir. Order matters only
# for status display (most-impactful first).
# Subtrees the user may relocate to an external SSD.
#
# DELIBERATELY EXCLUDED — postgres data dirs (data/immich/postgres,
# data/paperless/db). Two reasons:
#   1. PostgreSQL cannot run on exFAT/FAT/NTFS (no fsync, no proper
#      file locking, mounted-uid ownership rather than real POSIX).
#      A move there silently corrupts the data dir.
#   2. The postgres containers run as their internal `postgres` user
#      (UID 999) which is intentional — they don't need to be host-
#      owned, and our chown migration must NOT touch them.
# Postgres data is small (~MBs to single-digit GB at household scale)
# so keeping it on the internal disk costs nothing and saves us from
# the worst-case "user moves to exFAT, postgres dies, data unrecoverable".
RELOCATABLE = (
    "immich/library",
    # Paperless dirs are deliberately NOT relocatable. They contain lots
    # of small files tightly coupled to the Paperless Postgres DB, and
    # putting them on a USB/exFAT SSD slows OCR ingestion and breaks
    # rsync-style backups. See module docstring for the full reasoning.
    #
    # data/documents/ used to be in this list too. It's small, hot, and
    # tightly coupled to the vector index in data/documents.db which is
    # pinned to the internal disk — moving it bought little and added a
    # failure mode where partial-move state left a dangling symlink that
    # bricked startup via assert_storage_ready(). Photos-only for now.
)

# Dirs that MUST be host-owned (so the docker containers — which we
# pin to the host UID — can write). Used by start.sh's pre-flight
# ownership check. Note: paperless dirs are host-owned even though
# they're NOT in RELOCATABLE — the container still needs to write to
# them on the internal disk.
HOST_OWNED_DATA_DIRS = (
    "data/immich/library",
    "data/paperless/data",
    "data/paperless/media",
    "data/paperless/export",
    "data/paperless/consume",
)

# Non-POSIX filesystems where postgres + symlinks misbehave. Used by
# _filesystem_type() / move_to() to warn early.
NON_POSIX_FILESYSTEMS = frozenset({
    "exfat", "vfat", "msdos", "fat", "fat32",
    "ntfs", "ntfs3", "fuseblk",  # fuseblk == ntfs-3g and similar
})

# Marker file at the project root that records "this is where the
# relocated subtrees live now." Single source of truth — Settings UI,
# startup check, and backup scripts all read this.
MARKER_FILENAME = "storage_root.txt"


# ─── path helpers ─────────────────────────────────────────────────────


def _project_root() -> Path:
    """Yorik project root (the parent of backend/)."""
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    return _project_root() / "data"


# ─── bounce the immich-server container around the move ──────────────
#
# Only immich-server has a bind mount onto a RELOCATABLE subtree
# (./data/immich/library:/data in docker-compose.yml). We stop it
# before the move so shutil.move's copy+rmtree of the source tree
# doesn't leave the container's bind pinned to a now-unlinked inode
# (which renders as "Immich is up but /data is empty and every thumb
# 404s"). We start it back after — Docker re-resolves the bind mount
# through the new symlink to the SSD on restart.
#
# We talk to docker directly by container name instead of going via
# `docker compose down/up`. The bundled services are profile-gated
# (profiles: ["bundled-immich"|"bundled-paperless"|"bundled-whatsapp"])
# and `docker compose down` without --profile flags is a no-op for
# profile-gated services — which is the bug that left immich-server
# running through the move and pinned its mount to a zombie inode.
#
# Paperless / WhatsApp containers don't touch any relocatable path,
# so they stay up. immich-postgres / immich-redis / immich-ml siblings
# don't bind-mount /data either, so they also stay up — only the
# server needs to bounce to re-resolve.

_RELOCATION_AFFECTED_CONTAINER = "yorik-immich-server"


def _docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _container_running(name: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def _compose_down() -> bool:
    """Stop yorik-immich-server before the move so its open file
    handles don't pin the source tree's inodes. Returns True if we
    actually stopped a container (caller restarts on the way out).
    No-op when Docker isn't installed, the daemon is unreachable,
    or the container isn't running (BYO Immich or
    YORIK_ENABLE_IMMICH=0)."""
    if not _docker_available():
        return False
    if not _container_running(_RELOCATION_AFFECTED_CONTAINER):
        log.info("storage: %s not running, no stop needed", _RELOCATION_AFFECTED_CONTAINER)
        return False
    log.info("storage: stopping %s so its bind mount can re-resolve through the new symlink",
             _RELOCATION_AFFECTED_CONTAINER)
    r = subprocess.run(
        ["docker", "stop", "-t", "30", _RELOCATION_AFFECTED_CONTAINER],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        log.warning("storage: docker stop %s failed: %s — move will proceed but bind mount may stay stale",
                    _RELOCATION_AFFECTED_CONTAINER, (r.stderr or r.stdout).strip())
        return False
    return True


def _compose_up() -> None:
    """Restart yorik-immich-server after the move. Best-effort: a
    failure here is logged but data is already on the SSD and the
    symlink is in place — the user can recover manually with
    `docker start yorik-immich-server`."""
    if not _docker_available():
        return
    log.info("storage: restarting %s after move", _RELOCATION_AFFECTED_CONTAINER)
    r = subprocess.run(
        ["docker", "start", _RELOCATION_AFFECTED_CONTAINER],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        log.warning("storage: docker start %s failed: %s — recover with: docker start %s",
                    _RELOCATION_AFFECTED_CONTAINER,
                    (r.stderr or r.stdout).strip(),
                    _RELOCATION_AFFECTED_CONTAINER)


def _marker_path() -> Path:
    return _data_dir() / MARKER_FILENAME


def get_storage_root() -> Optional[Path]:
    """Read the marker file. None means "everything is internal" (default)."""
    marker = _marker_path()
    if not marker.exists():
        return None
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    return Path(raw)


def _write_marker(root: Optional[Path]) -> None:
    marker = _marker_path()
    if root is None:
        marker.unlink(missing_ok=True)
    else:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(root.resolve()), encoding="utf-8")


# ─── status ───────────────────────────────────────────────────────────


@dataclass
class SubtreeStatus:
    subtree: str          # e.g. "immich/library"
    expected_path: Path   # the path inside data/ that code uses
    is_symlink: bool      # True if we relocated it
    target: Optional[Path] = None    # symlink target when is_symlink
    healthy: bool = True             # False = dangling (target missing/unreachable)
    bytes_used: Optional[int] = None # best-effort du; None when path missing

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subtree": self.subtree,
            "expected_path": str(self.expected_path),
            "is_symlink": self.is_symlink,
            "target": str(self.target) if self.target else None,
            "healthy": self.healthy,
            "bytes_used": self.bytes_used,
        }


def status() -> Dict[str, Any]:
    """Snapshot of storage state — for the Settings page + startup checks."""
    root = get_storage_root()
    subtrees: List[SubtreeStatus] = []
    for rel in RELOCATABLE:
        expected = _data_dir() / rel
        st = SubtreeStatus(subtree=rel, expected_path=expected, is_symlink=False)
        try:
            if expected.is_symlink():
                st.is_symlink = True
                st.target = expected.resolve()
                # exists() follows symlinks → dangling iff target missing
                st.healthy = expected.exists()
            elif expected.exists():
                st.healthy = True
            else:
                # Path simply doesn't exist yet — fine; treated as healthy
                # (first install hasn't written anything to it).
                st.healthy = True
        except OSError as exc:
            log.warning("status() probe failed for %s: %s", expected, exc)
            st.healthy = False
        st.bytes_used = _safe_du(expected) if st.healthy and expected.exists() else None
        subtrees.append(st)
    all_healthy = all(s.healthy for s in subtrees)
    return {
        "storage_root": str(root) if root else None,
        "all_healthy": all_healthy,
        "subtrees": [s.to_dict() for s in subtrees],
    }


def _safe_du(path: Path) -> Optional[int]:
    """Best-effort `du -s` in bytes. Returns None on any error."""
    try:
        # Use the actual file blocks (-b) for accuracy on small files.
        # --apparent-size keeps the number stable across filesystems.
        res = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if res.returncode != 0:
            return None
        return int(res.stdout.split()[0])
    except (subprocess.SubprocessError, ValueError, IndexError):
        return None


# ─── volume detection ─────────────────────────────────────────────────


def detect_volumes() -> List[Dict[str, Any]]:
    """List external storage devices currently mounted. The Settings UI
    drops these into a dropdown so the user picks instead of typing.

    Tries `lsblk -J` first (Linux, rich info), falls back to parsing
    /proc/mounts. macOS-friendly: scans /Volumes for everything that
    isn't the system disk.
    """
    out: List[Dict[str, Any]] = []
    # Linux path via lsblk JSON
    try:
        res = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,MOUNTPOINT,SIZE,TYPE,LABEL,HOTPLUG"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if res.returncode == 0:
            import json
            blocks = json.loads(res.stdout).get("blockdevices", [])
            def walk(node: dict) -> None:
                mp = node.get("mountpoint")
                if (mp
                        and mp not in ("/", "/boot", "/boot/efi", "/home")
                        and not mp.startswith("/snap")
                        and not mp.startswith("/var/")
                        and not mp.startswith("/proc")
                        and not mp.startswith("/sys")
                        and not mp.startswith("/run")):
                    out.append({
                        "name":      node.get("name"),
                        "mountpoint": mp,
                        "size":       node.get("size"),
                        "label":      node.get("label"),
                        "hotplug":    bool(node.get("hotplug")),
                        # The actual yorik dir we'd suggest as the target.
                        "suggested_target": str(Path(mp) / "yorik"),
                    })
                for child in (node.get("children") or []):
                    walk(child)
            for n in blocks:
                walk(n)
            return out
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    # macOS fallback: /Volumes/*
    volumes_dir = Path("/Volumes")
    if volumes_dir.is_dir():
        for entry in sorted(volumes_dir.iterdir()):
            # Skip the system volume (usually named "Macintosh HD" or similar)
            # and anything that's clearly a symlink-to-/.
            try:
                if entry.is_symlink() and entry.resolve() == Path("/"):
                    continue
            except OSError:
                continue
            out.append({
                "name":     entry.name,
                "mountpoint": str(entry),
                "label":    entry.name,
                "hotplug":  True,
                "suggested_target": str(entry / "yorik"),
            })
    return out


# ─── the move-and-symlink operation ───────────────────────────────────


class StorageError(Exception):
    """Raised by move_to() / restore() when an irrecoverable problem
    hits mid-operation. The message is user-facing — keep it concrete."""


def _check_source_writable(src: Path) -> Optional[str]:
    """Walk `src` and look for files/dirs the current process can't
    delete. shutil.move uses rmtree under the hood for cross-filesystem
    moves, and rmtree needs delete permission on every entry.

    Returns None when everything's writable, or a user-facing error
    message (with a concrete sudo chown remediation) on first offender.

    The single most common cause: bundled Docker containers (Immich,
    Paperless) wrote files as root because the container itself ran as
    root. The host user (who is running uvicorn) then can't unlink them.
    Catching this BEFORE shutil.move starts saves the user from a
    half-copied state where the source is partially deleted and the
    target has a partial copy."""
    if not src.exists() or src.is_symlink():
        return None
    my_uid = os.geteuid()
    for dirpath, dirnames, filenames in os.walk(src):
        d = Path(dirpath)
        # Permission to delete an entry comes from WRITE on its PARENT
        # dir. Top-level src is handled by caller's permission to
        # rename it; for children we check write on the parent.
        if not os.access(d, os.W_OK):
            try:
                stat = d.stat()
                owner = stat.st_uid
            except OSError:
                owner = -1
            owner_s = "root" if owner == 0 else (f"uid={owner}" if owner >= 0 else "unknown")
            return (
                f"can't move: {d} is owned by {owner_s} and the current "
                f"user (uid={my_uid}) doesn't have write access to it. "
                "This usually means a Docker container ran as root and "
                "created files the host user can't move.\n\n"
                "Fix it once — chown the relocatable data dirs (do NOT "
                "touch data/immich/postgres; it's container-managed and "
                "needs its own UID 999):\n\n"
                "  sudo chown -R $(id -u):$(id -g) \\\n"
                "    data/documents \\\n"
                "    data/immich/library\n\n"
                "Then retry the move."
            )
    return None


def _filesystem_type(path: Path) -> str:
    """Find the filesystem type the given path lives on by scanning
    /proc/mounts for the longest mount-point prefix. Returns the fs
    type string ('ext4', 'exfat', 'ntfs', …) or 'unknown'.

    Used to warn callers when they pick a target on exFAT/FAT/NTFS —
    those work fine for media blobs (JPGs, PDFs) but kill postgres
    and lose POSIX permissions. We exclude postgres dirs from
    RELOCATABLE so the worst-case is no longer reachable, but the
    warning still helps users understand what they're getting into."""
    try:
        target = str(path.resolve())
    except OSError:
        target = str(path)
    best_mount = ""
    best_fstype = "unknown"
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp, fstype = parts[1], parts[2]
                # Either the target IS the mountpoint, or it's a descendant.
                if target == mp or target.startswith(mp.rstrip("/") + "/"):
                    if len(mp) > len(best_mount):
                        best_mount = mp
                        best_fstype = fstype
    except OSError:
        pass
    return best_fstype


def move_to(target_root: str | Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """Move each RELOCATABLE subtree into <target_root>/<subtree> and
    replace the original path with a symlink. Idempotent at the subtree
    level — anything already symlinked correctly is skipped.

    target_root MUST exist and be writable. The function refuses to
    operate on a path that resolves to the same filesystem as the
    project root (pointless; just makes the data harder to find).
    """
    target = Path(target_root).expanduser().resolve()
    # Auto-create the target if it's missing but its parent exists and
    # is writable — the user picked an external mount from the volumes
    # dropdown and typed a subfolder name (e.g. /media/user/SSD/yorik
    # where SSD is the mount). Making them `mkdir` first is needless
    # friction. We only create the LEAF; if the parent is also missing
    # the path is probably a typo, so we still refuse.
    if not target.exists():
        parent = target.parent
        if parent.is_dir() and os.access(parent, os.W_OK):
            try:
                target.mkdir(parents=False, exist_ok=True)
            except OSError as exc:
                raise StorageError(f"couldn't create target {target}: {exc}")
        else:
            raise StorageError(
                f"target path does not exist: {target} "
                f"(parent {parent} is also missing or not writable — "
                f"check the path)"
            )
    if not os.access(target, os.W_OK):
        raise StorageError(f"target path not writable: {target}")
    if _same_filesystem(target, _project_root()):
        raise StorageError(
            f"target {target} is on the same filesystem as the project; "
            "pick a path on the external SSD instead."
        )
    # Warn (don't refuse) on non-POSIX filesystems. Media subtrees
    # (photos, PDFs) work fine — they're just blobs being read/written.
    # We took postgres dirs out of RELOCATABLE so the "postgres on
    # exFAT silently corrupts" trap is no longer reachable. Still flag
    # the fs type so the user knows what they're picking.
    target_fs = _filesystem_type(target)
    fs_warning: Optional[str] = None
    if target_fs.lower() in NON_POSIX_FILESYSTEMS:
        fs_warning = (
            f"target {target} is on a {target_fs} filesystem. Photos "
            "and PDFs work, but POSIX permissions and symlinks aren't "
            "preserved. If you ever need to host databases (Postgres) "
            "or use rsync-style backups on this disk, reformat to ext4 "
            "or btrfs first."
        )
        log.warning("storage move: %s", fs_warning)

    # Stop bundled services. Without this, shutil.move on a cross-fs
    # path fails during rmtree because the container's still holding
    # open files (and their container-uid ownership prevents unlink
    # even after the copy completes). We always restart on the way out,
    # whether the move succeeded or raised.
    compose_was_stopped = False
    if not dry_run:
        compose_was_stopped = _compose_down()

    try:
        actions: List[Dict[str, Any]] = []
        for rel in RELOCATABLE:
            src = _data_dir() / rel
            dst = target / rel
            action = {"subtree": rel, "src": str(src), "dst": str(dst)}
            # Case 1: src already a symlink to the right place — skip.
            if src.is_symlink():
                try:
                    if src.resolve() == dst.resolve():
                        action["op"] = "already_linked"
                        actions.append(action)
                        continue
                except OSError:
                    pass
                # Symlink points somewhere else — re-target it.
                if not dry_run:
                    src.unlink(missing_ok=False)
            # Case 2: src is a real directory (or doesn't exist yet).
            if dst.exists():
                # Target already has data — be conservative and refuse
                # rather than merge or overwrite. Typical cause: a
                # previous move attempt copied partially before crashing.
                # Quote the rm -rf so the user can run it without
                # decoding what we mean.
                if any(dst.iterdir()):
                    raise StorageError(
                        f"refusing to merge into non-empty target {dst}. "
                        "This usually means a previous move attempt "
                        "partially copied before failing.\n\n"
                        f"Clean it up with:  rm -rf {dst}\n"
                        "Then retry the move."
                    )
            if src.exists() and not src.is_symlink():
                if dry_run:
                    action["op"] = "would_move"
                else:
                    # Pre-flight: check the source is fully writable by
                    # this process before we start copying. Catches the
                    # root-owned-files trap upfront with a clear hint
                    # instead of crashing mid-move on rmtree.
                    err = _check_source_writable(src)
                    if err:
                        raise StorageError(err)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    src.symlink_to(dst, target_is_directory=True)
                    action["op"] = "moved_and_linked"
            else:
                # Source didn't exist at all — create the empty target dir
                # so future writes land in the right place, then symlink.
                if dry_run:
                    action["op"] = "would_create_and_link"
                else:
                    dst.mkdir(parents=True, exist_ok=True)
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.symlink_to(dst, target_is_directory=True)
                    action["op"] = "created_and_linked"
            actions.append(action)

        if not dry_run:
            _write_marker(target)
        result: Dict[str, Any] = {
            "target": str(target),
            "actions": actions,
            "dry_run": dry_run,
        }
        if fs_warning:
            result["warnings"] = [fs_warning]
        return result
    finally:
        if compose_was_stopped:
            _compose_up()


def restore(*, dry_run: bool = False) -> Dict[str, Any]:
    """Reverse of move_to: copy data back into data/<subtree>, replace
    the symlink with the real directory. Leaves the external copy
    intact so the user can verify before deleting.
    """
    actions: List[Dict[str, Any]] = []
    for rel in RELOCATABLE:
        path = _data_dir() / rel
        action = {"subtree": rel, "path": str(path)}
        if not path.is_symlink():
            action["op"] = "not_linked"
            actions.append(action)
            continue
        target = path.resolve()
        if not target.exists():
            # Dangling symlink. Just remove it — there's nothing to copy.
            if not dry_run:
                path.unlink()
                path.mkdir(parents=True, exist_ok=True)
            action["op"] = "removed_dangling"
            actions.append(action)
            continue
        if dry_run:
            action["op"] = "would_restore"
            action["target"] = str(target)
            actions.append(action)
            continue
        # Replace the symlink with a fresh real dir, then copy contents in.
        path.unlink()
        path.mkdir(parents=True, exist_ok=True)
        # copy2 preserves metadata; copytree picks every subtree.
        for child in target.iterdir():
            dst_child = path / child.name
            if child.is_dir():
                shutil.copytree(child, dst_child)
            else:
                shutil.copy2(child, dst_child)
        action["op"] = "restored"
        action["target"] = str(target)
        actions.append(action)
    if not dry_run:
        _write_marker(None)
    return {"actions": actions, "dry_run": dry_run}


def _same_filesystem(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


# ─── startup gate ─────────────────────────────────────────────────────


def assert_storage_ready() -> None:
    """Called from main.py startup. If any RELOCATABLE subtree symlink
    is dangling (target gone — e.g. external SSD unmounted), raise
    SystemExit with a clear message. Matches the Q2 policy: refuse to
    boot rather than silently lose writes.
    """
    st = status()
    if st["all_healthy"]:
        return
    broken = [s for s in st["subtrees"] if not s["healthy"]]
    lines = ["YORIK STORAGE NOT READY — refusing to start.", ""]
    for b in broken:
        lines.append(
            f"  • {b['subtree']!s} is symlinked to {b['target']!r} which is missing."
        )
    lines.extend([
        "",
        "Likely causes:",
        "  1. External storage SSD is unplugged or unmounted.",
        "  2. The mount point is wrong (auto-mount didn't fire).",
        "  3. Permissions changed on the target directory.",
        "",
        "Fix it by:",
        "  - Plugging the SSD back in / re-running `mount`.",
        "  - Or, from another terminal: `python -c 'from backend.storage import restore; restore()'`",
        "    (copies the symlink targets back into data/ — needs the SSD attached).",
        "  - Then re-start Yorik.",
        "",
        "Configured storage root:",
        f"  {get_storage_root()}",
    ])
    raise SystemExit("\n".join(lines))


__all__ = [
    "RELOCATABLE",
    "get_storage_root",
    "status",
    "detect_volumes",
    "move_to",
    "restore",
    "assert_storage_ready",
    "StorageError",
]
