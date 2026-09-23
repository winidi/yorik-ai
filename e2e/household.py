"""The test household: a throwaway Yorik next to the real one.

    venv/bin/python e2e/household.py up      # build, start, seed a family
    venv/bin/python e2e/household.py down    # stop and throw it all away
    venv/bin/python e2e/household.py status

It never touches the household's own Yorik:

  * the code runs from a copy of the working tree under e2e/.run/app,
    so every relative `data/…` path and every path anchored at the
    project root lands in that copy's own data/ folder;
  * the copy gets its own config.env; the real one is not read, and the
    environment handed to the server is built from scratch;
  * its database is `yorik_e2e` in the same Postgres cluster the tests
    use, built from migrations_pg/ like the pytest template;
  * Paperless, Immich, WhatsApp, n8n and the agent point at a dead
    port; the language model is e2e/fake_llm.py.

The family — two parents, two children — is created through Yorik's own
routes (first-run setup, Settings → people), then given tasks, events,
contacts, mail and PINs. Logins land in e2e/.run/household.json for the
crawler.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "e2e" / ".run"
APP = RUN / "app"
DB_NAME = "yorik_e2e"
PORT = int(os.getenv("YORIK_E2E_PORT", "8177"))
LLM_PORT = int(os.getenv("YORIK_E2E_LLM_PORT", "8178"))
BASE = f"http://127.0.0.1:{PORT}"
PY = str(ROOT / "venv" / "bin" / "python")
DEAD = "http://127.0.0.1:9"          # the discard port: nothing answers
PASSWORD = "testhaus-2026"
# YORIK_E2E_REAL_LLM=http://127.0.0.1:8080/v1 lets the test household talk
# to the real model (read-only inference; whatever the chat creates lands
# in the throwaway database). Default: the fake model.
REAL_LLM = os.getenv("YORIK_E2E_REAL_LLM", "")

FAMILY = [
    # key, name, email, role, pin
    ("anna",  "Anna",  "anna@example.test",  "platform_admin", "1111"),
    ("ben",   "Ben",   "ben@example.test",   "member",         "2222"),
    ("clara", "Clara", "clara@example.test", "restricted",     "3333"),
    ("david", "David", "david@example.test", "restricted",     "4444"),
]

COPY_EXCLUDES = [
    ".git", "venv", "data", "models", "infra", "e2e", "archive", "config.env",
    "node_modules", "__pycache__", "*.pyc", ".pytest_cache", "whatsapp-bridge/auth*",
]


def _log(msg: str) -> None:
    print(f"[household] {msg}", flush=True)


# ─── database ────────────────────────────────────────────────────────

def _pg():
    """Admin settings and helpers from the pytest fixtures, so the test
    household's database is built exactly like the test template."""
    sys.path.insert(0, str(ROOT))
    from tests import conftest as C
    return C


def build_database() -> dict[str, str]:
    C = _pg()
    settings = C._pg_settings()
    with C._admin_conn(settings) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{DB_NAME}"')
    with C._admin_conn(settings, DB_NAME) as db:
        db.execute(C._AUTH_SHIM_SQL)
        for version, name, path in C._discover_migrations():
            if version not in C._LEGACY_VERSIONS and version not in C._CLUSTER_ONLY_VERSIONS:
                db.execute(path.read_text(encoding="utf-8"))
            db.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s) "
                       "ON CONFLICT (version) DO NOTHING", (version, name))
        db.execute(C._GRANTS_SQL.format(db=DB_NAME))
    _log(f"database {DB_NAME} built")
    return settings


def drop_database() -> None:
    C = _pg()
    settings = C._pg_settings()
    with C._admin_conn(settings) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


# ─── processes ───────────────────────────────────────────────────────

