"""REST routes for the Pipelines app (/api/pipelines).

Every route answers for the caller's own pipelines only; someone
else's is a 404, admins included. The per-person switch is for
parents and admins (store.ADULT_ROLES).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth_sessions import current_user
from . import engine, store
from .kinds import nachfassen
from .sources import mail as mail_src

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])

ACTIONS = {"mail_senden", "uebergabe"}


def _uid(user: dict) -> str:
    return str(user["id"])


def _own(pipeline_id: int, user: dict) -> dict[str, Any]:
    p = store.get(pipeline_id, _uid(user))
    if not p:
        raise HTTPException(404, "Pipeline nicht gefunden")
    return p


def _require_enabled(user: dict) -> None:
    if not store.person_enabled(_uid(user)):
        raise HTTPException(403, "Pipelines sind für dich ausgeschaltet.")


def _with_steps(p: dict[str, Any]) -> dict[str, Any]:
    all_steps = store.steps(p["id"])
    estimate = None
    for s in all_steps:
        if s["status"] == "offen":
            estimate = engine.due_at(p, s, all_steps) if estimate is None else \
                estimate + timedelta(days=s["after_days"])
            s["due_at"] = estimate.isoformat()
        else:
            s["due_at"] = None
    p["steps"] = all_steps
    nxt = engine.next_open_step(all_steps)
    p["next_step"] = {"id": nxt["id"], "action": nxt["action"], "due_at": nxt["due_at"],
                      "position": nxt["position"]} if nxt else None
    return p


def _detail(p: dict[str, Any]) -> dict[str, Any]:
    p = _with_steps(p)
    evs = store.events(p["id"])
    p["events"] = evs
    p["last_check"] = next((e["data"] | {"at": e["at"]} for e in evs
                            if e["kind"] == "pruefung" and e["data"]), None)
    p["actions"] = store.actions(p["id"])
    p["quote"] = nachfassen.quote(p["origin"]) if p["kind"] == nachfassen.KIND else ""
    return p


def _locked(pipeline_id: int):
    if not engine.try_lock(pipeline_id):
        raise HTTPException(409, "Yorik prüft diese Pipeline gerade. Bitte gleich noch einmal.")


# ─── me, people ─────────────────────────────────────────────────────

@router.get("/me")
def me(user: dict = Depends(current_user)) -> dict[str, Any]:
    role = (user.get("role") or "").lower()
    return {"enabled": store.person_enabled(_uid(user)),
            "can_manage_people": role in store.ADULT_ROLES}


@router.get("/people")
def people(user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    if (user.get("role") or "").lower() not in store.ADULT_ROLES:
        raise HTTPException(403, "Nur Eltern oder Admins")
    return store.people_settings((user.get("role") or "").lower())


class PersonSwitch(BaseModel):
    enabled: bool


@router.put("/people/{user_id}")
def switch_person(user_id: str, body: PersonSwitch, user: dict = Depends(current_user)) -> dict[str, Any]:
    role = (user.get("role") or "").lower()
    if role not in store.ADULT_ROLES:
        raise HTTPException(403, "Nur Eltern oder Admins")
    target_role = store.role_of(user_id)
    if not target_role:
        raise HTTPException(404, "Person nicht gefunden")
    if target_role in store.ADMIN_ROLES and role not in store.ADMIN_ROLES:
        raise HTTPException(403, "Für Admins kann das nur ein Admin ändern")
    store.set_person_enabled(user_id, body.enabled, _uid(user))
    return {"ok": True}


# ─── list, create ───────────────────────────────────────────────────

@router.get("")
def list_pipelines(user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    out = []
    for p in store.list_for(_uid(user)):
        p = _with_steps(p)
        p.pop("origin", None)
        out.append(p)
    return out


@router.get("/sent-mails")
def sent_mails(user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    return mail_src.recent_sent(_uid(user))


class CreateBody(BaseModel):
    mail_id: int


def draft_with_llm(pipeline_id: int, owner: str) -> None:
    """Let the model plan and write the reminders (runs after the
    response; the page shows "Yorik schreibt…" meanwhile)."""
    p = store.get(pipeline_id, owner)
    if not p:
        return
    try:
        plan = nachfassen.llm_steps(p["origin"], owner)
    except Exception as exc:  # noqa: BLE001
        plan = None
        store.event(pipeline_id, "status", f"Schreiben fehlgeschlagen: {type(exc).__name__}")
    config = dict(p["config"], drafting=False)
    if plan:
        store.replace_steps(pipeline_id, [
            {"action": d["action"], "after_days": d["after_days"], "payload": d["payload"]}
            for d in store.steps(pipeline_id) if d["status"] == "erledigt"] + plan["steps"])
        fields: dict[str, Any] = {"config_json": config}
        if plan["goal"]:
            fields["goal"] = plan["goal"]
        store.update(pipeline_id, **fields)
        store.event(pipeline_id, "status", "Yorik hat die Erinnerungen geschrieben und die Abstände vorgeschlagen")
    else:
        store.update(pipeline_id, config_json=config)
        store.event(pipeline_id, "status", "Das Sprachmodell war nicht erreichbar — Vorlage bleibt stehen")


@router.post("", status_code=201)
def create(body: CreateBody, background: BackgroundTasks, user: dict = Depends(current_user)) -> dict[str, Any]:
    _require_enabled(user)
    owner = _uid(user)
    mail = mail_src.load_sent_mail(owner, body.mail_id)
    if not mail:
        raise HTTPException(404, "Mail nicht gefunden")
    if not mail.get("is_sent"):
        raise HTTPException(400, "Nur eine gesendete Mail lässt sich verfolgen.")
    origin = nachfassen.origin_from_mail(mail)
    if not origin["to"]:
        raise HTTPException(400, "Die Mail hat keinen Empfänger.")
    since = store.to_dt(origin["sent_at"]) or store.now()
    subject = origin["subject"] or "(ohne Betreff)"
    pid = store.create(
        owner, kind=nachfassen.KIND, title=subject,
        goal=f"Antwort auf „{subject}“", origin=origin,
        features=mail_src.features_from_mail(mail),
        config=dict(nachfassen.default_config(), drafting=True),
        since_at=since,
    )
    store.replace_steps(pid, nachfassen.default_steps(origin, owner))
    store.event(pid, "status", "Entwurf angelegt aus der gesendeten Mail")
    background.add_task(draft_with_llm, pid, owner)
    return _detail(store.get(pid, owner))


@router.post("/{pipeline_id}/redraft")
def redraft(pipeline_id: int, background: BackgroundTasks, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Let the model write the open reminders again."""
    p = _own(pipeline_id, user)
    if p["state"] not in ("entwurf", "laeuft", "pausiert"):
        raise HTTPException(409, "Die Pipeline ist beendet.")
    if p["config"].get("drafting"):
        raise HTTPException(409, "Yorik schreibt schon.")
    store.update(pipeline_id, config_json=dict(p["config"], drafting=True))
    background.add_task(draft_with_llm, pipeline_id, _uid(user))
    return _detail(_own(pipeline_id, user))


