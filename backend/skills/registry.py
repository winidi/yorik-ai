"""Skills registry — discovery, loading, dispatch, cross-skill composition."""

from __future__ import annotations

import importlib.util
import inspect
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

log = logging.getLogger("yorik.skills")

SKILLS_DIR = Path(__file__).parent
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


class SkillError(Exception):
    """Raised when a skill is missing, malformed, or fails at runtime."""


@dataclass
class Skill:
    """One loaded skill. Fields mirror skill.md frontmatter; `entrypoint`
    is the resolved async callable from the matching skill.py."""
    name: str
    description: str
    when_to_use: str = ""
    when_not_to_use: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    cost: str = ""
    permissions: list[str] = field(default_factory=lambda: ["admin"])
    side_effects: str = ""
    tags: list[str] = field(default_factory=list)
    # Optional category for grouping in the LLM's index. If unset, the
    # first tag is used as a fallback so existing skills get a sensible
    # default without manual editing.
    category: str = ""
    body: str = ""
    entrypoint: Optional[Callable] = None
    manifest_path: Optional[Path] = None

    @property
    def effective_category(self) -> str:
        """Category used in the LLM-visible index. Explicit `category`
        wins; otherwise the first tag; otherwise 'misc'."""
        if self.category:
            return self.category
        if self.tags:
            return self.tags[0]
        return "misc"

    def to_manifest(self) -> dict[str, Any]:
        """JSON-safe dict for the /api/skills endpoint and the agent's
        skill-picker prompt. Excludes the entrypoint callable and the
        full markdown body (the body is fetched on-demand by name)."""
        return {
            "name":            self.name,
            "description":     self.description,
            "when_to_use":     self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "inputs":          self.inputs,
            "outputs":         self.outputs,
            "cost":            self.cost,
            "permissions":     self.permissions,
            "side_effects":    self.side_effects,
            "tags":            self.tags,
            "category":        self.effective_category,
        }

    def to_index_entry(self) -> dict[str, Any]:
        """Compact row for the LLM-visible skill index. Keep this tight —
        every loaded skill contributes one of these to the system prompt
        on every turn, so token cost adds up. ~80-120 chars per entry.

        `args` field added after the chat audit caught the LLM
        hallucinating arg names on first invocation (due_before,
        due_after, duration, address × 2 — five distinct skills in
        one 14-turn audit). Showing the canonical arg list inline lets
        the LLM avoid the bad-arg → skill_view → retry round trip.
        Marked required args with `*` so the LLM picks them up first."""
        arg_summary = self._arg_summary()
        out: dict[str, Any] = {
            "name":        self.name,
            "description": self.description,
            "category":    self.effective_category,
            "permissions": self.permissions,
        }
        if arg_summary:
            out["args"] = arg_summary
        return out

    def _arg_summary(self) -> str:
        """Comma-separated arg names with required marker. Capped at
        ~120 chars so a kitchen-sink skill (find_photo) doesn't blow
        the budget."""
        if not isinstance(self.inputs, dict) or not self.inputs:
            return ""
        parts: list[str] = []
        for k, meta in self.inputs.items():
            star = "*" if isinstance(meta, dict) and meta.get("required") else ""
            parts.append(f"{k}{star}")
        joined = ", ".join(parts)
        if len(joined) > 120:
            joined = joined[:117] + "…"
        return joined

    def to_view(self) -> dict[str, Any]:
        """Full structured manifest, returned by skill_view(name) when
        the LLM needs the detail. Includes the markdown body verbatim
        so any prose authored under section headings (When to Use, Key
        Concepts, Verification, …) flows through as-is."""
        return {
            **self.to_manifest(),
            "body": self.body,
        }


