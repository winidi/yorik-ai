"""Dashboard digest endpoint — feeds the briefing card on home.

Pure-SQL aggregation: today + tomorrow events, bills due in the next 7
days, priority open tasks, and unread emails grouped by category. No
LLM, no skill dispatch — sub-100ms per call so it can be polled every
few minutes without cost.

Why a dedicated endpoint vs. just calling the four /api/{events,bills,
tasks,email/messages} routes from the frontend: bundling cuts four
round-trips to one and lets us compose the natural-language `summary`
line server-side where the data lives, in the user's language. The
full app screens still exist for deep dives.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends

from .auth_sessions import current_user
from .database import get_conn
from . import workers as _workers


# 60s in-process cache for the "photos uploaded today" Immich query.
# The briefing card polls /digest every 5 minutes, and several active
# tabs would each round-trip to Immich on every poll. One cache covers
# all of them.
_PHOTO_CACHE: dict[str, Any] = {"ts": 0.0, "items": []}
_PHOTO_CACHE_TTL = 60.0


def _recent_uploads_cached() -> list[dict]:
    """Wrapper around immich._recent_uploads with a 60s cache. Returns
    [] silently if Immich is unreachable (digest must not block)."""
    now = _time.monotonic()
    if now - _PHOTO_CACHE["ts"] < _PHOTO_CACHE_TTL:
        return _PHOTO_CACHE["items"]
    try:
        from .connectors import immich as _immich
        items = _immich._recent_uploads(hours=24, take_count=6, exclude_whatsapp=True)
    except Exception:  # noqa: BLE001
        items = []
    _PHOTO_CACHE["ts"] = now
    _PHOTO_CACHE["items"] = items
    return items

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/workers")
def workers(user: dict = Depends(current_user)) -> dict:
    """Liveness snapshot of every long-running background worker. The
    home screen polls this to render a colored row — green per worker
    that heartbeat recently, amber for stale, red for crashed.
    Auth-required because worker names hint at infra (which connectors
    are wired up)."""
    return {"workers": _workers.get_all()}


@router.get("/digest")
def digest(user: dict = Depends(current_user)) -> dict[str, Any]:
    """Return everything the home-screen briefing card needs.

    Localized to the user's chosen language (profile.language, falls
    back to 'en'). Carries today's events + tomorrow's events + the
    week's bills + priority tasks + unread email counts. The
    `title` / `greeting` / `summary` strings are pre-rendered so the
    same wording appears anywhere this gets surfaced (frontend card,
    future voice-driven morning briefing, etc.).
    """
    lang = _normalize_lang(user.get("language"))
    today = date.today()
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=7)

    today_start = datetime.combine(today, time.min).isoformat()
    today_end   = datetime.combine(today, time.max).isoformat()
    tomorrow_start = datetime.combine(tomorrow, time.min).isoformat()
    tomorrow_end   = datetime.combine(tomorrow, time.max).isoformat()

    with get_conn() as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT id, title, starts_at, ends_at, color, person "
            "FROM events WHERE starts_at >= ? AND starts_at <= ? "
            "ORDER BY starts_at ASC LIMIT 10",
            (today_start, today_end),
        ).fetchall()]

        tomorrow_events = [dict(r) for r in conn.execute(
            "SELECT id, title, starts_at, ends_at, color, person "
            "FROM events WHERE starts_at >= ? AND starts_at <= ? "
            "ORDER BY starts_at ASC LIMIT 10",
            (tomorrow_start, tomorrow_end),
        ).fetchall()]

        bills_week = [dict(r) for r in conn.execute(
            "SELECT id, name, amount, currency, due_date, recurring, notes, "
            "email_message_id, document_id FROM bills WHERE paid = 0 AND due_date >= ? "
            "AND due_date <= ? ORDER BY due_date ASC LIMIT 10",
            (today.isoformat(), week_end.isoformat()),
        ).fetchall()]

        # Priority tasks: open + due in next 7 days OR no due date.
        # Avoid surfacing the user's entire backlog — only what's
        # genuinely actionable this week.
        priority_tasks = [dict(r) for r in conn.execute(
            "SELECT id, title, due_date, person "
            "FROM tasks WHERE done = 0 AND ("
            "       due_date IS NULL OR (due_date >= ? AND due_date <= ?)"
            ") ORDER BY due_date IS NULL, due_date ASC LIMIT 8",
            (today.isoformat(), week_end.isoformat()),
        ).fetchall()]

        # Tomorrow tasks: open + due tomorrow specifically. Separate
        # from priority_tasks (which is week-window) so the "Tomorrow"
        # section can show what's actually scheduled for tomorrow
        # without dragging the rest of the week in.
        tomorrow_tasks = [dict(r) for r in conn.execute(
            "SELECT id, title, due_date, person "
            "FROM tasks WHERE done = 0 AND due_date = ? "
            "ORDER BY id ASC LIMIT 6",
            (tomorrow.isoformat(),),
        ).fetchall()]

        # Unread emails grouped by category. We only count messages
        # actually addressed to this user (owner_user_id) — so a shared
        # box doesn't bleed into someone else's digest.
        unread_rows = conn.execute(
            "SELECT COALESCE(category, 'other') AS cat, COUNT(*) AS n "
            "FROM email_messages WHERE is_unread = 1 AND is_sent = 0 "
            "AND owner_user_id = ? GROUP BY cat",
            (user["id"],),
        ).fetchall()
        unread = {r["cat"]: r["n"] for r in unread_rows}

        name = (user.get("name") or "").split(" ", 1)[0] or _STR[lang]["fallback_name"]

    summary = _make_summary(events, bills_week, priority_tasks, unread, lang)
    daypart = _daypart()
    photos_today = _recent_uploads_cached()

    return {
        "language":       lang,
        "title":          _STR[lang]["title"][daypart],
        "greeting":       f"{_STR[lang]['greeting'][daypart]}, {name}",
        "summary":        summary,
        "today_events":   events,
        "tomorrow_events": tomorrow_events,
        "tomorrow_tasks":  tomorrow_tasks,
        "bills_due_week": bills_week,
        "priority_tasks": priority_tasks,
        "photos_today":   photos_today,
        "unread_by_category": unread,
        "unread_total":   sum(unread.values()),
        # Section labels rendered by the frontend — kept on the server so
        # adding a new language doesn't require a frontend rebuild.
        "labels": _STR[lang]["labels"],
    }


# ───────────────────────── language helpers ──────────────────────────

_SUPPORTED = {"en", "de", "fr", "es", "it"}


def _normalize_lang(lang: str | None) -> str:
    if not lang:
        return "en"
    short = lang.lower().split("-")[0].strip()
    return short if short in _SUPPORTED else "en"


def _daypart() -> str:
    """Which part of the day for greeting/title selection."""
    h = datetime.now().hour
    if h < 5:  return "night"
    if h < 12: return "morning"
    if h < 18: return "afternoon"
    return "evening"


# Static strings, one block per supported language. Adding a language
# is one entry here — no frontend change needed because the card reads
# `title` + `labels` straight off the digest payload.
_STR: dict[str, dict[str, Any]] = {
    "en": {
        "fallback_name": "there",
        "title": {
            "night":     "Your night briefing",
            "morning":   "Your morning briefing",
            "afternoon": "Your afternoon briefing",
            "evening":   "Your evening briefing",
        },
        "greeting": {
            "night":     "Late night",
            "morning":   "Good morning",
            "afternoon": "Good afternoon",
            "evening":   "Good evening",
        },
        "labels": {
            "today":              "Today",
            "tomorrow":           "Tomorrow",
            "bills_this_week":    "Bills this week",
            "tasks":              "Tasks",
            "unread":             "unread",
            "all_day":            "All day",
            "no_due_date":        "no due date",
            "inbox_clear":        "Inbox is clear, no events today.",
            "and_bill":           "{n} bill due this week",
            "and_bills":          "{n} bills due this week",
            "priority_task":      "{n} priority task",
            "priority_tasks":     "{n} priority tasks",
            "bill_email":         "{n} bill email",
            "bill_emails":        "{n} bill emails",
            "appt_email":         "{n} appointment email",
            "appt_emails":        "{n} appointment emails",
            "unread_suffix":      "unread",
            "photos_today":       "Photos today",
        },
    },
    "de": {
        "fallback_name": "Du",
        "title": {
            "night":     "Deine Nacht-Übersicht",
            "morning":   "Dein Morgen-Briefing",
            "afternoon": "Dein Nachmittags-Briefing",
            "evening":   "Dein Abend-Briefing",
        },
        "greeting": {
            "night":     "Späte Stunde",
            "morning":   "Guten Morgen",
            "afternoon": "Guten Tag",
            "evening":   "Guten Abend",
        },
        "labels": {
            "today":              "Heute",
            "tomorrow":           "Morgen",
            "bills_this_week":    "Rechnungen diese Woche",
            "tasks":              "Aufgaben",
            "unread":             "ungelesen",
            "all_day":            "Ganztägig",
            "no_due_date":        "ohne Fälligkeit",
            "inbox_clear":        "Posteingang leer, heute keine Termine.",
            "and_bill":           "{n} Rechnung fällig diese Woche",
            "and_bills":          "{n} Rechnungen fällig diese Woche",
            "priority_task":      "{n} wichtige Aufgabe",
            "priority_tasks":     "{n} wichtige Aufgaben",
            "bill_email":         "{n} Rechnungs-E-Mail",
            "bill_emails":        "{n} Rechnungs-E-Mails",
            "appt_email":         "{n} Termin-E-Mail",
            "appt_emails":        "{n} Termin-E-Mails",
            "unread_suffix":      "ungelesen",
            "photos_today":       "Heute hochgeladen",
        },
    },
    "fr": {
        "fallback_name": "vous",
        "title": {
            "night":     "Votre récap de nuit",
            "morning":   "Votre récap du matin",
            "afternoon": "Votre récap de l'après-midi",
            "evening":   "Votre récap du soir",
        },
        "greeting": {
            "night":     "Tard dans la nuit",
            "morning":   "Bonjour",
            "afternoon": "Bon après-midi",
            "evening":   "Bonsoir",
        },
        "labels": {
            "today":              "Aujourd'hui",
            "tomorrow":           "Demain",
            "bills_this_week":    "Factures cette semaine",
            "tasks":              "Tâches",
            "unread":             "non lus",
            "all_day":            "Toute la journée",
            "no_due_date":        "sans échéance",
            "inbox_clear":        "Boîte vide, aucun événement aujourd'hui.",
            "and_bill":           "{n} facture à payer cette semaine",
            "and_bills":          "{n} factures à payer cette semaine",
            "priority_task":      "{n} tâche prioritaire",
            "priority_tasks":     "{n} tâches prioritaires",
            "bill_email":         "{n} e-mail de facture",
            "bill_emails":        "{n} e-mails de factures",
            "appt_email":         "{n} e-mail de rendez-vous",
            "appt_emails":        "{n} e-mails de rendez-vous",
            "unread_suffix":      "non lus",
            "photos_today":       "Photos d'aujourd'hui",
        },
    },
    "es": {
        "fallback_name": "tú",
        "title": {
            "night":     "Tu resumen de noche",
            "morning":   "Tu resumen matinal",
            "afternoon": "Tu resumen de tarde",
            "evening":   "Tu resumen vespertino",
        },
        "greeting": {
            "night":     "Buenas noches",
            "morning":   "Buenos días",
            "afternoon": "Buenas tardes",
            "evening":   "Buenas noches",
        },
        "labels": {
            "today":              "Hoy",
            "tomorrow":           "Mañana",
            "bills_this_week":    "Facturas esta semana",
            "tasks":              "Tareas",
            "unread":             "sin leer",
            "all_day":            "Todo el día",
            "no_due_date":        "sin fecha",
            "inbox_clear":        "Bandeja vacía, ningún evento hoy.",
            "and_bill":           "{n} factura esta semana",
            "and_bills":          "{n} facturas esta semana",
            "priority_task":      "{n} tarea prioritaria",
            "priority_tasks":     "{n} tareas prioritarias",
            "bill_email":         "{n} correo de factura",
            "bill_emails":        "{n} correos de factura",
            "appt_email":         "{n} correo de cita",
            "appt_emails":        "{n} correos de citas",
            "unread_suffix":      "sin leer",
            "photos_today":       "Fotos de hoy",
        },
    },
    "it": {
        "fallback_name": "ciao",
        "title": {
            "night":     "Il tuo riepilogo notturno",
            "morning":   "Il tuo riepilogo mattutino",
            "afternoon": "Il tuo riepilogo pomeridiano",
            "evening":   "Il tuo riepilogo serale",
        },
        "greeting": {
            "night":     "Notte fonda",
            "morning":   "Buongiorno",
            "afternoon": "Buon pomeriggio",
            "evening":   "Buonasera",
        },
        "labels": {
            "today":              "Oggi",
            "tomorrow":           "Domani",
            "bills_this_week":    "Bollette di questa settimana",
            "tasks":              "Attività",
            "unread":             "non lette",
            "all_day":            "Tutto il giorno",
            "no_due_date":        "senza scadenza",
            "inbox_clear":        "Posta vuota, nessun evento oggi.",
            "and_bill":           "{n} bolletta in scadenza questa settimana",
            "and_bills":          "{n} bollette in scadenza questa settimana",
            "priority_task":      "{n} attività prioritaria",
            "priority_tasks":     "{n} attività prioritarie",
            "bill_email":         "{n} e-mail di fattura",
            "bill_emails":        "{n} e-mail di fatture",
            "appt_email":         "{n} e-mail di appuntamento",
            "appt_emails":        "{n} e-mail di appuntamenti",
            "unread_suffix":      "non lette",
            "photos_today":       "Foto di oggi",
        },
    },
}


def _make_summary(events: list, bills: list, tasks: list, unread: dict, lang: str) -> str:
    """One-liner combining the most-pressing items in the user's language.
    Order chosen to surface time-bound things first (events today > bills
    due > tasks > unread)."""
    L = _STR[lang]["labels"]
    parts: list[str] = []

    if events:
        next_ev = events[0]
        when = (next_ev.get("starts_at", "") or "")[11:16]  # HH:MM
        title = (next_ev.get("title") or "").strip() or {
            "en": "an event", "de": "ein Termin", "fr": "un événement",
            "es": "un evento", "it": "un evento",
        }[lang]
        # "at HH:MM" / "um HH:MM" / "à HH:MM" / "a las HH:MM" / "alle HH:MM"
        AT = {"en": "at", "de": "um", "fr": "à", "es": "a las", "it": "alle"}[lang]
        parts.append(f"{title} {AT} {when}" if when else title)

    if bills:
        n = len(bills)
        parts.append((L["and_bills"] if n != 1 else L["and_bill"]).format(n=n))

    if tasks:
        n = len(tasks)
        parts.append((L["priority_tasks"] if n != 1 else L["priority_task"]).format(n=n))

    bill_unread = unread.get("bill", 0)
    appt_unread = unread.get("appointment", 0)
    if bill_unread or appt_unread:
        bits = []
        if bill_unread:
            bits.append((L["bill_emails"] if bill_unread != 1 else L["bill_email"]).format(n=bill_unread))
        if appt_unread:
            bits.append((L["appt_emails"] if appt_unread != 1 else L["appt_email"]).format(n=appt_unread))
        parts.append(", ".join(bits) + " " + L["unread_suffix"])

    if not parts:
        return L["inbox_clear"]
    return ". ".join(parts) + "."
