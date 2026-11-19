"""yorik-guard (integrations/hermes): what a Hermes agent may not do around Yorik.

The blocked cases are the steps an agent really took on 2026-11-19 when no
Yorik tool could file an email attachment."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "integrations" / "hermes"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load("yorik_guard", ROOT / "yorik-guard" / "__init__.py")
setup = _load("yorik_hermes_setup", ROOT / "setup.py")

CFG = {
    "yorik_dirs": ["/home/anna/yorik"], "home": "/home/anna",
    "containers": ["yorik-paperless-web", "supabase-db"], "words": ["yorik", "paperless", "immich"],
    "service_ports": [8010, 2283], "api_ports": [8000], "hosts": ["homebox"], "block_env_files": True,
}


def term(cmd):
    return guard.check("terminal", {"command": cmd}, CFG)


@pytest.mark.parametrize("cmd", [
    "grep -oE '^PAPERLESS' /home/anna/yorik/.env",
    "cd ~/yorik && cat backend/credential_store.py",
    "python3 -c \"open('$HOME/yorik/data/family.db')\"",
    "docker exec yorik-paperless-web python -c 'print(1)'",
    "docker inspect supabase-db",
    "docker compose -f x.yml exec paperless sh",
    "sudo -n tr '\\0' '\\n' < /proc/13898/environ",
    "curl -s http://localhost:8010/api/documents/",
    "curl http://homebox:2283/api/assets",
    "curl http://127.0.0.1:8000/api/email/attachments/4/download",
    "docker run --rm -v /:/host alpine cat /host/etc/shadow",
    "curl --unix-socket /var/run/docker.sock http://x/containers/json",
])
def test_the_ways_around_yorik_are_blocked(cmd):
    msg = term(cmd)
    assert msg and msg.startswith("Blocked by yorik-guard") and "mcp__yorik__" in msg


def test_a_script_that_opens_a_password_file_cannot_be_written():
    code = 'env = env_map("/home/anna/notes/.env")\nM.login(USER, env["MAIL_APP_PASSWORD"])'
    assert guard.check("write_file", {"path": "/tmp/x.py", "content": code}, CFG)
    assert guard.check("execute_code", {"code": "print(open('.env.local').read())"}, CFG)
    # Through the tool_call wrapper too.
    assert guard.check("tool_call", {"calls": [{"name": "terminal", "arguments": {"command": "cat .env"}}]}, CFG)


@pytest.mark.parametrize("tool,args", [
    ("terminal", {"command": "ls -la ~/projects && git status"}),
    ("terminal", {"command": "docker ps --format '{{.Names}}' | grep yorik"}),
    ("terminal", {"command": "curl -s http://127.0.0.1:8000/mcp -d '{}'"}),
    ("terminal", {"command": "cp .env.example .env.example.bak"}),
    ("write_file", {"path": "/tmp/app.js", "content": "const key = process.env.API_KEY; import.meta.env.MODE"}),
    ("execute_code", {"code": "import os; print(os.environ.get('HOME'))"}),
    ("read_file", {"path": "/home/anna/projects/README.md"}),
    ("mcp__yorik__read_email", {"message_id": 1, "include_html": True}),
    ("tool_call", {"calls": [{"name": "mcp__yorik__find_email_by_subject", "arguments": {"query": ".env"}}]}),
])
def test_everything_else_runs(tool, args):
    assert guard.check(tool, args, CFG) is None


def test_the_hook_vetoes_with_a_block_directive(monkeypatch):
    monkeypatch.setattr(guard, "_config", lambda: CFG)
    assert guard._on_pre_tool_call(tool_name="terminal", args={"command": "cat /home/anna/yorik/.env"})["action"] == "block"
    assert guard._on_pre_tool_call(tool_name="terminal", args={"command": "uptime"}) is None


def test_setup_installs_once_and_respects_a_later_disable(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "anna").mkdir(parents=True)
    (root / "config.yaml").write_text("{}\n")
    (root / "profiles" / "anna" / "config.yaml").write_text("{}\n")
    calls = []
    monkeypatch.setattr(setup, "_run", lambda cmd, timeout=20: calls.append(cmd) or "")
    monkeypatch.setattr(setup, "hermes_bin", lambda root: "hermes")
    assert setup.homes(root) == [root, root / "profiles" / "anna"]
    cfg = {"yorik_dirs": ["/y"]}
    assert setup.install(root, root, cfg) == "updated, enabled"
    assert setup.install(root / "profiles" / "anna", root, cfg) == "updated, enabled"
    assert calls == [["hermes", "plugins", "enable", "yorik-guard"],
                     ["hermes", "-p", "anna", "plugins", "enable", "yorik-guard"]]
    dest = root / "plugins" / "yorik-guard"
    assert json.loads((dest / "guard.json").read_text()) == cfg and (dest / "__init__.py").exists()
    # Second run: nothing to copy, and no second enable (a disable sticks).
    assert setup.install(root, root, cfg) == "current"
    assert len(calls) == 2