@router.get("/{pipeline_id}")
def detail(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    return _detail(_own(pipeline_id, user))


# ─── edit ───────────────────────────────────────────────────────────

class Features(BaseModel):
    addresses: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)
    words: list[str] = Field(default_factory=list)


class Config(BaseModel):
    send_days: str = "alle"
    send_from_hour: int = 8
    send_to_hour: int = 20


class PatchBody(BaseModel):
    title: Optional[str] = None
    goal: Optional[str] = None
    features: Optional[Features] = None
    config: Optional[Config] = None


def _clean(items: list[str]) -> list[str]:
    out = []
    for i in items:
        v = (i or "").strip()
        if v and v not in out:
            out.append(v[:120])
    return out[:30]


@router.patch("/{pipeline_id}")
def patch(pipeline_id: int, body: PatchBody, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    fields: dict[str, Any] = {}
    if body.title is not None and body.title.strip():
        fields["title"] = body.title.strip()[:200]
    if body.goal is not None:
        fields["goal"] = body.goal.strip()[:500]
    if body.features is not None:
        f = body.features
        merged = dict(p["features"])
        merged.update({
            "addresses": [a.lower() for a in _clean(f.addresses)],
            "domains": [d.lower().lstrip("@") for d in _clean(f.domains)],
            "names": [n.lower() for n in _clean(f.names)],
            "numbers": _clean(f.numbers),
            "words": _clean(f.words),
        })
        fields["features_json"] = merged
    if body.config is not None:
        c = body.config
        if c.send_days not in ("alle", "werktags"):
            raise HTTPException(400, "send_days: alle oder werktags")
        if not (0 <= c.send_from_hour < c.send_to_hour <= 24):
            raise HTTPException(400, "Sendefenster ungültig")
        fields["config_json"] = dict(p["config"], **c.model_dump())
    if fields:
        store.update(pipeline_id, **fields)
        if p["state"] == "laeuft":
            store.update(pipeline_id, next_run_at=store.now())
    return _detail(_own(pipeline_id, user))


class StepIn(BaseModel):
    action: str
    after_days: int
    payload: dict[str, Any] = Field(default_factory=dict)


class StepsBody(BaseModel):
    steps: list[StepIn]


@router.put("/{pipeline_id}/steps")
def put_steps(pipeline_id: int, body: StepsBody, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["state"] in ("erledigt", "abgebrochen"):
        raise HTTPException(409, "Die Pipeline ist beendet.")
    if p["config"].get("drafting"):
        raise HTTPException(409, "Yorik schreibt die Erinnerungen gerade. Gleich noch einmal.")
    done = [s for s in store.steps(pipeline_id) if s["status"] == "erledigt"]
    new = []
    for s in body.steps:
        if s.action not in ACTIONS:
            raise HTTPException(400, f"Unbekannter Schritt: {s.action}")
        if not (0 <= s.after_days <= 365):
            raise HTTPException(400, "Tage: 0 bis 365")
        payload: dict[str, Any] = {}
        if s.action == "mail_senden":
            to = [a.strip() for a in (s.payload.get("to") or []) if isinstance(a, str) and "@" in a]
            if not to:
                raise HTTPException(400, "Eine Erinnerung braucht einen Empfänger.")
            payload = {"to": to[:10], "subject": str(s.payload.get("subject") or "")[:300],
                       "body": str(s.payload.get("body") or "")[:20000]}
            for k in ("why", "source", "written_at"):
                if isinstance(s.payload.get(k), str):
                    payload[k] = s.payload[k][:300]
        new.append({"action": s.action, "after_days": s.after_days, "payload": payload})
    if not new or new[-1]["action"] != "uebergabe":
        new.append({"action": "uebergabe", "after_days": 7, "payload": {}})
    # Done steps stay where they are; the new list follows them.
    full = [{"action": d["action"], "after_days": d["after_days"], "payload": d["payload"]}
            for d in done] + new
    store.replace_steps(pipeline_id, full)
    if p["state"] == "laeuft":
        clear = p["attention"] in ("uebergabe", "schritt_faellig")
        store.update(pipeline_id, next_run_at=store.now(),
                     **({"attention": None, "attention_json": None} if clear else {}))
    return _detail(_own(pipeline_id, user))


class ApproveBody(BaseModel):
    approved: bool = True


@router.post("/{pipeline_id}/steps/{step_id}/approve")
def approve(pipeline_id: int, step_id: int, body: ApproveBody,
            user: dict = Depends(current_user)) -> dict[str, Any]:
    if _own(pipeline_id, user)["config"].get("drafting"):
        raise HTTPException(409, "Yorik schreibt die Erinnerungen gerade.")
    if not store.approve_step(pipeline_id, step_id, body.approved):
        raise HTTPException(404, "Schritt nicht gefunden oder schon erledigt")
    return _detail(_own(pipeline_id, user))


# ─── run state ──────────────────────────────────────────────────────

@router.post("/{pipeline_id}/start")
def start(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    _require_enabled(user)
    p = _own(pipeline_id, user)
    if p["state"] != "entwurf":
        raise HTTPException(409, "Schon gestartet")
    if p["config"].get("drafting"):
        raise HTTPException(409, "Yorik schreibt die Erinnerungen gerade.")
    open_mail = [s for s in store.steps(pipeline_id) if s["status"] == "offen" and s["action"] == "mail_senden"]
    if any(not s["approved"] for s in open_mail):
        raise HTTPException(409, "Bitte erst jede Mail freigeben.")
    store.update(pipeline_id, state="laeuft", next_run_at=store.now(), attention=None, attention_json=None)
    store.event(pipeline_id, "mensch", "Gestartet (begleitet: jede Erinnerung fragt vor dem Senden)")
    return _detail(_own(pipeline_id, user))


@router.post("/{pipeline_id}/pause")
def pause(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["state"] != "laeuft":
        raise HTTPException(409, "Läuft nicht")
    store.update(pipeline_id, state="pausiert", next_run_at=None)
    store.event(pipeline_id, "mensch", "Pausiert")
    return _detail(_own(pipeline_id, user))


@router.post("/{pipeline_id}/resume")
def resume(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    _require_enabled(user)
    p = _own(pipeline_id, user)
    if p["state"] != "pausiert":
        raise HTTPException(409, "Nicht pausiert")
    store.update(pipeline_id, state="laeuft", next_run_at=store.now())
    store.event(pipeline_id, "mensch", "Fortgesetzt")
    return _detail(_own(pipeline_id, user))


class FinishBody(BaseModel):
    note: Optional[str] = None


@router.post("/{pipeline_id}/finish")
def finish(pipeline_id: int, body: FinishBody, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["state"] in ("erledigt", "abgebrochen"):
        raise HTTPException(409, "Schon beendet")
    store.update(pipeline_id, state="erledigt", attention=None, attention_json=None, next_run_at=None,
                 finished_at=store.now(), result_json={"by": "person", "note": (body.note or "")[:500]})
    store.event(pipeline_id, "mensch", "Als erledigt markiert")
    return _detail(_own(pipeline_id, user))


@router.post("/{pipeline_id}/cancel")
def cancel(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["state"] in ("erledigt", "abgebrochen"):
        raise HTTPException(409, "Schon beendet")
    store.update(pipeline_id, state="abgebrochen", attention=None, attention_json=None,
                 next_run_at=None, finished_at=store.now())
    store.event(pipeline_id, "mensch", "Abgebrochen")
    return _detail(_own(pipeline_id, user))


@router.delete("/{pipeline_id}", status_code=204)
def delete(pipeline_id: int, user: dict = Depends(current_user)):
    p = _own(pipeline_id, user)
    if p["state"] in ("laeuft", "pausiert"):
        raise HTTPException(409, "Erst beenden oder abbrechen")
    store.delete(pipeline_id)


# ─── the engine, on the person's word ───────────────────────────────

@router.post("/{pipeline_id}/check")
def check_now(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["state"] == "laeuft":
        _locked(pipeline_id)
        try:
            engine.process(pipeline_id)
        finally:
            engine.unlock(pipeline_id)
    else:
        chk = engine.kind_of(p).check(p)
        engine._log_check(p, chk, force=True)
    return _detail(_own(pipeline_id, user))


class SendBody(BaseModel):
    despite_stale: bool = False
    approve: bool = False   # "Freigeben und senden": approve the text shown, then send
    seen_body: Optional[str] = None  # the text the person saw; approval only for exactly that


@router.post("/{pipeline_id}/steps/{step_id}/send")
def send(pipeline_id: int, step_id: int, body: SendBody, user: dict = Depends(current_user)) -> dict[str, Any]:
    _require_enabled(user)
    _own(pipeline_id, user)
    _locked(pipeline_id)
    try:
        p = _own(pipeline_id, user)
        if body.approve:
            current = next((s for s in store.steps(pipeline_id) if s["id"] == step_id), None)
            if not current or (current["payload"].get("body") or "") != (body.seen_body or ""):
                raise engine.NotNow("Der Text hat sich inzwischen geändert. Bitte noch einmal lesen.")
            store.approve_step(pipeline_id, step_id, True)
        res = engine.send_step(p, step_id, despite_stale=body.despite_stale)
    except engine.NotNow as exc:
        raise HTTPException(409, {"reason": exc.reason, "detail": exc.detail})
    finally:
        engine.unlock(pipeline_id)
    if not res["ok"]:
        raise HTTPException(502, {"reason": f"Versand fehlgeschlagen: {res.get('error')}"})
    return _detail(_own(pipeline_id, user))


class AnswerBody(BaseModel):
    mail_id: int
    is_answer: bool


@router.post("/{pipeline_id}/answer")
def answer(pipeline_id: int, body: AnswerBody, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    _locked(pipeline_id)
    try:
        if body.is_answer:
            store.update(pipeline_id, state="erledigt", attention=None, attention_json=None,
                         next_run_at=None, finished_at=store.now(),
                         result_json={"by": "person", "answer_mail_id": body.mail_id})
            store.event(pipeline_id, "mensch", "Antwort bestätigt — erledigt", {"mail_id": body.mail_id})
        else:
            feats = dict(p["features"])
            dismissed = list(feats.get("dismissed_mail_ids") or [])
            if body.mail_id not in dismissed:
                dismissed.append(body.mail_id)
            feats["dismissed_mail_ids"] = dismissed
            store.update(pipeline_id, features_json=feats, attention=None, attention_json=None,
                         next_run_at=store.now() if p["state"] == "laeuft" else None)
            store.event(pipeline_id, "mensch", "Keine Antwort — weiter warten", {"mail_id": body.mail_id})
    finally:
        engine.unlock(pipeline_id)
    return _detail(_own(pipeline_id, user))


@router.post("/{pipeline_id}/continue")
def continue_after_own_reply(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    """The person wrote to the other side themselves and wants the
    pipeline to keep following anyway."""
    p = _own(pipeline_id, user)
    if p["attention"] != "selbst_geantwortet":
        raise HTTPException(409, "Nichts zu bestätigen")
    feats = dict(p["features"])
    ids = list(feats.get("dismissed_sent_ids") or [])
    for m in (p["attention_detail"] or {}).get("mails", []):
        if m["id"] not in ids:
            ids.append(m["id"])
    feats["dismissed_sent_ids"] = ids
    store.update(pipeline_id, features_json=feats, attention=None, attention_json=None,
                 next_run_at=store.now())
    store.event(pipeline_id, "mensch", "Eigene Mail gesehen — weiter verfolgen")
    return _detail(_own(pipeline_id, user))


class UnclearBody(BaseModel):
    was_sent: bool


@router.post("/{pipeline_id}/unclear")
def resolve_unclear(pipeline_id: int, body: UnclearBody, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["attention"] != "versand_unklar":
        raise HTTPException(409, "Nichts zu klären")
    step_id = (p["attention_detail"] or {}).get("step_id")
    if body.was_sent and step_id:
        store.mark_step_done(int(step_id))
        store.event(pipeline_id, "mensch", "Erinnerung war gesendet (bestätigt)")
    else:
        store.event(pipeline_id, "mensch", "Erinnerung war nicht gesendet — Schritt bleibt offen")
    store.update(pipeline_id, attention=None, attention_json=None, next_run_at=store.now())
    return _detail(_own(pipeline_id, user))