class SkillContext:
    """Passed to every skill's execute() as the first kwarg. Carries
    request-scoped state (role, user) AND the registry handle so
    skills can compose: `await ctx.call_skill("find_photo", query="X")`.
    """

    def __init__(self, registry: "Registry", role: str = "admin",
                 user_id: Optional[int] = None,
                 conversation_id: Optional[str] = None):
        # user_id defaults to None (not 1!) so a missed pass-through at
        # the boundary surfaces as NULL on owner_user_id columns instead
        # of silently picking the seeded "Admin" user. Skills that need
        # a real user_id should validate (see add_calendar_event,
        # add_contact — both handle None already via getattr default).
        self._registry = registry
        self.role = role
        self.user_id = user_id
        # Optional: when populated by the chat path (skill_tool.py),
        # every skill_invocations row gets stamped with this id so
        # `WHERE conversation_id=?` reconstructs what the LLM did in
        # one chat thread. Outside of chat (cron, autodraft, briefings)
        # this stays None and the rows have no conv link, which is fine.
        self.conversation_id = conversation_id

    async def call_skill(self, name: str, **args) -> Any:
        """Invoke another skill from inside a skill. Permission check
        happens in the registry — a skill the current role can't call
        directly is also blocked from being called via composition."""
        return await self._registry.invoke(name, ctx=self, **args)


# ─── Admin-controlled enable/disable ────────────────────────────────
# A skill is "disabled" when its name appears in the comma-separated
# value at app_settings.disabled_skills. Disabled skills are filtered
# out of three LLM-facing surfaces — the skill_index in the system
# prompt (Registry.index), the list_skills tool, and the skill_view
# tool — AND Registry.invoke refuses them with a clean error if the
# LLM names one anyway (defence in depth, e.g. when the LLM is
# carrying a stale name in its conversation memory).
#
# Storage is intentionally minimal — a single row in app_settings.
# No schema migration, no per-row overhead, and a missing row is the
# safe default (no skills disabled). Read on every Registry.index /
# invoke / list-skills call; the DB hit is one indexed PK lookup
# and we trade caching for "admin toggle is live next chat turn."

_DISABLED_SKILLS_KEY = "disabled_skills"


def _get_disabled_skills() -> set[str]:
    """Return the set of currently-disabled skill names. Empty set
    on any error (DB not initialised, table missing in tests, …) —
    fail-open keeps the LLM working when the disable infra itself
    breaks."""
    try:
        from ..database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (_DISABLED_SKILLS_KEY,),
            ).fetchone()
        if not row or not row["value"]:
            return set()
        return {n.strip() for n in str(row["value"]).split(",") if n.strip()}
    except Exception:  # noqa: BLE001
        return set()


def _set_disabled_skills(skills: set[str]) -> None:
    """Persist the disabled-skill set. Empty set clears the row's
    value (we still leave the row in place — cheaper than DELETE +
    INSERT on next write)."""
    from ..database import get_conn
    val = ",".join(sorted(skills))
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value      = excluded.value, "
            "  updated_at = excluded.updated_at",
            (_DISABLED_SKILLS_KEY, val),
        )
        conn.commit()


# ─── UI-only naming-convention category ─────────────────────────────
# Returns a human-readable category for each skill, derived from its
# name. Used ONLY by the /api/skills response (Settings → Skills
# accordion); deliberately NOT wired into Skill.effective_category
# or Registry.index so the LLM's view of skills (which is sorted by
# `category` in the rendered prompt) stays exactly as it has been
# — alphabetical inside one `[misc]` bucket — until we measure that
# categorisation helps tool-pick accuracy.
#
# Order matters: the first substring match wins. Curated specifically
# so that all 59 today-loaded skills land in a meaningful bucket and
# no skill gets misrouted (e.g. find_recipient_address_from_documents
# is Compose, not Documents — the address lookup is part of the
# compose pipeline). New skills that don't match any rule fall into
# "System" rather than vanishing into an "Other" pile.
_CATEGORY_RULES: list[tuple[str, str]] = [
    ("recipient_address", "Compose"),
    ("compose",           "Compose"),
    ("group_price",       "Compose"),
    ("price_table",       "Compose"),
    ("whatsapp",          "WhatsApp"),
    ("email",             "Email"),
    ("photo",             "Photos"),
    ("document",          "Documents"),
    ("bill",              "Bills"),
    ("subtask",           "Tasks"),
    ("task",              "Tasks"),
    ("meeting",           "Calendar"),
    ("travel",            "Calendar"),
    ("calendar",          "Calendar"),
    ("event",             "Calendar"),
    ("contact",           "Contacts"),
    ("provider",          "Contacts"),
    ("person",            "Contacts"),
    ("venue",             "Contacts"),
    ("user",              "Contacts"),
]
_DEFAULT_CATEGORY = "System"


