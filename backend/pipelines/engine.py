"""The pipeline engine: wait, check, act, next.

The engine knows nothing about mail. A kind (kinds/*.py) says how to
check and how to perform a step; the engine decides *whether* to, by
fixed rules that hold for every kind:

* Only a check that says "sicher_nicht" lets a step go outward.
  "vielleicht" and "kann_nicht_pruefen" always go to the person.
* A step is checked again right before it acts.
* Every outward action claims a fixed key first (store.action_begin);
  a restart never sends twice, an unclear send is asked about.
* Outward only inside the pipeline's send window.
* After the last step, the person takes over.

Stage 1 runs in the accompanied mode only: when a step is due, the
person gets a notification and sends with one tap (routes.send_step).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import store
from .kinds import nachfassen

log = logging.getLogger("yorik.pipelines.engine")

KINDS = {nachfassen.KIND: nachfassen}

TICK_S = 60
BATCH = 20
LOCK_S = 300
RECHECK_WAITING = timedelta(hours=6)       # a quiet pipeline is looked at this often anyway
RECHECK_STALE = timedelta(minutes=15)      # a source that cannot be trusted is retried this often
KICK_IF_OLDER_S = 5 * 60                   # before asking to send, fetch mail that is older than this
KICK_WAIT = timedelta(minutes=3)
STUCK_SEND = timedelta(minutes=10)

# Attention that waits for the person; the engine does not touch it.
HUMAN_ATTENTION = {"vielleicht", "selbst_geantwortet", "versand_unklar", "uebergabe", "person_aus"}

_loop: Optional[asyncio.AbstractEventLoop] = None
_kicked: dict[int, datetime] = {}

ATTENTION_TEXT = {
    "vielleicht": "Ist das die Antwort?",
    "kann_nicht_pruefen": "Yorik kann gerade nicht sicher prüfen",
    "schritt_faellig": "Keine Antwort — Erinnerung senden?",
    "uebergabe": "Keine Antwort nach allen Erinnerungen — jetzt du",
    "selbst_geantwortet": "Du hast selbst geschrieben — weiter verfolgen?",
    "versand_unklar": "Unklar, ob die Erinnerung rausging",
}


def kind_of(p: dict[str, Any]):
    return KINDS[p["kind"]]


# ─── time ───────────────────────────────────────────────────────────

def due_at(p: dict[str, Any], step: dict[str, Any], all_steps: list[dict[str, Any]]) -> datetime:
    base = store.to_dt(p["since_at"])
    for s in all_steps:
        if s["position"] < step["position"] and s["status"] == "erledigt" and s["done_at"]:
            base = max(base, store.to_dt(s["done_at"]))
    return base + timedelta(days=int(step["after_days"]))


def next_open_step(all_steps: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for s in all_steps:
        if s["status"] == "offen":
            return s
    return None


def in_window(config: dict[str, Any], at: datetime) -> bool:
    local = at.astimezone()
    if config.get("send_days") == "werktags" and local.weekday() >= 5:
        return False
    return int(config.get("send_from_hour", 8)) <= local.hour < int(config.get("send_to_hour", 20))


def next_window(config: dict[str, Any], at: datetime) -> datetime:
    t = at.astimezone().replace(minute=0, second=0, microsecond=0)
    for _ in range(24 * 8):
        t += timedelta(hours=1)
        if in_window(config, t):
            return t.astimezone(timezone.utc)
    return at + timedelta(days=1)


# ─── attention ──────────────────────────────────────────────────────

def _set_attention(p: dict[str, Any], attention: Optional[str], detail: Any = None,
                   next_run: Optional[datetime] = None, notify: bool = True) -> None:
    changed = attention != p.get("attention")
    store.update(p["id"], attention=attention, attention_json=detail, next_run_at=next_run)
    if changed and attention and notify and attention != "person_aus":
        try:
            from .. import notifications
            notifications.create(
                p["owner_user_id"], "pipeline", ATTENTION_TEXT.get(attention, "Pipeline braucht dich"),
                body=p["title"], payload={"pipeline_id": p["id"], "attention": attention},
                navigate_to=f"/r/pipelines/{p['id']}",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("pipeline %s: notification failed: %s", p["id"], exc)


def _log_check(p: dict[str, Any], chk: dict[str, Any], force: bool = False) -> None:
    """A check goes into the history when its result changed or when a
    step hangs on it; quiet re-checks every few hours do not pile up."""
    last = next((e for e in store.events(p["id"], limit=30) if e["kind"] == "pruefung"), None)
    if not force and last and (last["data"] or {}).get("result") == chk["result"]:
        return
    text = {"vielleicht": f"Möglicher Treffer ({len(chk['candidates'])})",
            "kann_nicht_pruefen": "Kann nicht sicher prüfen",
            "sicher_nicht": "Noch keine Antwort"}.get(chk["result"], chk["result"])
    store.event(p["id"], "pruefung", f"{text} — {chk['summary']}", {
        "result": chk["result"], "summary": chk["summary"], "problems": chk["problems"],
        "candidates": chk["candidates"][:10], "took_over": chk["took_over"][:5],
        "sources": chk["sources"],
    })


# ─── one pipeline ───────────────────────────────────────────────────

def process(pipeline_id: int) -> None:
    """One look at one pipeline. The caller holds its lock."""
    p = store.get(pipeline_id, None)
    if not p or p["state"] != "laeuft":
        return
    if not store.person_enabled(p["owner_user_id"]):
        if p["attention"] in HUMAN_ATTENTION:
            store.update(p["id"], next_run_at=None)
        else:
            _set_attention(p, "person_aus", None, None)
        return
    if p["attention"] in HUMAN_ATTENTION:
        store.update(p["id"], next_run_at=None)
        return

    kind = kind_of(p)
    all_steps = store.steps(p["id"])
    step = next_open_step(all_steps)
    if step is None:
        _set_attention(p, "uebergabe", None, None)
        return

    chk = kind.check(p)
    now = store.now()
    due = due_at(p, step, all_steps)
    _log_check(p, chk, force=now >= due and chk["result"] != "sicher_nicht")

    if chk["result"] == "vielleicht":
        _set_attention(p, "vielleicht", {"candidates": chk["candidates"][:10],
                                          "summary": chk["summary"]}, None)
        return
    if chk["took_over"]:
        _set_attention(p, "selbst_geantwortet", {"mails": chk["took_over"][:5]}, None)
        return

    if now < due:
        store.update(p["id"], attention=None, attention_json=None,
                     next_run_at=min(due, now + RECHECK_WAITING))
        return

    if chk["result"] == "kann_nicht_pruefen":
        _set_attention(p, "kann_nicht_pruefen", {"problems": chk["problems"]}, now + RECHECK_STALE)
        return

    # sicher_nicht, and the step is due.
    if step["action"] == "uebergabe":
        store.mark_step_done(step["id"])
        store.event(p["id"], "status", "Letzter Schritt erreicht, keine Antwort — Übergabe an dich")
        _set_attention(p, "uebergabe", {"summary": chk["summary"]}, None)
        return

    if not in_window(p["config"], now):
        store.update(p["id"], next_run_at=next_window(p["config"], now))
        return

    # Fetch mail once more right before asking, so the question is not
    # asked on a 20-minute-old view of the inbox.
    stale = [a for a in chk["sources"]["mail"]["accounts"]
             if a["sync_age_s"] is not None and a["sync_age_s"] > KICK_IF_OLDER_S]
    kicked = _kicked.get(p["id"])
    if stale and (kicked is None or now - kicked > timedelta(minutes=30)):
        for a in stale:
            _kick_mail(a["id"])
        _kicked[p["id"]] = now
        store.update(p["id"], next_run_at=now + KICK_WAIT)
        return

    detail = {"step_id": step["id"], "position": step["position"], "summary": chk["summary"],
              "due_at": due.isoformat()}
    if p["mode"] == "begleitet" or not step["approved"]:
        _set_attention(p, "schritt_faellig", detail, now + RECHECK_WAITING)
        return
    # Autonomous mode is stage 4; until then nothing here sends by itself.
    _set_attention(p, "schritt_faellig", detail, now + RECHECK_WAITING)


def _kick_mail(account_id: int) -> None:
    if _loop is None:
        return
    try:
        from .. import email_fetcher
        asyncio.run_coroutine_threadsafe(email_fetcher.reload_account(int(account_id)), _loop)
    except Exception as exc:  # noqa: BLE001
        log.warning("pipelines: could not kick mail account %s: %s", account_id, exc)


# ─── sending on the person's word ───────────────────────────────────

class NotNow(Exception):
    """The step cannot go now; `.detail` says why (shown to the person)."""

    def __init__(self, reason: str, detail: Any = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def send_step(p: dict[str, Any], step_id: int, *, despite_stale: bool = False) -> dict[str, Any]:
    """The person pressed "Senden". Check again right now; send only on
    'sicher_nicht' (or 'kann_nicht_pruefen' when the person, having seen
    why, says send anyway). The caller holds the lock."""
    if p["state"] != "laeuft":
        raise NotNow("Die Pipeline läuft nicht.")
    all_steps = store.steps(p["id"])
    step = next_open_step(all_steps)
    if not step or step["id"] != step_id:
        raise NotNow("Das ist nicht der nächste Schritt.")
    if step["action"] != "mail_senden":
        raise NotNow("Dieser Schritt sendet nichts.")
    if not step["approved"]:
        raise NotNow("Dieser Schritt ist nicht freigegeben.")

    chk = kind_of(p).check(p)
    _log_check(p, chk, force=True)
    if chk["result"] == "vielleicht":
        _set_attention(p, "vielleicht", {"candidates": chk["candidates"][:10],
                                          "summary": chk["summary"]}, None, notify=False)
        raise NotNow("Es gibt eine Mail, die die Antwort sein könnte. Bitte erst ansehen.",
                     {"candidates": chk["candidates"][:10]})
    if chk["took_over"]:
        _set_attention(p, "selbst_geantwortet", {"mails": chk["took_over"][:5]}, None, notify=False)
        raise NotNow("Du hast inzwischen selbst geschrieben.", {"mails": chk["took_over"][:5]})
    if chk["result"] == "kann_nicht_pruefen" and not despite_stale:
        raise NotNow("Yorik kann gerade nicht sicher prüfen.", {"problems": chk["problems"]})

    attempt = 1 + sum(1 for a in store.actions(p["id"]) if a["step_id"] == step["id"])
    res = kind_of(p).perform(p, step, attempt)
    if res["ok"]:
        store.mark_step_done(step["id"])
        store.event(p["id"], "aktion", f"Erinnerung gesendet an {', '.join(step['payload'].get('to') or [])}",
                    {"message_id": res.get("message_id"), "step_id": step["id"],
                     "despite_stale": despite_stale and chk["result"] == "kann_nicht_pruefen"})
        store.update(p["id"], attention=None, attention_json=None, next_run_at=store.now())
        return {"ok": True}
    store.event(p["id"], "aktion", f"Versand fehlgeschlagen: {res.get('error')}", {"step_id": step["id"]})
    _set_attention(p, "versand_unklar", {"step_id": step["id"], "error": res.get("error")}, None,
                   notify=False)
    return {"ok": False, "error": res.get("error")}


# ─── planner ────────────────────────────────────────────────────────

def recover_stuck_sends() -> int:
    """A send that started and never finished (crash, restart): if the
    sent copy exists it went out; otherwise ask the person."""
    from ..database import conn_ctx
    from .sources import mail as mail_src
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT a.idem_key, a.step_id, a.message_id, a.pipeline_id, p.owner_user_id "
            "FROM pipeline_actions a JOIN pipelines p ON p.id = a.pipeline_id "
            "WHERE a.status = 'sendet' AND a.created_at < ?",
            (store.now() - STUCK_SEND,),
        ).fetchall()
    for r in rows:
        owner = str(r["owner_user_id"])
        if r["message_id"] and mail_src.message_was_sent(owner, r["message_id"]):
            store.action_finish(r["idem_key"], "gesendet")
            if r["step_id"]:
                store.mark_step_done(int(r["step_id"]))
            store.event(int(r["pipeline_id"]), "aktion", "Erinnerung war gesendet (nach Neustart bestätigt)")
        else:
            store.action_finish(r["idem_key"], "unklar")
            p = store.get(int(r["pipeline_id"]), None)
            if p:
                store.event(p["id"], "aktion", "Versand unterbrochen — unklar, ob die Mail rausging")
                _set_attention(p, "versand_unklar", {"step_id": r["step_id"]}, None)
    return len(rows)


def claim_due(limit: int = BATCH) -> list[int]:
    from ..database import conn_ctx
    with conn_ctx() as c:
        rows = c.execute(
            "UPDATE pipelines SET locked_until = now() + (? * interval '1 second') "
            "WHERE id IN (SELECT id FROM pipelines WHERE state = 'laeuft' AND next_run_at <= now() "
            "  AND (locked_until IS NULL OR locked_until < now()) "
            "  ORDER BY next_run_at LIMIT ? FOR UPDATE SKIP LOCKED) RETURNING id",
            (LOCK_S, limit),
        ).fetchall()
    return [int(r["id"]) for r in rows]


def try_lock(pipeline_id: int, seconds: int = 120) -> bool:
    from ..database import conn_ctx
    with conn_ctx() as c:
        r = c.execute(
            "UPDATE pipelines SET locked_until = now() + (? * interval '1 second') "
            "WHERE id = ? AND (locked_until IS NULL OR locked_until < now()) RETURNING id",
            (seconds, pipeline_id),
        ).fetchone()
    return r is not None


def unlock(pipeline_id: int) -> None:
    from ..database import conn_ctx
    with conn_ctx() as c:
        c.execute("UPDATE pipelines SET locked_until = NULL WHERE id = ?", (pipeline_id,))


def tick() -> dict[str, int]:
    stuck = recover_stuck_sends()
    ids = claim_due()
    for pid in ids:
        try:
            process(pid)
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline %s: processing failed", pid)
            store.event(pid, "status", f"Fehler bei der Prüfung: {type(exc).__name__}")
            store.update(pid, next_run_at=store.now() + RECHECK_STALE)
        finally:
            unlock(pid)
    return {"processed": len(ids), "stuck": stuck}


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop
    from .. import workers
    workers.register("pipelines", kind="scheduler", expected_interval_s=TICK_S)

    async def _run():
        while True:
            try:
                out = await asyncio.get_running_loop().run_in_executor(None, tick)
                workers.heartbeat("pipelines", "ok", f"{out['processed']} geprüft")
            except Exception as exc:  # noqa: BLE001
                log.warning("pipelines: tick failed: %s", exc)
                workers.heartbeat("pipelines", "error", str(exc)[:120])
            await asyncio.sleep(TICK_S)

    loop.create_task(_run(), name="pipelines-scheduler")