def server_env(settings: dict[str, str]) -> dict[str, str]:
    """Only what a stock shell has, plus the test household's settings.
    Nothing of the real config.env comes along."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOMEOS_", "YORIK_", "PAPERLESS_", "IMMICH_"))}
    env.update({
        "YORIK_DB_HOST": settings["host"], "YORIK_DB_PORT": str(settings["port"]),
        "YORIK_DB_USER": "postgres", "YORIK_DB_PASSWORD": settings["password"],
        "YORIK_DB_NAME": DB_NAME,
        "YORIK_BIND": "127.0.0.1", "HOMEOS_PORT": str(PORT), "YORIK_SKIP_BIND_PROBE": "1",
        "YORIK_TRUSTED_ORIGINS": BASE,
        "YORIK_TZ": "Europe/Berlin", "TZ": "Europe/Berlin",
        "HOMEOS_LLM_BASE_URL": REAL_LLM or f"http://127.0.0.1:{LLM_PORT}/v1",
        "HOMEOS_MODEL": os.getenv("YORIK_E2E_MODEL", "qwen3.8-27b") if REAL_LLM else "fake-household-model",
        "HOMEOS_DEFAULT_LANGUAGE": "de",
        "YORIK_ENABLE_WHATSAPP": "0", "YORIK_ENABLE_PAPERLESS": "0", "YORIK_ENABLE_IMMICH": "0",
        "YORIK_WA_BRIDGE_URL": DEAD, "YORIK_WA_BRIDGE_WS": "ws://127.0.0.1:9/events",
        "YORIK_WA_BRIDGE_TOKEN": "e2e",
        "HOMEOS_API_URL": f"{BASE}/api/ask",
        "PAPERLESS_INTERNAL_URL": DEAD, "HOMEOS_N8N_BASE_URL": DEAD,
        "YORIK_HOST_INTERNAL_URL": DEAD,
        "HOMEOS_AGENT_URL": "", "HOMEOS_AGENT_SHARED": "0",
        "YORIK_SEARCH_EMBED": "0",
        "HOMEOS_EMBED_BASE_URL": f"http://127.0.0.1:{LLM_PORT}/v1",
        "HOMEOS_DIARIZATION_AUTO_DOWNLOAD": "0",
        # Six crawls share one address; the per-IP limit is for strangers.
        "YORIK_API_MAX_REQUESTS": "100000", "YORIK_API_ASK_MAX": "1000", "YORIK_API_LOGIN_MAX": "1000",
        # e2e/guard/sitecustomize.py: nothing local but these ports.
        "PYTHONPATH": str(ROOT / "e2e" / "guard"),
        # …and no docker: a stand-in that refuses (e2e/guard/bin/docker).
        "PATH": f"{ROOT / 'e2e' / 'guard' / 'bin'}:{os.environ.get('PATH', '')}",
        "YORIK_E2E_ALLOWED_PORTS": ",".join(map(str, (settings["port"], PORT, LLM_PORT, 9080)
                                                + ((int(REAL_LLM.split(":")[2].split("/")[0]),) if REAL_LLM else ()))),
        "YORIK_E2E_BLOCKED_LOG": str(RUN / "blocked.log"),
    })
    return env


def check_guard(env: dict[str, str]) -> None:
    """Refuse to start when the network guard is not in force."""
    probe = ("import socket\n"
             "try:\n    socket.create_connection(('127.0.0.1', 3015), timeout=1)\n"
             "except ConnectionRefusedError as e:\n    raise SystemExit(0 if 'off limits' in str(e) else 1)\n"
             "raise SystemExit(1)\n")
    if subprocess.run([PY, "-c", probe], env=env).returncode != 0:
        raise SystemExit("network guard not active — refusing to start the test household")
    (RUN / "blocked.log").write_text("")


def _write_config(env: dict[str, str]) -> None:
    keys = [k for k in env if k.startswith(("HOMEOS_", "YORIK_", "PAPERLESS_"))]
    (APP / "config.env").write_text(
        "# The test household's own config (e2e/household.py). Not the real one.\n"
        + "".join(f"{k}={env[k]}\n" for k in sorted(keys)))


def copy_code() -> None:
    APP.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--delete"]
    for ex in COPY_EXCLUDES:
        cmd += ["--exclude", ex]
    subprocess.run(cmd + [f"{ROOT}/", f"{APP}/"], check=True)
    (APP / "data").mkdir(exist_ok=True)
    _log(f"code copied to {APP}")


def _start(name: str, args: list[str], cwd: Path, env: dict[str, str]) -> None:
    log = open(RUN / f"{name}.log", "w")
    p = subprocess.Popen(args, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    (RUN / f"{name}.pid").write_text(str(p.pid))


def _stop(name: str) -> None:
    pid_file = RUN / f"{name}.pid"
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text())
    try:
        os.killpg(pid, signal.SIGTERM)
        for _ in range(50):
            os.killpg(pid, 0)
            time.sleep(0.1)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pid_file.unlink(missing_ok=True)


def _wait(url: str, seconds: int, what: str) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=2).status_code < 500:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{what} did not come up within {seconds}s — see {RUN}/*.log")


# ─── the family ──────────────────────────────────────────────────────

def _ok(r: requests.Response, what: str) -> dict:
    if r.status_code >= 400:
        raise SystemExit(f"seeding failed at '{what}': HTTP {r.status_code} {r.text[:300]}")
    try:
        return r.json()
    except ValueError:
        return {}


def seed(settings: dict[str, str]) -> dict:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    at = lambda d, hh, mm=0: datetime(d.year, d.month, d.day, hh, mm).isoformat(timespec="minutes")

    sessions: dict[str, requests.Session] = {}
    ids: dict[str, str] = {}

    # First run: Anna sets the box up, then adds the family (Settings → people).
    anna = requests.Session()
    key, name, email, role, _ = FAMILY[0]
    _ok(anna.post(f"{BASE}/api/auth/setup", json={"email": email, "password": PASSWORD, "name": name}), "setup")
    sessions["anna"] = anna
    for key, name, email, role, _ in FAMILY[1:]:
        u = _ok(anna.post(f"{BASE}/api/users", json={"email": email, "name": name, "role": role,
                                                     "password": PASSWORD, "auto_provision": []}), f"create {name}")
        ids[key] = str(u["id"])
        s = requests.Session()
        _ok(s.post(f"{BASE}/api/auth/login", json={"email": email, "password": PASSWORD}), f"login {name}")
        sessions[key] = s
    ids["anna"] = str(_ok(anna.get(f"{BASE}/api/auth/me"), "me")["user"]["id"])

    for key, *_rest, pin in FAMILY:
        s = sessions[key]
        _ok(s.post(f"{BASE}/api/onboarding/complete"), f"onboarding {key}")
        _ok(s.post(f"{BASE}/api/profile/pin", json={"pin": pin}), f"pin {key}")

    role_of = {k: r for k, _n, _e, r, _p in FAMILY}

    def task(who: str, title: str, **kw) -> None:
        _ok(sessions[who].post(f"{BASE}/api/tasks?role={role_of[who]}", json={"title": title, **kw}),
            f"task {title}")

    task("anna", "Steuererklärung abgeben", due_date=today.isoformat(), priority=2)
    task("anna", "Geschenk für Oma besorgen")
    task("anna", "Zimmer aufräumen", due_date=today.isoformat(), assignee_user_ids=[ids["clara"]])
    task("anna", "Tisch decken", due_date=today.isoformat(), assignee_user_ids=[ids["clara"], ids["david"]],
         recurrence_rule="daily")
    task("ben", "Auto zum TÜV bringen", due_date=tomorrow.isoformat())
    task("ben", "Hausaufgaben Mathe", due_date=today.isoformat(), assignee_user_ids=[ids["david"]])
    task("ben", "Getränke holen", due_date=(today - timedelta(days=2)).isoformat(), assignee_user_ids=[ids["anna"]])
    task("clara", "Buch zurückgeben", due_date=tomorrow.isoformat())

    def event(who: str, title: str, start: str, end: str, **kw) -> None:
        _ok(sessions[who].post(f"{BASE}/api/events?role={role_of[who]}",
                               json={"title": title, "starts_at": start, "ends_at": end, **kw}), f"event {title}")

    event("anna", "Zahnarzt", at(today, 15), at(today, 16))
    event("anna", "Elternabend", at(tomorrow, 19), at(tomorrow, 21), attendee_user_ids=[ids["ben"]])
    event("ben", "Fußballtraining David", at(today, 17), at(today, 18, 30), attendee_names=["David"])
    event("ben", "Kundentermin", at(tomorrow, 10), at(tomorrow, 11))
    event("clara", "Geburtstag bei Mia", at(tomorrow, 14), at(tomorrow, 17))

    for who, display in (("anna", "Oma Hilde"), ("anna", "Praxis Dr. Berg"), ("ben", "Autohaus Lange")):
        _ok(sessions[who].post(f"{BASE}/api/contacts",
                               json={"display_name": display,
                                     "kind": "business" if display.startswith(("Praxis", "Autohaus")) else "person"}),
            f"contact {display}")

    _ok(anna.post(f"{BASE}/api/demo/seed"), "demo data")

    # Mail cannot come in without an IMAP server; put a small inbox in
    # place by hand, on an account that is switched off (no fetcher).
    C = _pg()
    with C._admin_conn(settings, DB_NAME) as db:
        for who in ("anna", "ben"):
            owner = ids[who]
            aid = db.execute(
                "INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                "smtp_username, credential_key, enabled) VALUES (%s, %s, '127.0.0.1', %s, '127.0.0.1', %s, 'e2e', 0) "
                "RETURNING id", (owner, f"{who}@example.test", who, who)).fetchone()[0]
            for uid, (sender, subject, body) in enumerate((
                ("schule@example.test", "Elternbrief: Wandertag am Freitag",
                 "Liebe Eltern,\n\nam Freitag ist Wandertag. Bitte Brotdose und Regenjacke mitgeben.\n\nViele Grüße"),
                ("rechnung@stadtwerke.example.test", "Ihre Rechnung Oktober",
                 "Guten Tag,\n\nIhre Rechnung über 84,20 € ist am 15. fällig.\n\nStadtwerke"),
                ("oma.hilde@example.test", "Sonntag Kaffee?",
                 "Hallo ihr Lieben,\n\nkommt ihr Sonntag um drei zum Kaffee?\n\nOma"),
            ), start=1):
                db.execute(
                    "INSERT INTO email_messages (account_id, uid, owner_user_id, message_id, from_email, subject, "
                    "body_text, snippet, is_unread, is_sent, date_received) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 0, %s)",
                    (aid, uid, owner, f"<e2e-{who}-{uid}@example.test>", sender, subject, body, body[:120],
                     (datetime.now() - timedelta(hours=uid * 3)).isoformat(timespec="seconds")))

    household = {
        "base_url": BASE,
        "password": PASSWORD,
        "real_llm": bool(REAL_LLM),
        "people": [{"key": k, "name": n, "email": e, "role": r, "pin": p, "id": ids.get(k, "")}
                   for k, n, e, r, p in FAMILY],
    }
    (RUN / "household.json").write_text(json.dumps(household, indent=2, ensure_ascii=False))
    _log("family seeded: " + ", ".join(f"{n} ({r})" for _k, n, _e, r, _p in FAMILY))
    return household


# ─── commands ────────────────────────────────────────────────────────

def up() -> None:
    down(quiet=True)
    RUN.mkdir(parents=True, exist_ok=True)
    copy_code()
    settings = build_database()
    env = server_env(settings)
    _write_config(env)
    check_guard(env)
    _start("fake_llm", [PY, "-m", "uvicorn", "e2e.fake_llm:app", "--host", "127.0.0.1",
                        "--port", str(LLM_PORT), "--log-level", "warning"], ROOT, env)
    _wait(f"http://127.0.0.1:{LLM_PORT}/health", 30, "fake model")
    _start("yorik", [PY, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
                     "--port", str(PORT), "--loop", "asyncio"], APP, env)
    _wait(f"{BASE}/api/health", 180, "test Yorik")
    _log(f"test Yorik is up at {BASE}")
    seed(settings)
    _log(f"ready — logins in {RUN / 'household.json'}, password {PASSWORD}")


def down(quiet: bool = False) -> None:
    _stop("yorik")
    _stop("fake_llm")
    try:
        drop_database()
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            _log(f"database not dropped: {exc}")
    if not quiet:
        _log("test household stopped and thrown away")


def status() -> None:
    for name in ("fake_llm", "yorik"):
        pid_file = RUN / f"{name}.pid"
        alive = False
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 0)
                alive = True
            except ProcessLookupError:
                pass
        print(f"{name}: {'running' if alive else 'stopped'}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"up": up, "down": down, "status": status}.get(cmd, status)()