def derive_ui_category(skill_name: str) -> str:
    """Naming-convention category used by Settings → Skills. UI-only —
    do not surface to the LLM (would change the prompt layout)."""
    n = (skill_name or "").lower()
    for substr, cat in _CATEGORY_RULES:
        if substr in n:
            return cat
    return _DEFAULT_CATEGORY


class Registry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    # ── public API ──────────────────────────────────────────────────────

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def index(self, *, role: Optional[str] = None) -> list[dict[str, Any]]:
        """Compact catalog the LLM sees in the system prompt every turn.
        One row per loaded skill: name, short description, category,
        permissions. Filter by role if given — skills whose permission
        list excludes the caller's role are omitted from the menu
        (they'd raise on invoke anyway, so dangling them in the prompt
        wastes tokens and invites confusion). Disabled skills are also
        filtered out so the LLM never sees something it can't actually
        call.

        Sorted by category then name so the LLM scans related skills
        together — calendar/* before contacts/*, etc."""
        disabled = _get_disabled_skills()
        rows: list[dict[str, Any]] = []
        for s in self._skills.values():
            if s.name in disabled:
                continue
            if role is not None and s.permissions:
                if role not in s.permissions and "*" not in s.permissions:
                    continue
            rows.append(s.to_index_entry())
        rows.sort(key=lambda r: (r["category"], r["name"]))
        return rows

    def view(self, name: str) -> Optional[dict[str, Any]]:
        """Full structured manifest for one skill — returned by the
        `skill_view` LLM tool when the model decides it needs the body
        + when_to_use + inputs/outputs detail. Returns None if the
        skill doesn't exist (caller surfaces that to the LLM)."""
        s = self._skills.get(name)
        return s.to_view() if s else None

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            log.warning("skill name collision: %s (replacing)", skill.name)
        self._skills[skill.name] = skill

    async def invoke(self, name: str, /, *, ctx: Optional[SkillContext] = None, **args) -> Any:
        """Dispatch to a skill by name. Creates a default admin context
        if none passed (useful for direct HTTP endpoint calls). Enforces
        the skill's `permissions` list against ctx.role. Logs every
        invocation to `skill_invocations` so the quality dashboard can
        surface per-(skill, llm) success rates."""
        import time
        started = time.monotonic()

        skill = self.get(name)
        if not skill:
            _log_invocation(name, success=False, error="unknown_skill", latency_ms=0,
                            args=args)
            raise SkillError(f"unknown skill: {name!r}")
        if ctx is None:
            ctx = SkillContext(self)
        conv_id = getattr(ctx, "conversation_id", None)
        # Defence in depth: even if the skill_index already hid this
        # skill, the LLM might name it from conversation memory.
        # Refuse with a clean error rather than silently running.
        if name in _get_disabled_skills():
            _log_invocation(name, success=False, error="skill_disabled", latency_ms=0,
                            user_id=getattr(ctx, "user_id", None),
                            conversation_id=conv_id, args=args)
            raise SkillError(
                f"skill {name!r} is disabled by admin — pick a different "
                "skill from the index.\n"
                "For value-seeking questions about documents (amounts, dates, "
                "IBANs), try search_documents → read_document → "
                "read_document_vision.\n"
                "Otherwise pick whichever skill matches the user's actual intent."
            )
        # Phase C T10: platform_admin is the infra role added on top of
        # `admin`. Every existing skill.md lists `admin` as the highest
        # privilege; rather than rewriting every skill manifest, treat
        # platform_admin as inheriting admin-level skill access. The
        # row-level workspace scoping (spaces.user_visible_space_ids) is
        # what actually enforces isolation — skill access is just the
        # outer gate, and platform_admin is *more* privileged than admin
        # by definition.
        effective_role = ctx.role
        if effective_role == "platform_admin" and "admin" in (skill.permissions or []):
            effective_role = "admin"
        if skill.permissions and effective_role not in skill.permissions and "*" not in skill.permissions:
            _log_invocation(name, success=False, error="permission_denied", latency_ms=0,
                            user_id=getattr(ctx, "user_id", None),
                            conversation_id=conv_id, args=args)
            raise SkillError(
                f"role {ctx.role!r} not permitted to call skill {name!r} "
                f"(allowed: {skill.permissions})"
            )
        if skill.entrypoint is None:
            _log_invocation(name, success=False, error="no_entrypoint", latency_ms=0,
                            conversation_id=conv_id, args=args)
            raise SkillError(f"skill {name!r} has no entrypoint")
        # Skills are async; if someone wrote a sync function, run it
        # directly (uncommon but handled).
        uid = getattr(ctx, "user_id", None)
        try:
            if inspect.iscoroutinefunction(skill.entrypoint):
                result = await skill.entrypoint(ctx=ctx, **args)
            else:
                result = skill.entrypoint(ctx=ctx, **args)
            dur = int((time.monotonic() - started) * 1000)
            log.info("skill ok: %s (%dms)", name, dur,
                     extra={"skill": name, "user_id": uid, "duration_ms": dur, "status": "ok"})
            _log_invocation(name, success=True, error=None,
                            latency_ms=dur, user_id=uid,
                            conversation_id=conv_id, args=args, result=result)
            return result
        except SkillError as e:
            dur = int((time.monotonic() - started) * 1000)
            log.warning("skill err: %s (%dms) — %s", name, dur, str(e)[:200],
                        extra={"skill": name, "user_id": uid, "duration_ms": dur, "status": "skill_error"})
            _log_invocation(name, success=False, error=str(e)[:200],
                            latency_ms=dur, user_id=uid,
                            conversation_id=conv_id, args=args)
            raise
        except TypeError as e:
            dur = int((time.monotonic() - started) * 1000)
            log.warning("skill bad_args: %s (%dms) — %s", name, dur, str(e)[:200],
                        extra={"skill": name, "user_id": uid, "duration_ms": dur, "status": "bad_args"})
            _log_invocation(name, success=False, error=f"bad_args: {e}"[:200],
                            latency_ms=dur, user_id=uid,
                            conversation_id=conv_id, args=args)
            raise SkillError(_format_bad_args_error(name, e, skill.inputs))
        except Exception as e:
            dur = int((time.monotonic() - started) * 1000)
            log.exception("skill raised: %s (%dms)", name, dur,
                          extra={"skill": name, "user_id": uid, "duration_ms": dur, "status": "exception"})
            _log_invocation(name, success=False, error=f"{type(e).__name__}: {e}"[:200],
                            latency_ms=dur, user_id=uid,
                            conversation_id=conv_id, args=args)
            raise SkillError(f"skill {name!r} failed: {e}")


