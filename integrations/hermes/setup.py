#!/usr/bin/env python3
"""Put yorik-guard into this machine's Hermes, and keep it current.

    python3 integrations/hermes/setup.py [--hermes-home DIR] [--check]

For the Hermes home (~/.hermes, or $HERMES_HOME) and each of its profiles:
copies integrations/hermes/yorik-guard/ to <home>/plugins/yorik-guard/,
writes guard.json there (what to protect: this Yorik's folder, its
containers and their ports), and enables the plugin once. Nothing else in
Hermes changes. Someone who disables the plugin later keeps it disabled: it
is enabled only the first time it lands in a home.

Run by start.sh on every start (a no-op without Hermes) and by install.sh.
Standard library only, so it also runs on the host of a Docker install.
A running Hermes picks the plugin up at its next start (hermes gateway restart).
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
PLUGIN_SRC = HERE / "yorik-guard"
PLUGIN = "yorik-guard"
YORIK_DIR = HERE.parent.parent
ENABLED_MARK = ".enabled-once"
API_PORT = int(os.environ.get("HOMEOS_PORT") or os.environ.get("YORIK_PORT") or 8000)
# Model servers hold no household data; the agent may use them.
NOT_DATA = re.compile(r"llamacpp|-llm\b|search-embed", re.I)


def _run(cmd: List[str], timeout: int = 20) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def hermes_bin(root: Path) -> str:
    """The hermes command; systemd's PATH often lacks ~/.local/bin."""
    for c in (shutil.which("hermes"), Path.home() / ".local" / "bin" / "hermes",
              root / "hermes-agent" / "venv" / "bin" / "hermes"):
        if c and Path(c).exists():
            return str(c)
    return "hermes"


def containers() -> tuple[list, list]:
    """Names of Yorik's containers (started from this folder, or named
    yorik-*) and the host ports they publish. Other stacks on the machine
    stay the agent's business."""
    fmt = "{{.Names}}\t{{.Ports}}\t{{.Label \"com.docker.compose.project.working_dir\"}}"
    out = _run(["docker", "ps", "-a", "--format", fmt]) or ""
    names, ports = [], set()
    for line in out.splitlines():
        name, published, workdir = (line.split("\t") + ["", ""])[:3]
        ours = workdir and (workdir == str(YORIK_DIR) or workdir.startswith(str(YORIK_DIR) + "/"))
        if not (ours or name.lower().startswith("yorik")) or NOT_DATA.search(name):
            continue
        names.append(name.strip())
        ports.update(int(p) for p in re.findall(r":(\d+)->", published))
    return sorted(names), sorted(ports)


def hosts() -> list:
    """Names this machine answers to besides localhost."""
    found = {socket.gethostname()}
    status = _run(["tailscale", "status", "--json"], timeout=5)
    if status:
        try:
            me = json.loads(status).get("Self") or {}
            found.add((me.get("DNSName") or "").rstrip("."))
            found.add((me.get("HostName") or ""))
            found.update(me.get("TailscaleIPs") or [])
        except ValueError:
            pass
    return sorted(h for h in found if h)


def guard_config() -> dict:
    names, ports = containers()
    return {
        "yorik_dirs": [str(YORIK_DIR)],
        "home": str(Path.home()),
        "containers": names,
        "words": ["yorik", "paperless", "immich"],
        "service_ports": [p for p in ports if p != API_PORT] or [8010, 2283],
        "api_ports": [API_PORT],
        "hosts": hosts(),
        "block_env_files": True,
    }


def homes(root: Path) -> list:
    """The Hermes home and every profile home under it."""
    out = [root] if (root / "config.yaml").exists() else []
    prof = root / "profiles"
    if prof.is_dir():
        out += sorted(p for p in prof.iterdir() if p.is_dir() and (p / "config.yaml").exists())
    return out


def _same_tree(a: Path, b: Path) -> bool:
    if not b.is_dir():
        return False
    cmp = filecmp.dircmp(a, b, ignore=["__pycache__", "guard.json", ENABLED_MARK])
    return not (cmp.left_only or cmp.diff_files or cmp.funny_files)


def install(home: Path, root: Path, cfg: dict) -> str:
    dest = home / "plugins" / PLUGIN
    changed = not _same_tree(PLUGIN_SRC, dest)
    if changed:
        dest.mkdir(parents=True, exist_ok=True)
        for f in PLUGIN_SRC.iterdir():
            if f.is_file():
                shutil.copy2(f, dest / f.name)
    text = json.dumps(cfg, indent=2) + "\n"
    gpath = dest / "guard.json"
    if not gpath.exists() or gpath.read_text(encoding="utf-8") != text:
        gpath.write_text(text, encoding="utf-8")
        changed = True
    state = "updated" if changed else "current"
    mark = dest / ENABLED_MARK
    if not mark.exists():
        cmd = [hermes_bin(root)] + ([] if home == root else ["-p", home.name]) + ["plugins", "enable", PLUGIN]
        if _run(cmd, timeout=60) is None:
            return f"{state}, NOT enabled (run: {' '.join(cmd)})"
        mark.write_text("enabled by Yorik's setup; a later 'hermes plugins disable' is respected\n")
        state += ", enabled"
    return state


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    ap.add_argument("--check", action="store_true", help="print what would be protected and exit")
    a = ap.parse_args()
    cfg = guard_config()
    if a.check:
        print(json.dumps(cfg, indent=2))
        return 0
    root = Path(a.hermes_home).expanduser()
    targets = homes(root)
    if not targets:
        print("no Hermes here, nothing to do")
        return 0
    for home in targets:
        print(f"{PLUGIN} in {home}: {install(home, root, cfg)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
