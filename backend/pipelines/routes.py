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

from ..messages import tr

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])

ACTIONS = {"mail_senden", "uebergabe"}


def _uid(user: dict) -> str:
    return str(user["id"])


def _own(pipeline_id: int, user: dict) -> dict[str, Any]:
    p = store.get(pipeline_id, _uid(user))
    if not p:
        raise HTTPException(404, tr("pipelines.not_found", user=user))
    return p


def _require_enabled(user: dict) -> None:
    if not store.person_enabled(_uid(user)):
        raise HTTPException(403, tr("pipelines.off_for_you", user=user))


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
        raise HTTPException(409, tr("pipelines.busy", user_id=engine._owner_of(pipeline_id)))


# ─── me, people ─────────────────────────────────────────────────────

@router.get("/me")
def me(user: dict = Depends(current_user)) -> dict[str, Any]:
    role = (user.get("role") or "").lower()
    return {"enabled": store.person_enabled(_uid(user)),
            "can_manage_people": role in store.ADULT_ROLES}


@router.get("/people")
def people(user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    if (user.get("role") or "").lower() not in store.ADULT_ROLES:
        raise HTTPException(403, tr("pipelines.parents_only", user=user))
    return store.people_settings((user.get("role") or "").lower())


class PersonSwitch(BaseModel):
    enabled: bool


@router.put("/people/{user_id}")
def switch_person(user_id: str, body: PersonSwitch, user: dict = Depends(current_user)) -> dict[str, Any]:
    role = (user.get("role") or "").lower()
    if role not in store.ADULT_ROLES:
        raise HTTPException(403, tr("pipelines.parents_only", user=user))
    target_role = store.role_of(user_id)
    if not target_role:
        raise HTTPException(404, tr("pipelines.person_not_found", user=user))
    if target_role in store.ADMIN_ROLES and role not in store.ADMIN_ROLES:
        raise HTTPException(403, tr("pipelines.admin_only", user=user))
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
        store.event(pipeline_id, "status", tr("pipelines.event.drafting_failed", user_id=owner, error=type(exc).__name__))
    config = dict(p["config"], drafting=False)
    if plan:
        store.replace_steps(pipeline_id, [
            {"action": d["action"], "after_days": d["after_days"], "payload": d["payload"]}
            for d in store.steps(pipeline_id) if d["status"] == "erledigt"] + plan["steps"])
        fields: dict[str, Any] = {"config_json": config}
        if plan["goal"]:
            fields["goal"] = plan["goal"]
        store.update(pipeline_id, **fields)
        store.event(pipeline_id, "status", tr("pipelines.event.drafted", user_id=owner))
    else:
        store.update(pipeline_id, config_json=config)
        store.event(pipeline_id, "status", tr("pipelines.event.llm_unreachable", user_id=owner))


class CreateError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def create_follow_up(owner: str, mail_id: int) -> int:
    """A follow-up pipeline in draft from one sent mail; the reminders are
    written afterwards by draft_with_llm. Shared by the route and the
    chat's pipeline skill."""
    if not store.person_enabled(owner):
        raise CreateError(403, tr("pipelines.off_for_you", user_id=owner))
    mail = mail_src.load_sent_mail(owner, mail_id)
    if not mail:
        raise CreateError(404, tr("pipelines.mail_not_found", user_id=owner))
    if not mail.get("is_sent"):
        raise CreateError(400, tr("pipelines.only_sent", user_id=owner))
    origin = nachfassen.origin_from_mail(mail)
    if not origin["to"]:
        raise CreateError(400, tr("pipelines.no_recipient", user_id=owner))
    since = store.to_dt(origin["sent_at"]) or store.now()
    subject = origin["subject"] or tr("pipelines.no_subject", user_id=owner)
    pid = store.create(
        owner, kind=nachfassen.KIND, title=subject,
        goal=tr("pipelines.goal", user_id=owner, subject=subject), origin=origin,
        features=mail_src.features_from_mail(mail),
        config=dict(nachfassen.default_config(), drafting=True),
        since_at=since,
    )
    store.replace_steps(pid, nachfassen.default_steps(origin, owner))
    store.event(pid, "status", tr("pipelines.event.created", user_id=owner))
    return pid


@router.post("", status_code=201)
def create(body: CreateBody, background: BackgroundTasks, user: dict = Depends(current_user)) -> dict[str, Any]:
    owner = _uid(user)
    try:
        pid = create_follow_up(owner, body.mail_id)
    except CreateError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    background.add_task(draft_with_llm, pid, owner)
    return _detail(store.get(pid, owner))


@router.post("/{pipeline_id}/redraft")
def redraft(pipeline_id: int, background: BackgroundTasks, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Let the model write the open reminders again."""
    p = _own(pipeline_id, user)
    if p["state"] not in ("entwurf", "laeuft", "pausiert"):
        raise HTTPException(409, tr("pipelines.finished", user=user))
    if p["config"].get("drafting"):
        raise HTTPException(409, tr("pipelines.already_drafting", user=user))
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
            raise HTTPException(400, tr("pipelines.bad_send_days", user=user))
        if not (0 <= c.send_from_hour < c.send_to_hour <= 24):
            raise HTTPException(400, tr("pipelines.bad_window", user=user))
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
        raise HTTPException(409, tr("pipelines.finished", user=user))
    if p["config"].get("drafting"):
        raise HTTPException(409, tr("pipelines.drafting_retry", user=user))
    done = [s for s in store.steps(pipeline_id) if s["status"] == "erledigt"]
    new = []
    for s in body.steps:
        if s.action not in ACTIONS:
            raise HTTPException(400, tr("pipelines.unknown_step", user=user, action=s.action))
        if not (0 <= s.after_days <= 365):
            raise HTTPException(400, tr("pipelines.bad_days", user=user))
        payload: dict[str, Any] = {}
        if s.action == "mail_senden":
            to = [a.strip() for a in (s.payload.get("to") or []) if isinstance(a, str) and "@" in a]
            if not to:
                raise HTTPException(400, tr("pipelines.reminder_needs_recipient", user=user))
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
        raise HTTPException(409, tr("pipelines.drafting", user=user))
    if not store.approve_step(pipeline_id, step_id, body.approved):
        raise HTTPException(404, tr("pipelines.step_not_found", user=user))
    return _detail(_own(pipeline_id, user))


# ─── run state ──────────────────────────────────────────────────────

@router.post("/{pipeline_id}/start")
def start(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    _require_enabled(user)
    p = _own(pipeline_id, user)
    if p["state"] != "entwurf":
        raise HTTPException(409, tr("pipelines.already_started", user=user))
    if p["config"].get("drafting"):
        raise HTTPException(409, tr("pipelines.drafting", user=user))
    open_mail = [s for s in store.steps(pipeline_id) if s["status"] == "offen" and s["action"] == "mail_senden"]
    if any(not s["approved"] for s in open_mail):
        raise HTTPException(409, tr("pipelines.approve_all_first", user=user))
    store.update(pipeline_id, state="laeuft", next_run_at=store.now(), attention=None, attention_json=None)
    store.event(pipeline_id, "mensch", tr("pipelines.event.started", user=user))
    return _detail(_own(pipeline_id, user))


@router.post("/{pipeline_id}/pause")
def pause(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["state"] != "laeuft":
        raise HTTPException(409, tr("pipelines.not_running", user=user))
    store.update(pipeline_id, state="pausiert", next_run_at=None)
    store.event(pipeline_id, "mensch", tr("pipelines.event.paused", user=user))
    return _detail(_own(pipeline_id, user))


@router.post("/{pipeline_id}/resume")
def resume(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    _require_enabled(user)
    p = _own(pipeline_id, user)
    if p["state"] != "pausiert":
        raise HTTPException(409, tr("pipelines.not_paused", user=user))
    store.update(pipeline_id, state="laeuft", next_run_at=store.now())
    store.event(pipeline_id, "mensch", tr("pipelines.event.resumed", user=user))
    return _detail(_own(pipeline_id, user))


class FinishBody(BaseModel):
    note: Optional[str] = None


@router.post("/{pipeline_id}/finish")
def finish(pipeline_id: int, body: FinishBody, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["state"] in ("erledigt", "abgebrochen"):
        raise HTTPException(409, tr("pipelines.already_ended", user=user))
    store.update(pipeline_id, state="erledigt", attention=None, attention_json=None, next_run_at=None,
                 finished_at=store.now(), result_json={"by": "person", "note": (body.note or "")[:500]})
    store.event(pipeline_id, "mensch", tr("pipelines.event.marked_done", user=user))
    return _detail(_own(pipeline_id, user))


@router.post("/{pipeline_id}/cancel")
def cancel(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["state"] in ("erledigt", "abgebrochen"):
        raise HTTPException(409, tr("pipelines.already_ended", user=user))
    store.update(pipeline_id, state="abgebrochen", attention=None, attention_json=None,
                 next_run_at=None, finished_at=store.now())
    store.event(pipeline_id, "mensch", tr("pipelines.event.cancelled", user=user))
    return _detail(_own(pipeline_id, user))


@router.delete("/{pipeline_id}", status_code=204)
def delete(pipeline_id: int, user: dict = Depends(current_user)):
    p = _own(pipeline_id, user)
    if p["state"] in ("laeuft", "pausiert"):
        raise HTTPException(409, tr("pipelines.end_first", user=user))
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
                raise engine.NotNow(tr("pipelines.not_now.text_changed", user=user))
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
            store.event(pipeline_id, "mensch", tr("pipelines.event.answer_confirmed", user=user), {"mail_id": body.mail_id})
        else:
            feats = dict(p["features"])
            dismissed = list(feats.get("dismissed_mail_ids") or [])
            if body.mail_id not in dismissed:
                dismissed.append(body.mail_id)
            feats["dismissed_mail_ids"] = dismissed
            store.update(pipeline_id, features_json=feats, attention=None, attention_json=None,
                         next_run_at=store.now() if p["state"] == "laeuft" else None)
            store.event(pipeline_id, "mensch", tr("pipelines.event.not_the_answer", user=user), {"mail_id": body.mail_id})
    finally:
        engine.unlock(pipeline_id)
    return _detail(_own(pipeline_id, user))


@router.post("/{pipeline_id}/continue")
def continue_after_own_reply(pipeline_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    """The person wrote to the other side themselves and wants the
    pipeline to keep following anyway."""
    p = _own(pipeline_id, user)
    if p["attention"] != "selbst_geantwortet":
        raise HTTPException(409, tr("pipelines.nothing_to_confirm", user=user))
    feats = dict(p["features"])
    ids = list(feats.get("dismissed_sent_ids") or [])
    for m in (p["attention_detail"] or {}).get("mails", []):
        if m["id"] not in ids:
            ids.append(m["id"])
    feats["dismissed_sent_ids"] = ids
    store.update(pipeline_id, features_json=feats, attention=None, attention_json=None,
                 next_run_at=store.now())
    store.event(pipeline_id, "mensch", tr("pipelines.event.own_mail_seen", user=user))
    return _detail(_own(pipeline_id, user))


class UnclearBody(BaseModel):
    was_sent: bool


@router.post("/{pipeline_id}/unclear")
def resolve_unclear(pipeline_id: int, body: UnclearBody, user: dict = Depends(current_user)) -> dict[str, Any]:
    p = _own(pipeline_id, user)
    if p["attention"] != "versand_unklar":
        raise HTTPException(409, tr("pipelines.nothing_to_clarify", user=user))
    step_id = (p["attention_detail"] or {}).get("step_id")
    if body.was_sent and step_id:
        store.mark_step_done(int(step_id))
        store.event(pipeline_id, "mensch", tr("pipelines.event.was_sent", user=user))
    else:
        store.event(pipeline_id, "mensch", tr("pipelines.event.was_not_sent", user=user))
    store.update(pipeline_id, attention=None, attention_json=None, next_run_at=store.now())
    return _detail(_own(pipeline_id, user))