_ARGS_CAP = 1024
_RESULT_CAP = 1024


def _format_bad_args_error(skill_name: str, type_error: TypeError,
                             inputs: dict) -> str:
    """Compose a focused bad-args error message that lets the LLM
    fix its kwargs on the SAME turn without a skill_view detour.

    Leads with the original TypeError (preserves the substring
    matchers in ui_tools.py), then a compact AVAILABLE KEYS list,
    then a SUGGESTION line ONLY when a close fuzzy match exists
    (cutoff 0.7 — high enough that "due_date_from" → "start_date_iso"
    won't trigger a wrong suggestion, but "name" → "display_name"
    will). Full schema follows for the cases where the suggestion
    isn't enough."""
    import difflib
    import re
    available = list(inputs.keys()) if isinstance(inputs, dict) else []
    msg = f"bad args to skill {skill_name!r}: {type_error}.\n"
    if available:
        msg += f"  AVAILABLE KEYS: {available}\n"
    bad_kwarg_match = re.search(
        r"unexpected keyword argument ['\"]?([\w_]+)['\"]?", str(type_error)
    )
    if bad_kwarg_match and available:
        bad = bad_kwarg_match.group(1)
        # 1) Substring match — handles "name" → "display_name",
        # "duration" → "duration_minutes", "id" → "task_id", which
        # difflib's character-ratio rates too low to suggest.
        substring_hits = [
            k for k in available
            if bad in k or k in bad
        ]
        suggestion = substring_hits[0] if len(substring_hits) == 1 else None
        # 2) Fall back to difflib for legitimately misspelled keys.
        if not suggestion:
            close = difflib.get_close_matches(bad, available, n=1, cutoff=0.7)
            suggestion = close[0] if close else None
        if suggestion:
            msg += f"  SUGGESTION: did you mean `{suggestion}` instead of `{bad}`?\n"
    msg += f"  Full schema: {inputs}"
    return msg


