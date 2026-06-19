"""Custom Vanna tools that let the LLM drive the browser UI.

The LLM can call:

- `show_calendar(view, anchor_date, highlight_event_ids, reason)` — switches the
  dashboard's calendar view and (optionally) highlights specific events. Used
  for "when was I at the doctor last", "show me next week", etc.

- `list_calendar_layouts()` — returns the layouts available in the marketplace
  catalogue. The agent can then explain choices to the user; the frontend can
  open a picker modal in response.

Both tools return a small `ToolResult` so the LLM has something to summarize,
AND record their structured args into module-level ContextVars that the
`/api/ask` response surfaces as a `ui_actions` array. The browser reads that
array and dispatches DOM events.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Any, Dict, List, Literal, Optional, Type

log = logging.getLogger("homeos.ui_tools")

from pydantic import BaseModel, Field
from .agent.vanna_shim import Tool, ToolContext, ToolResult

from . import apps as apps_mod
from . import connectors
from . import documents as documents_mod


# ---------------------------------------------------------------------------
# Per-request capture: the audit logger / drain reads from this after
# `agent.send_message(...)` finishes and clears it again on the next call.
# ---------------------------------------------------------------------------

_pending_ui_actions: ContextVar[List[Dict[str, Any]]] = ContextVar("_pending_ui_actions", default=[])


def reset_ui_actions() -> None:
    _pending_ui_actions.set([])


def get_ui_actions() -> List[Dict[str, Any]]:
    return list(_pending_ui_actions.get())


def _append(action: Dict[str, Any]) -> None:
    cur = list(_pending_ui_actions.get())
    cur.append(action)
    _pending_ui_actions.set(cur)


# ---------------------------------------------------------------------------
# show_calendar
# ---------------------------------------------------------------------------

class ShowCalendarArgs(BaseModel):
    view: Literal["month", "week", "day"] = Field(
        description="Which calendar view to display. 'week' for 7-day hourly grid, 'month' for the full-month overview, 'day' for a single-day focus."
    )
    anchor_date: str = Field(
        description="ISO date (YYYY-MM-DD) that should be visible in the view. For 'week', the week containing this date is shown. For 'month', the month containing it.",
    )
    highlight_event_ids: List[int] = Field(
        default_factory=list,
        description="Optional list of event row IDs (from the events table) to highlight in the view. Use when the user asks about a specific event ('when was I at the doctor last' → highlight that row).",
    )
    reason: str = Field(
        default="",
        description="Very short, user-visible explanation of why this view was opened (e.g. 'last doctor visit', 'this week's free slots'). Shown in the response panel.",
    )


class ShowCalendarTool(Tool[ShowCalendarArgs]):
    @property
    def name(self) -> str:
        return "show_calendar"

    @property
    def description(self) -> str:
        return (
            "Open a specific calendar view for the user in the browser. "
            "Call this whenever the user wants to *see* their calendar — e.g. 'show me this week', "
            "'when was I at the doctor last' (find the row with run_sql first, then call this with that "
            "event's id and the week containing it), 'what's free Thursday'. After calling this, the "
            "browser switches view and highlights the events you list. Always set 'reason' so the user "
            "knows why the view changed."
        )

    def get_args_schema(self) -> Type[ShowCalendarArgs]:
        return ShowCalendarArgs

    async def execute(self, context: ToolContext, args: ShowCalendarArgs) -> ToolResult:
        action = {
            "type": "show_calendar",
            "view": args.view,
            "anchor_date": args.anchor_date,
            "highlight_event_ids": args.highlight_event_ids,
            "reason": args.reason,
        }
        _append(action)
        summary = f"Displayed {args.view} view anchored on {args.anchor_date}"
        if args.highlight_event_ids:
            summary += f", highlighting event(s) {args.highlight_event_ids}"
        if args.reason:
            summary += f" ({args.reason})"
        return ToolResult(success=True, result_for_llm=summary)


# ---------------------------------------------------------------------------
# list_calendar_layouts — marketplace stub
# ---------------------------------------------------------------------------

# In v2 this fetches from a remote catalogue and supports user uploads. For MVP
# we ship a hardcoded list that mirrors the bundled layouts in frontend/layouts/.
LAYOUT_CATALOGUE: List[Dict[str, Any]] = [
    {
        "id": "yorik-calendar",
        "name": "Yorik Calendar",
        "author": "Yorik core",
        "description": "Month + week grid with colored event dots and an hourly week view. The default.",
        "tags": ["bundled", "month", "week"],
        "rating": None,
        "installed": True,
    },
    # Apple-style layout is temporarily hidden — its in-iframe toolbar
    # hasn't been ported to the new pattern, so the user gets stuck without
    # controls if they switch to it. Re-enable when apple.js is updated.
    # {
    #     "id": "apple",
    #     "name": "Apple-style minimal",
    #     "author": "Yorik core",
    #     "description": "Large date numbers, soft rounded cards, minimal chrome.",
    #     "tags": ["bundled", "minimal", "list"],
    #     "rating": None,
    #     "installed": True,
    # },
]


# ---------------------------------------------------------------------------
# Connector tools — bridge the LLM to the connector layer (weather, etc).
# ---------------------------------------------------------------------------

class TriggerConnectorArgs(BaseModel):
    name: str = Field(description="Connector name, e.g. 'weather'. Use list_connectors to discover.")
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters specific to the connector — see its params_schema in list_connectors.",
    )


class TriggerConnectorTool(Tool[TriggerConnectorArgs]):
    @property
    def name(self) -> str:
        return "trigger_connector"

    @property
    def description(self) -> str:
        return (
            "Call an installed connector (external integration: weather, email, maps, etc) "
            "by name with the parameters it expects. Returns a dict with the connector's "
            "result OR {ok: false, error: '...'} on failure. Use list_connectors first if "
            "you don't know what's available. Examples: "
            "trigger_connector(name='weather', params={'city': 'Berlin'}) returns "
            "{temp, condition, icon, humidity_pct, ...}."
        )

    def get_args_schema(self) -> Type[TriggerConnectorArgs]:
        return TriggerConnectorArgs

    async def execute(self, context: ToolContext, args: TriggerConnectorArgs) -> ToolResult:
        result = await connectors.invoke(args.name, args.params or {})
        # Stringify for the LLM so it can summarize naturally.
        if result.get("ok") is False:
            return ToolResult(
                success=False,
                result_for_llm=f"Connector '{args.name}' failed: {result.get('error')}",
                error=result.get("error"),
            )
        summary = ", ".join(f"{k}={v}" for k, v in result.items() if k != "ok")
        return ToolResult(success=True, result_for_llm=f"{args.name}: {summary}")


class ListConnectorsArgs(BaseModel):
    query: str = Field(default="", description="Optional substring filter on name/tags/description.")


class ListConnectorsTool(Tool[ListConnectorsArgs]):
    @property
    def name(self) -> str:
        return "list_connectors"

    @property
    def description(self) -> str:
        return (
            "List installed external-integration connectors (weather, email, maps, etc.) "
            "with their parameter schemas. Call this whenever the user asks 'what can you "
            "connect to' / 'what services are installed' / 'show me my integrations'. "
            "Returns a list of {name, description, params_schema, requires_auth}."
        )

    def get_args_schema(self) -> Type[ListConnectorsArgs]:
        return ListConnectorsArgs

    async def execute(self, context: ToolContext, args: ListConnectorsArgs) -> ToolResult:
        q = (args.query or "").lower().strip()
        all_specs = connectors.list_all()
        if q:
            picked = [s for s in all_specs if q in s.name.lower() or q in s.description.lower() or any(q in t.lower() for t in s.tags)]
        else:
            picked = all_specs
        entries = [connectors.to_catalogue_entry(s) for s in picked]
        # Surface to the UI too so a frontend modal could render the list.
        _append({"type": "show_connectors", "connectors": entries})
        if not entries:
            return ToolResult(success=True, result_for_llm=f"No connectors match '{args.query}'.")
        names = ", ".join(f"{e['name']} ({e['version']})" for e in entries)
        return ToolResult(success=True, result_for_llm=f"{len(entries)} connector(s): {names}")


# ---------------------------------------------------------------------------
# install_connector — stub for Wave 1. In Wave 3 this imports an n8n workflow
# template + opens the OAuth modal. Today, built-in connectors are always
# pre-installed; this tool just confirms or explains.
# ---------------------------------------------------------------------------

class InstallConnectorArgs(BaseModel):
    name: str = Field(description="Connector name to install (from the marketplace catalogue).")


class InstallConnectorTool(Tool[InstallConnectorArgs]):
    @property
    def name(self) -> str:
        return "install_connector"

    @property
    def description(self) -> str:
        return (
            "Install or configure a connector. For Python connectors that need credentials "
            "(email-imap, banking-fints, etc.), this opens a form modal so the user enters "
            "their IMAP/SMTP details, API key, etc. For n8n-backed connectors (email-gmail, "
            "sms-twilio), this opens an OAuth flow. Use whenever the user asks to set up an "
            "integration OR a connector returned {needs_install: true}."
        )

    def get_args_schema(self) -> Type[InstallConnectorArgs]:
        return InstallConnectorArgs

    async def execute(self, context: ToolContext, args: InstallConnectorArgs) -> ToolResult:
        from . import credential_store  # sibling module; deferred to avoid cycle
        spec = connectors.get(args.name)
        if not spec:
            return ToolResult(
                success=False,
                result_for_llm=(
                    f"Connector '{args.name}' isn't in this box's catalogue. Available: "
                    f"{', '.join(s.name for s in connectors.list_all())}. "
                    "Community connectors from the marketplace need a separate fetch step "
                    "(not wired in this release)."
                ),
                error="not_in_catalogue",
            )

        if not spec.requires_auth:
            return ToolResult(
                success=True,
                result_for_llm=f"'{spec.name}' needs no setup — it's ready to use right now.",
            )

        if spec.backend == "builtin":
            # Emit UI action that opens the credentials form modal.
            already = credential_store.get(spec.name) is not None
            _append({
                "type": "open_credentials_form",
                "connector_name": spec.name,
                "connector_description": spec.description,
                "install_hint": spec.install_hint,
                "credentials_schema": spec.credentials_schema,
                "is_reconfigure": already,
            })
            verb = "Re-open" if already else "Open"
            return ToolResult(
                success=True,
                result_for_llm=(
                    f"{verb}ing the credentials form for '{spec.name}'. "
                    f"Once the user enters and saves them, the connector is ready to use."
                ),
            )

        if spec.backend == "n8n":
            from . import n8n_client
            if not n8n_client.is_configured():
                _append({
                    "type": "open_n8n_setup",
                    "connector_name": spec.name,
                    "n8n_base_url": os.getenv("HOMEOS_N8N_BASE_URL", "http://127.0.0.1:5678"),
                })
                return ToolResult(
                    success=False,
                    result_for_llm=(
                        f"'{spec.name}' is n8n-backed but n8n isn't configured yet. "
                        "I've opened the n8n setup wizard for the user — they need to create "
                        "an owner account + API key in n8n, paste the key into Yorik's "
                        "Settings → n8n tab, then re-try this install."
                    ),
                    error="n8n_not_configured",
                )
            # Import the workflow template + activate + store the webhook URL.
            if not spec.n8n_workflow_template:
                return ToolResult(
                    success=False,
                    result_for_llm=f"connector '{spec.name}' has no n8n_workflow_template — bug in the connector module",
                    error="missing_template",
                )
            imp = n8n_client.import_workflow(spec.n8n_workflow_template)
            if not imp.get("ok"):
                return ToolResult(
                    success=False,
                    result_for_llm=f"n8n import failed: {imp.get('error')}",
                    error="import_failed",
                )
            workflow_id = imp.get("workflow_id")
            act = n8n_client.activate_workflow(workflow_id) if workflow_id else {"ok": False, "error": "no workflow_id"}
            # Record what we installed so trigger_connector can route to the right webhook.
            credential_store.put(spec.name, {
                "n8n_workflow_id": workflow_id,
                "webhook_path": spec.n8n_webhook_path,
                "active": bool(act.get("ok")),
            })
            # Many n8n nodes need credentials configured in n8n's own UI (OAuth, etc).
            # We can't do OAuth flows for the user — open n8n so they can finish.
            _append({
                "type": "open_n8n_workflow",
                "connector_name": spec.name,
                "n8n_base_url": os.getenv("HOMEOS_N8N_BASE_URL", "http://127.0.0.1:5678"),
                "workflow_id": workflow_id,
                "active": bool(act.get("ok")),
                "install_hint": spec.install_hint,
            })
            return ToolResult(
                success=True,
                result_for_llm=(
                    f"Imported the '{spec.name}' workflow into n8n (id={workflow_id}). "
                    f"{'Activated.' if act.get('ok') else 'Activation pending — credentials needed.'} "
                    f"{spec.install_hint or ''}"
                ),
            )

        return ToolResult(
            success=False,
            result_for_llm=f"Unknown backend '{spec.backend}' for connector '{spec.name}'.",
            error="unknown_backend",
        )


# open_app was removed — it emitted an unhandled `open_app` ui_action
# and overlapped with the `navigate_to` skill that emits `navigate`
# (the one NavigationBridge actually listens to). Use `navigate_to`.

class ListAppsArgs(BaseModel):
    pass


class ListAppsTool(Tool[ListAppsArgs]):
    @property
    def name(self) -> str:
        return "list_apps"

    @property
    def description(self) -> str:
        return (
            "List the apps installed on this Yorik box (Calendar, Chat, Documents, and any "
            "the user has installed from the community marketplace). Use when the user asks "
            "'what apps are there', 'what can you do', 'show me my apps'."
        )

    def get_args_schema(self) -> Type[ListAppsArgs]:
        return ListAppsArgs

    async def execute(self, context: ToolContext, args: ListAppsArgs) -> ToolResult:
        role = (context.user.group_memberships or ["admin"])[0]
        apps = apps_mod.list_all(role=role)
        lines = [f"{a.icon} {a.name} — {a.description}" for a in apps]
        return ToolResult(
            success=True,
            result_for_llm=f"{len(apps)} app(s) installed:\n" + "\n".join(lines),
        )


# search_documents migrated to backend/skills/search_documents/. The legacy
# Vanna-shaped Tool class lived here; it was registered as a top-level tool
# in parallel to the skills registry, which produced the audit's
# "Unknown skill 'search_documents'" failure when the LLM tried to dispatch
# via invoke_skill(). Class removed; the skill loader picks up the new
# implementation at startup.


class ListLayoutsArgs(BaseModel):
    query: str = Field(
        default="",
        description="Optional substring to filter layouts by name, author, or tag. Empty = list all.",
    )


class ListCalendarLayoutsTool(Tool[ListLayoutsArgs]):
    @property
    def name(self) -> str:
        return "list_calendar_layouts"

    @property
    def description(self) -> str:
        return (
            "List the calendar layouts available in the HomeOS marketplace. "
            "Call this when the user asks 'what calendars are there?', 'show me other styles', "
            "'can I switch the calendar look?'. The browser will open a picker modal with the "
            "returned layouts so the user can preview and select one. Always summarize the choices "
            "briefly in your reply."
        )

    def get_args_schema(self) -> Type[ListLayoutsArgs]:
        return ListLayoutsArgs

    async def execute(self, context: ToolContext, args: ListLayoutsArgs) -> ToolResult:
        q = (args.query or "").lower().strip()
        if q:
            filtered = [
                lay for lay in LAYOUT_CATALOGUE
                if q in lay["name"].lower()
                or q in lay["author"].lower()
                or any(q in t.lower() for t in lay["tags"])
            ]
        else:
            filtered = list(LAYOUT_CATALOGUE)
        _append({"type": "open_layout_picker", "layouts": filtered})
        if not filtered:
            return ToolResult(success=True, result_for_llm=f"No layouts found matching '{args.query}'.")
        names = ", ".join(f"{lay['name']} ({lay['id']})" for lay in filtered)
        return ToolResult(
            success=True,
            result_for_llm=f"{len(filtered)} layout(s) available: {names}. Frontend modal opened.",
        )


# ---------------------------------------------------------------------------
# use_skill — dispatch into the Yorik skills registry
# ---------------------------------------------------------------------------
#
# The agent reads /api/skills to know what's available, then calls
# use_skill(name='find_photo', args={'query': 'Berlin trip'}). This is
# the unification point — every LLM-callable capability is a skill,
# every external one (Paperless search, Immich, WhatsApp draft, calendar
# lookup, …) goes through the same dispatch path. Adding a new skill =
# drop a folder, restart; the agent picks it up automatically.

class UseSkillArgs(BaseModel):
    name: str = Field(..., description="The skill's `name` field from /api/skills.")
    args: Dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments for the skill. See the skill's `inputs` schema in /api/skills.",
    )


class UseSkillTool(Tool[UseSkillArgs]):
    @property
    def name(self) -> str:
        return "use_skill"

    @property
    def description(self) -> str:
        return (
            "Dispatch a named Yorik skill. Skills are discoverable capabilities — "
            "things like 'find_photo' (Immich CLIP search), 'find_document' (Paperless "
            "RAG), 'whatsapp_draft' (compose a chat reply), 'check_calendar' "
            "(availability lookup), 'whatsapp_briefing' (inbox summary). "
            "Call list_skills first if you don't know what's available. "
            "Each skill has its own input schema — read it from /api/skills/<name> "
            "before invoking. Returns whatever the skill returns (typically a dict)."
        )

    def get_args_schema(self) -> Type[UseSkillArgs]:
        return UseSkillArgs

    async def execute(self, context: ToolContext, args: UseSkillArgs) -> ToolResult:
        from .skills import get_registry, SkillContext, SkillError
        reg = get_registry()
        skill = reg.get(args.name)
        if not skill:
            return ToolResult(
                success=False,
                result_for_llm=f"Unknown skill '{args.name}'. Call list_skills to see available.",
                error="unknown_skill",
            )
        # Pull the requesting role + user_id off the in-tree ToolContext.
        # Without explicitly passing user_id, SkillContext silently defaults
        # to 1 (registry.py:65) — which on fresh installs is the seeded
        # "Admin" user, so every skill ran as that phantom user instead of
        # the logged-in one. That broke owner_user_id on every INSERT and
        # made auto_route_calendar pick the Shared calendar instead of
        # the real user's Personal one.
        role = "admin"
        # Three places user_id might live, in priority order:
        #   1. context.user_id   — in-tree ToolContext exposes it directly
        #   2. context.metadata["user_id"]  — set by vanna_adapter when bridging
        #      the in-tree ctx to the Vanna-shim ToolContext (the User.id
        #      field is typed str so the int can't ride there)
        #   3. None — every chat-path skill that writes owner_user_id /
        #      pending_actions.user_id will fail loudly rather than
        #      silently leaking to the seeded Admin (former bug fixed
        #      in c301985)
        # Accept either int (pre-Phase-E) or str/UUID (post-Phase-E).
        # The hard restriction to `int` here was the silent reason
        # chat-driven mutations (add_task, add_event, …) were arriving
        # at pending_actions with user_id=None and crashing the
        # NOT NULL constraint, even though the calling user was
        # correctly authenticated.
        def _looks_like_user_id(v: Any) -> bool:
            return isinstance(v, (int, str)) and bool(v)

        user_id: Any = getattr(context, "user_id", None)
        if not _looks_like_user_id(user_id):
            meta = getattr(context, "metadata", None) or {}
            try:
                meta_uid = meta.get("user_id") if isinstance(meta, dict) else None
            except Exception:
                meta_uid = None
            if _looks_like_user_id(meta_uid):
                user_id = meta_uid
        try:
            user = getattr(context, "user", None)
            if user:
                # In-tree User (agent/context.py) has .role + .id directly.
                if getattr(user, "role", None):
                    role = user.role
                elif getattr(user, "group_memberships", None):
                    # Legacy Vanna User shape — role lives in group_memberships.
                    role = next(iter(user.group_memberships), "admin")
        except Exception:
            pass
        if not _looks_like_user_id(user_id):
            user_id = None
        # Plumb conversation_id from the ToolContext (both the
        # in-tree dataclass and the vanna_shim BaseModel expose it
        # with the same attribute name) so skill_invocations rows
        # for chat-driven calls get the conv id stamped. Without
        # this, `WHERE conversation_id=?` queries to reconstruct
        # what the LLM did in chat X never find anything.
        cid = getattr(context, "conversation_id", None) or None
        ctx = SkillContext(reg, role=role, user_id=user_id, conversation_id=cid)
        try:
            result = await reg.invoke(args.name, ctx=ctx, **(args.args or {}))
        except SkillError as e:
            err_text = str(e)
            err_lower = err_text.lower()
            # Detect bad-args / TypeError-style errors (registry wraps
            # them as SkillError("bad args to skill 'X': ..."). For
            # these, the LLM should call skill_view to learn the real
            # shape — retrying with another guess just loops. Different
            # error class from permission/business errors gets a
            # different override that points at skill_view by name.
            looks_like_bad_args = (
                "bad args to skill" in err_lower
                or "got an unexpected keyword argument" in err_lower
                or "missing" in err_lower and "required" in err_lower
                and "argument" in err_lower
            )
            if looks_like_bad_args:
                # When the registry's bad-args formatter produced a
                # high-confidence SUGGESTION (cutoff 0.7), tell the
                # LLM to retry directly with the corrected kwarg —
                # no skill_view round-trip. Saves ~2-3s + one LLM
                # call when the fix is obvious. When no suggestion
                # is present (truly novel arg shape), fall back to
                # the old "go read skill_view" path.
                has_suggestion = "SUGGESTION:" in err_text
                if has_suggestion:
                    next_step = (
                        "NEXT STEP: retry `invoke_skill` with the "
                        "SUGGESTION above. AVAILABLE KEYS are listed in "
                        "the error — pick from those, do NOT guess "
                        "again."
                    )
                else:
                    next_step = (
                        f"NEXT STEP: call `skill_view(name='{args.name}')` "
                        f"to read the manifest's `inputs` section, then "
                        f"retry `invoke_skill` with the correct argument "
                        f"names. Do NOT retry with the same args."
                    )
                return ToolResult(
                    success=False,
                    result_for_llm=(
                        f"WRONG ARGS for skill '{args.name}'.\n"
                        f"Error: {err_text}\n\n"
                        f"{next_step} Do NOT report success."
                    ),
                    error=err_text,
                )
            # Architecture-level enforcement against the "hallucinate
            # success after a failed skill" pattern (K2 regression: LLM
            # told a member 'wurde gelöscht' after the ownership gate
            # raised EventPermissionError). The polite "Skill failed: X"
            # message wasn't loud enough — Qwen would silently swallow
            # it. Override the LLM-facing string so the SAME OBSERVATION
            # that previously got swallowed now SCREAMS at the model.
            return ToolResult(
                success=False,
                result_for_llm=(
                    f"TOOL FAILED — DO NOT REPORT SUCCESS TO THE USER.\n"
                    f"Skill '{args.name}' raised:\n"
                    f"  {err_text}\n\n"
                    f"You MUST: (1) quote this error to the user in their language "
                    f"(German if they wrote German), (2) suggest the next step the "
                    f"error itself indicates ('ask the owner', 'check the args', "
                    f"'try a different time', etc.). DO NOT invent a different "
                    f"reason for the failure. DO NOT silently retry the same call. "
                    f"DO NOT claim the operation succeeded — it did NOT."
                ),
                error=err_text,
            )
        # When a skill returns `_llm_hint`, surface BOTH the hint (steering
        # rule) AND the structured data (truth). Hiding data caused the
        # briefing hallucination bug (audit 2026-06-01-rerun #T8): hint
        # said "5 events, write one sentence" but LLM had no events to be
        # accurate about, so it invented five. Including both keeps the
        # narration discipline AND gives the LLM ground truth when a
        # legitimate enumeration is asked for.
        if isinstance(result, dict) and result.get("_llm_hint"):
            from .skills.skill_tool import _compact_json
            hint = str(result["_llm_hint"])
            data = {k: v for k, v in result.items() if k != "_llm_hint"}
            if data:
                preview = (
                    f"{hint}\n\n"
                    f"Source data (quote only what is here — do not invent rows):\n"
                    f"{_compact_json(data, max_chars=1500)}"
                )
            else:
                preview = hint
        else:
            preview = str(result)
            if len(preview) > 800:
                preview = preview[:800] + "…(truncated)"
        return ToolResult(success=True, result_for_llm=preview)


# ─────────────────────── Phase 2 (toolfix): unified surface ──────────
#
# `invoke_skill` is the canonical way to run any skill. `UseSkillTool`
# is no longer registered to the LLM (it was the back-compat alias
# during Phase 2; live testing in Phase 5 confirmed the LLM defaulted
# to it instead of `invoke_skill`, defeating the asymmetry-removal we
# did this whole branch for). The class survives only for any internal
# callers that may still construct it directly; do not re-register it
# without a strong reason.

class InvokeSkillTool(UseSkillTool):
    """Drop-in subclass of UseSkillTool with a new name + description.
    The dispatch logic, role resolution, _llm_hint extraction — all
    identical. The split exists purely so the LLM sees a clear pair:
    skill_view(name) reads the manual, invoke_skill(name, args) runs
    the operation."""

    @property
    def name(self) -> str:
        return "invoke_skill"

    @property
    def description(self) -> str:
        return (
            "Run a Yorik skill by name. Pair with `skill_view` — read the "
            "full manifest there first when you need the inputs/outputs "
            "schema or behavioural detail. Returns whatever the skill "
            "returns (typically a dict). Permission check runs server-"
            "side against the caller's role."
        )


def _find_latest_invoke_for_skill(
    messages: list, target_skill: str,
) -> Optional[tuple]:
    """Walk the current turn's messages (since the last user message)
    looking for the most recent invoke_skill call targeting
    `target_skill`. Returns (was_rejected, inner_args_dict) on a
    match, or None if the model hasn't tried to invoke this skill yet
    this turn.

    Used by SkillViewTool to detect the "invoke → REJECTED →
    skill_view" pattern and echo the original args back so the 9B
    Qwen doesn't hallucinate "I called it successfully" without
    actually re-calling invoke_skill.
    """
    import json as _json
    # Anchor: find the cutoff (last user message index + 1).
    cutoff = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            cutoff = i + 1
            break
    # Map tool_call_id → (inner skill name, inner args) for every
    # invoke_skill call the assistant has emitted this turn.
    call_meta: dict = {}
    for m in messages[cutoff:]:
        if not isinstance(m, dict): continue
        if m.get("role") != "assistant": continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name") != "invoke_skill": continue
            try:
                parsed = _json.loads(fn.get("arguments") or "{}")
            except Exception:
                continue
            if not isinstance(parsed, dict): continue
            tc_id = tc.get("id")
            if not tc_id: continue
            call_meta[tc_id] = (
                parsed.get("name"),
                parsed.get("args") or {},
            )
    # Walk results from newest to oldest; stop on the first invoke
    # result for target_skill. Different-skill results are skipped.
    for i in range(len(messages) - 1, cutoff - 1, -1):
        m = messages[i]
        if not isinstance(m, dict): continue
        if m.get("role") != "tool": continue
        if m.get("name") != "invoke_skill": continue
        meta = call_meta.get(m.get("tool_call_id"))
        if not meta: continue
        inner_name, inner_args = meta
        if inner_name != target_skill: continue
        content = m.get("content") or ""
        # Detect the synthetic skill_view-needed hint emitted by the
        # agent loop at backend/agent/loop.py — _INVOKE_NEEDS_READ_HINT.
        # The wording changed 2026-06-14 to drop the poison-token
        # "REJECTED" opener (cloud Qwen treats it as terminal); keep
        # the legacy prefix in the detection so historical conversation
        # rows still get the correct echo-args treatment, and add the
        # current "Read skill_view(" opener for new rows.
        was_rejected = isinstance(content, str) and (
            content.startswith("Read skill_view(")
            or content.startswith("REJECTED — call skill_view")
        )
        return (was_rejected, inner_args)
    return None


def _count_skill_view_reads_since_user(messages: list, target_skill: str) -> int:
    """Count how many times `skill_view(name=target_skill)` has been
    called by the assistant since the latest user message in `messages`.

    Used by SkillViewTool to break manual-rereading loops: after the
    first read, a second call returns a tight directive instead of the
    manifest again. The window resets on each user turn.
    """
    import json as _json
    cutoff = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            cutoff = i + 1
            break
    count = 0
    for m in messages[cutoff:]:
        if not isinstance(m, dict): continue
        if m.get("role") != "assistant": continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name") != "skill_view": continue
            try:
                parsed = _json.loads(fn.get("arguments") or "{}")
            except Exception:
                continue
            if isinstance(parsed, dict) and parsed.get("name") == target_skill:
                count += 1
    return count


class SkillViewArgs(BaseModel):
    name: str = Field(..., description="Skill name as it appears in the skill index.")


class SkillViewTool(Tool[SkillViewArgs]):
    """Returns the structured manifest for one skill — frontmatter +
    body — so the LLM can read its full when_to_use, key concepts,
    inputs/outputs schema, etc. before deciding whether to call it.
    Cheap; no side effects."""

    @property
    def name(self) -> str:
        return "skill_view"

    @property
    def description(self) -> str:
        return (
            "Read the full manifest of one Yorik skill — when_to_use, "
            "inputs/outputs schema, examples, pitfalls, verification "
            "notes. Use this when the skill index entry isn't enough "
            "and you need the manual before committing to a call. "
            "Returns a structured dict; does NOT execute the skill."
        )

    def get_args_schema(self) -> Type[SkillViewArgs]:
        return SkillViewArgs

    async def execute(self, context: ToolContext, args: SkillViewArgs) -> ToolResult:
        from .skills import get_registry
        from .skills.registry import _get_disabled_skills
        if args.name in _get_disabled_skills():
            return ToolResult(
                success=False,
                result_for_llm=(
                    f"Skill {args.name!r} is disabled by admin. It won't appear "
                    "in your skill index and you can't invoke it. Pick a "
                    "different one from the index."
                ),
                error="skill_disabled",
            )
        view = get_registry().view(args.name)
        if view is None:
            return ToolResult(
                success=False,
                result_for_llm=(
                    f"No skill named {args.name!r}. The skill index in your "
                    "system prompt lists every available skill — re-scan it "
                    "for the right name. Don't guess names that aren't on "
                    "the list."
                ),
                error="unknown_skill",
            )
        # Re-read guard. Pattern from the mietminderung iteration-cap
        # loop (2026-06-18 trace): the LLM reads a manifest, doesn't
        # commit, reads it again, repeat 7-11 times in one turn, never
        # invokes. Each re-read returns identical 1-4 KB of prose that
        # doesn't help break the indecision. After the first read this
        # turn, return a tight directive instead of the manifest — the
        # second read produces NO new context, so the loop dies. If the
        # LLM legitimately needs a re-read (e.g., after the manifest
        # was pruned by `_prune_recent_skill_view`), the prior invoke
        # is already in history so the LLM has the answers.
        history = getattr(context, "conversation_so_far", []) or []
        prior_reads = _count_skill_view_reads_since_user(history, args.name)
        if prior_reads >= 1:
            return ToolResult(
                success=True,
                result_for_llm=(
                    f"You already read the {args.name!r} manifest this turn "
                    f"({prior_reads}× before this call). Re-reading returns "
                    f"the same bytes and will not help you decide.\n\n"
                    f"NEXT STEP: either (a) call invoke_skill(name="
                    f"{args.name!r}, args={{…}}) now — the rules from the "
                    f"earlier read still apply — or (b) move on to a "
                    f"different skill. Do NOT call skill_view({args.name!r}) "
                    f"a third time."
                ),
            )
        # Compact JSON-ish rendering, body included. The LLM reads this
        # once and then calls invoke_skill with the right args.
        import json
        rendered = json.dumps(view, indent=2)

        # If this skill_view was preceded by a REJECTED invoke_skill
        # for the same skill in this turn, append a directive echoing
        # the original args back. Without it, the 9B Qwen occasionally
        # reads the manifest, then narrates "task done" to the user
        # without re-invoking the skill — see project_yorik_compose
        # _skill_view_hallucination memory note (observed 2026-06-13
        # in conv 018dc984…). Echoing the args is the lightest-touch
        # nudge: it doesn't change the loop, doesn't change other
        # skills, and is a no-op when the model is on the happy path
        # (first-time skill_view with no prior REJECTED).
        latest = _find_latest_invoke_for_skill(
            getattr(context, "conversation_so_far", []) or [],
            args.name,
        )
        if latest is not None:
            was_rejected, inner_args = latest
            if was_rejected:
                rendered += (
                    "\n\nRESUME — your last invoke_skill(name="
                    f"{args.name!r}) was REJECTED because you hadn't "
                    "called skill_view first. You have now. Your "
                    "NEXT action MUST be to re-call invoke_skill("
                    f"name={args.name!r}, args={json.dumps(inner_args, ensure_ascii=False)}) "
                    "— adjust the args if the manifest above shows "
                    "you got something wrong, otherwise pass them "
                    "verbatim. Do NOT reply to the user before that "
                    "call returns; the user is waiting on the actual "
                    "result, not a narrated one."
                )
        return ToolResult(success=True, result_for_llm=rendered)


class ListSkillsArgs(BaseModel):
    query: str = Field(default="", description="Optional substring filter on name/description/tags.")


class ListSkillsTool(Tool[ListSkillsArgs]):
    @property
    def name(self) -> str:
        return "list_skills"

    @property
    def description(self) -> str:
        return (
            "List Yorik's available skills (callable capabilities). Returns each skill's "
            "name, description, when-to-use, and inputs schema so you can pick the right one "
            "for the user's intent and call use_skill(name, args) with the correct arguments."
        )

    def get_args_schema(self) -> Type[ListSkillsArgs]:
        return ListSkillsArgs

    async def execute(self, context: ToolContext, args: ListSkillsArgs) -> ToolResult:
        from .skills import get_registry
        from .skills.registry import _get_disabled_skills
        disabled = _get_disabled_skills()
        q_raw = (args.query or "").strip().lower()
        # Tokenize on whitespace, hyphens, underscores. The LLM types
        # "compose draft" (space) but the skill is named compose_draft
        # (underscore) — a whole-phrase substring match misses it. Each
        # token >2 chars (stopwords excluded by length) must hit name,
        # description, OR a tag for the row to qualify. Tokens are
        # ALL-required so "calendar list" doesn't drown the LLM with
        # every calendar AND every list-style skill.
        _STOPWORDS = {"the","and","for","with","skill","skills"}
        tokens = [
            t for t in q_raw.replace("-", " ").replace("_", " ").split()
            if len(t) > 2 and t not in _STOPWORDS
        ]
        out = []
        for s in get_registry().all():
            if s.name in disabled:
                continue
            if tokens:
                name_l = s.name.lower()
                desc_l = s.description.lower()
                tags_l = [t.lower() for t in s.tags]
                matched_all = True
                for tok in tokens:
                    if (tok not in name_l
                            and tok not in desc_l
                            and not any(tok in t for t in tags_l)):
                        matched_all = False
                        break
                if not matched_all:
                    continue
            out.append({
                "name": s.name,
                "description": s.description,
                "when_to_use": s.when_to_use,
                "inputs": s.inputs,
                "tags": s.tags,
            })
        return ToolResult(
            success=True,
            result_for_llm=f"{len(out)} skills available: " +
                            ", ".join(f"{s['name']}({', '.join(s.get('tags', []))})" for s in out)
                            + ". Each has its own inputs schema — see /api/skills/<name> for details.",
        )