def _truncate_json(obj: Any, cap: int) -> Optional[str]:
    """JSON-encode obj for the trace; hard-cap at `cap` chars so a giant
    result (e.g. a 100-row find_emails return) can't blow up the DB.
    Falls back to repr() for non-JSON-serialisable objects."""
    if obj is None:
        return None
    try:
        import json as _json
        s = _json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        try:
            s = repr(obj)
        except Exception:  # noqa: BLE001
            return None
    if len(s) > cap:
        s = s[:cap] + f"…[+{len(s) - cap}ch]"
    return s


def _log_invocation(skill_id: str, *, success: bool, error: Optional[str],
                     latency_ms: int, user_id: Optional[int] = None,
                     conversation_id: Optional[str] = None,
                     args: Optional[dict] = None,
                     result: Any = None) -> None:
    """Best-effort telemetry — never raises, never blocks the caller."""
    try:
        from ..database import conn_ctx
        # Lazy import — avoid circular at module load time.
        try:
            from .. import ask as _va
            model = _va.LLM_MODEL
        except Exception:  # noqa: BLE001
            model = None
        args_json = _truncate_json(args, _ARGS_CAP) if args else None
        result_json = _truncate_json(result, _RESULT_CAP) if result is not None else None
        with conn_ctx() as conn:
            conn.execute(
                "INSERT INTO skill_invocations "
                "(skill_id, llm_model, success, error, latency_ms, user_id, "
                " conversation_id, args_json, result_summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (skill_id, model, 1 if success else 0, error, latency_ms, user_id,
                 conversation_id, args_json, result_json),
            )
    except Exception:  # noqa: BLE001
        # We never want telemetry to break the user's flow.
        log.warning("skill telemetry write failed", exc_info=True)


# Module-level singleton — populated by load_all() on app startup.
_registry: Optional[Registry] = None


def get_registry() -> Registry:
    """Returns the loaded registry. Triggers a lazy load if startup
    hasn't called load_all() yet (e.g. test-time access)."""
    global _registry
    if _registry is None:
        _registry = load_all()
    return _registry


def load_all() -> Registry:
    """Walk backend/skills/*/ and register every skill found. Sets the
    module-level singleton. Idempotent — safe to call repeatedly
    (e.g. after a skill is hot-reloaded)."""
    global _registry
    reg = Registry()
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        manifest = skill_dir / "skill.md"
        module_file = skill_dir / "skill.py"
        if not manifest.exists() or not module_file.exists():
            continue
        try:
            skill = _load_one(skill_dir, manifest, module_file)
            reg.register(skill)
            log.info("registered skill: %s (%s)", skill.name, skill_dir.name)
        except Exception as e:
            log.exception("failed to load skill %s: %s", skill_dir.name, e)
    _registry = reg
    log.info("skills loaded: %d total", len(reg.all()))
    return reg


def _load_one(skill_dir: Path, manifest_path: Path, module_path: Path) -> Skill:
    text = manifest_path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillError(f"{manifest_path.name} has no YAML frontmatter")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"invalid YAML in {manifest_path}: {e}")
    body = (m.group(2) or "").strip()

    name = fm.get("name") or skill_dir.name
    if not fm.get("description"):
        raise SkillError(f"{manifest_path}: missing required 'description'")

    # Dynamic import — give the module a unique name so two skills can't
    # collide on Python's module cache.
    mod_name = f"yorik_skill_{skill_dir.name}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    if spec is None or spec.loader is None:
        raise SkillError(f"could not load {module_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    entrypoint = getattr(mod, "execute", None)
    if entrypoint is None or not callable(entrypoint):
        raise SkillError(f"{module_path}: must define a callable `execute`")

    return Skill(
        name=name,
        description=fm["description"],
        when_to_use=fm.get("when_to_use", ""),
        when_not_to_use=fm.get("when_not_to_use", "") or "",
        inputs=fm.get("inputs", {}) or {},
        outputs=fm.get("outputs", {}) or {},
        cost=fm.get("cost", ""),
        permissions=fm.get("permissions") or ["admin"],
        side_effects=fm.get("side_effects", ""),
        tags=fm.get("tags") or [],
        category=fm.get("category", "") or "",
        body=body,
        entrypoint=entrypoint,
        manifest_path=manifest_path,
    )
