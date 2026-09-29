"""Messages for people, in their language. English is the source; every
other language is a column next to it. A key or a language that is
missing falls back to English, never to the key.

    tr("pipelines.not_found", user=user)            # the signed-in person
    tr("pipelines.attention.handover", user_id=uid) # someone the work is for
    tr("pipelines.sent_to", "de", to="a@b.de")      # a known language

Text for the model (prompts, skill hints) does not belong here.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

log = logging.getLogger("yorik.messages")

MESSAGES: dict[str, dict[str, str]] = {
    # ── Pipelines: what needs the person ─────────────────────────────
    "pipelines.attention.maybe": {"en": "Is this the answer?", "de": "Ist das die Antwort?"},
    "pipelines.attention.cannot_check": {"en": "Yorik can't check reliably right now",
                                         "de": "Yorik kann gerade nicht sicher prüfen"},
    "pipelines.attention.step_due": {"en": "No answer yet. Send the reminder?",
                                     "de": "Keine Antwort — Erinnerung senden?"},
    "pipelines.attention.handover": {"en": "No answer after all reminders. Over to you",
                                     "de": "Keine Antwort nach allen Erinnerungen — jetzt du"},
    "pipelines.attention.self_replied": {"en": "You wrote to them yourself. Keep following up?",
                                         "de": "Du hast selbst geschrieben — weiter verfolgen?"},
    "pipelines.attention.send_unclear": {"en": "Not sure whether the reminder went out",
                                         "de": "Unklar, ob die Erinnerung rausging"},
    "pipelines.attention.other": {"en": "A pipeline needs you", "de": "Pipeline braucht dich"},
    # ── Pipelines: history lines ─────────────────────────────────────
    "pipelines.check.maybe": {"en": "Possible match ({{n}})", "de": "Möglicher Treffer ({{n}})"},
    "pipelines.check.cannot_check": {"en": "Can't check reliably", "de": "Kann nicht sicher prüfen"},
    "pipelines.check.surely_not": {"en": "No answer yet", "de": "Noch keine Antwort"},
    "pipelines.event.last_step": {"en": "Last step reached, no answer. Handed over to you",
                                  "de": "Letzter Schritt erreicht, keine Antwort — Übergabe an dich"},
    "pipelines.event.sent_to": {"en": "Reminder sent to {{to}}", "de": "Erinnerung gesendet an {{to}}"},
    "pipelines.event.sent_confirmed": {"en": "Reminder had been sent (confirmed after a restart)",
                                       "de": "Erinnerung war gesendet (nach Neustart bestätigt)"},
    "pipelines.event.check_failed": {"en": "Check failed: {{error}}", "de": "Fehler bei der Prüfung: {{error}}"},
    "pipelines.event.drafted": {"en": "Yorik wrote the reminders and suggested the intervals",
                                "de": "Yorik hat die Erinnerungen geschrieben und die Abstände vorgeschlagen"},
    "pipelines.event.started": {"en": "Started (accompanied: every reminder asks before it is sent)",
                                "de": "Gestartet (begleitet: jede Erinnerung fragt vor dem Senden)"},
    "pipelines.worker.checked": {"en": "{{n}} checked", "de": "{{n}} geprüft"},
    "pipelines.goal": {"en": "Answer to “{{subject}}”", "de": "Antwort auf „{{subject}}“"},
    "pipelines.no_subject": {"en": "(no subject)", "de": "(ohne Betreff)"},
    "pipelines.label": {"en": "Follow up until answered", "de": "Antwort verfolgen"},
    # ── Pipelines: why something can't happen now ────────────────────
    "pipelines.not_now.not_running": {"en": "The pipeline isn't running.", "de": "Die Pipeline läuft nicht."},
    "pipelines.not_now.not_next": {"en": "That isn't the next step.", "de": "Das ist nicht der nächste Schritt."},
    "pipelines.not_now.sends_nothing": {"en": "This step doesn't send anything.", "de": "Dieser Schritt sendet nichts."},
    "pipelines.not_now.not_approved": {"en": "This step isn't approved.", "de": "Dieser Schritt ist nicht freigegeben."},
    "pipelines.not_now.maybe_answer": {"en": "There is a mail that could be the answer. Please look at it first.",
                                       "de": "Es gibt eine Mail, die die Antwort sein könnte. Bitte erst ansehen."},
    "pipelines.not_now.cannot_check": {"en": "Yorik can't check reliably right now.",
                                       "de": "Yorik kann gerade nicht sicher prüfen."},
    "pipelines.not_now.self_wrote": {"en": "You have written to them yourself in the meantime.",
                                     "de": "Du hast inzwischen selbst geschrieben."},
    "pipelines.not_now.text_changed": {"en": "The text has changed in the meantime. Please read it again.",
                                       "de": "Der Text hat sich inzwischen geändert. Bitte noch einmal lesen."},
    # ── Pipelines: request errors ────────────────────────────────────
    "pipelines.not_found": {"en": "Pipeline not found", "de": "Pipeline nicht gefunden"},
    "pipelines.off_for_you": {"en": "Pipelines are switched off for you.", "de": "Pipelines sind für dich ausgeschaltet."},
    "pipelines.busy": {"en": "Yorik is checking this pipeline right now. Please try again in a moment.",
                       "de": "Yorik prüft diese Pipeline gerade. Bitte gleich noch einmal."},
    "pipelines.person_not_found": {"en": "Person not found", "de": "Person nicht gefunden"},
    "pipelines.admin_only": {"en": "Only an admin can change this for an admin",
                             "de": "Für Admins kann das nur ein Admin ändern"},
    "pipelines.mail_not_found": {"en": "Mail not found", "de": "Mail nicht gefunden"},
    "pipelines.only_sent": {"en": "Only a sent mail can be followed up.", "de": "Nur eine gesendete Mail lässt sich verfolgen."},
    "pipelines.no_recipient": {"en": "The mail has no recipient.", "de": "Die Mail hat keinen Empfänger."},
    "pipelines.bad_window": {"en": "Invalid sending window", "de": "Sendefenster ungültig"},
    "pipelines.unknown_step": {"en": "Unknown step: {{action}}", "de": "Unbekannter Schritt: {{action}}"},
    "pipelines.reminder_needs_recipient": {"en": "A reminder needs a recipient.",
                                           "de": "Eine Erinnerung braucht einen Empfänger."},
    "pipelines.step_not_found": {"en": "Step not found or already done", "de": "Schritt nicht gefunden oder schon erledigt"},
    "pipelines.not_running": {"en": "Not running", "de": "Läuft nicht"},
    "pipelines.nothing_to_confirm": {"en": "Nothing to confirm", "de": "Nichts zu bestätigen"},
    "pipelines.nothing_to_clarify": {"en": "Nothing to clarify", "de": "Nichts zu klären"},
    # ── Pipelines: the default reminders (mail to the other side) ────
    "pipelines.mail.greeting": {"en": "Hello,", "de": "Guten Tag,"},
    "pipelines.mail.signoff": {"en": "Kind regards", "de": "Freundliche Grüße"},
    "pipelines.mail.first": {
        "en": "I'm following up on my message of {{when}}. I haven't received a reply so far. "
              "Could you briefly confirm that it arrived and let me know how things will proceed?",
        "de": "ich komme zurück auf meine Nachricht vom {{when}}. Bisher habe ich keine Antwort erhalten. "
              "Könnten Sie mir bitte kurz bestätigen, dass sie angekommen ist, und mir sagen, "
              "wie es weitergeht?"},
    "pipelines.mail.second": {
        "en": "I still haven't received a reply to my message of {{when}} and my reminder. "
              "Please get back to me within the next seven days.",
        "de": "auf meine Nachricht vom {{when}} und meine Erinnerung habe ich leider noch keine Antwort. "
              "Bitte melden Sie sich innerhalb der nächsten sieben Tage."},
    "pipelines.event.rewritten": {"en": "Reminder {{n}} rewritten for today. Please read and approve it",
                                  "de": "Erinnerung {{n}} für heute neu geschrieben — bitte lesen und freigeben"},
    # ── Pipelines: mail freshness and why a mail may be the answer ──
    "pipelines.fresh.fetch_error": {"en": "fetching reports an error: {{error}}", "de": "Abruf meldet einen Fehler: {{error}}"},
    "pipelines.fresh.never": {"en": "never fetched", "de": "noch nie abgerufen"},
    "pipelines.fresh.minutes": {"en": "last fetched {{n}} minutes ago", "de": "zuletzt vor {{n}} Minuten abgerufen"},
    "pipelines.fresh.syncing": {"en": "still syncing with the server", "de": "Abgleich mit dem Server läuft noch"},
    "pipelines.fresh.unreadable": {"en": "{{n}} mail(s) could not be read", "de": "{{n}} Mail(s) konnten nicht gelesen werden"},
    "pipelines.fresh.no_account": {"en": "no active mail account", "de": "kein aktives Mailkonto"},
    "pipelines.why.thread": {"en": "reply in the same thread", "de": "Antwort im selben Verlauf"},
    "pipelines.why.address": {"en": "from the address you wrote to", "de": "von der angeschriebenen Adresse"},
    "pipelines.why.domain": {"en": "from the domain {{d}}", "de": "von der Domain {{d}}"},
    "pipelines.why.domain_name": {"en": "sender domain {{d}} matches the name", "de": "Absender-Domain {{d}} passt zum Namen"},
    "pipelines.why.number": {"en": "mentions {{n}}", "de": "nennt {{n}}"},
    "pipelines.why.word": {"en": "contains “{{w}}”", "de": "enthält „{{w}}“"},
    # ── Write ──
    "write.einvoice_missing": {"en": "The e-invoice can't be made here yet: the \"ZUGFeRD\" extension isn't installed (Settings → Extensions).",
                               "de": "Die E-Rechnung kann hier noch nicht erzeugt werden: die Erweiterung „ZUGFeRD“ ist nicht installiert (Settings → Extensions)."},
    "write.einvoice_failed": {"en": "The e-invoice did not pass the check; no number was used.",
                              "de": "Die E-Rechnung hat die Prüfung nicht bestanden; es wurde keine Nummer vergeben."},
    # ── Pipelines: more ──
    "pipelines.parents_only": {"en": "Only parents or admins", "de": "Nur Eltern oder Admins"},
    "pipelines.event.llm_unreachable": {"en": "The AI model wasn't reachable. The template stays", "de": "Das Sprachmodell war nicht erreichbar — Vorlage bleibt stehen"},
    "pipelines.event.created": {"en": "Draft created from the sent mail", "de": "Entwurf angelegt aus der gesendeten Mail"},
    "pipelines.finished": {"en": "The pipeline has ended.", "de": "Die Pipeline ist beendet."},
    "pipelines.already_drafting": {"en": "Yorik is already writing.", "de": "Yorik schreibt schon."},
    "pipelines.bad_send_days": {"en": "send_days: all or weekdays", "de": "send_days: alle oder werktags"},
    "pipelines.drafting_retry": {"en": "Yorik is writing the reminders right now. Try again in a moment.", "de": "Yorik schreibt die Erinnerungen gerade. Gleich noch einmal."},
    "pipelines.bad_days": {"en": "Days: 0 to 365", "de": "Tage: 0 bis 365"},
    "pipelines.drafting": {"en": "Yorik is writing the reminders right now.", "de": "Yorik schreibt die Erinnerungen gerade."},
    "pipelines.already_started": {"en": "Already started", "de": "Schon gestartet"},
    "pipelines.approve_all_first": {"en": "Please approve every mail first.", "de": "Bitte erst jede Mail freigeben."},
    "pipelines.event.paused": {"en": "Paused", "de": "Pausiert"},
    "pipelines.not_paused": {"en": "Not paused", "de": "Nicht pausiert"},
    "pipelines.event.resumed": {"en": "Resumed", "de": "Fortgesetzt"},
    "pipelines.already_ended": {"en": "Already ended", "de": "Schon beendet"},
    "pipelines.event.marked_done": {"en": "Marked as done", "de": "Als erledigt markiert"},
    "pipelines.event.cancelled": {"en": "Cancelled", "de": "Abgebrochen"},
    "pipelines.end_first": {"en": "End or cancel it first", "de": "Erst beenden oder abbrechen"},
    "pipelines.event.answer_confirmed": {"en": "Answer confirmed. Done", "de": "Antwort bestätigt — erledigt"},
    "pipelines.event.not_the_answer": {"en": "Not the answer. Waiting on", "de": "Keine Antwort — weiter warten"},
    "pipelines.event.own_mail_seen": {"en": "Own mail seen. Following up on", "de": "Eigene Mail gesehen — weiter verfolgen"},
    "pipelines.event.was_sent": {"en": "Reminder had been sent (confirmed)", "de": "Erinnerung war gesendet (bestätigt)"},
    "pipelines.event.was_not_sent": {"en": "Reminder had not been sent. The step stays open", "de": "Erinnerung war nicht gesendet — Schritt bleibt offen"},
    "pipelines.event.drafting_failed": {"en": "Writing failed: {{error}}", "de": "Schreiben fehlgeschlagen: {{error}}"},
}


def default_language() -> str:
    return (os.getenv("HOMEOS_DEFAULT_LANGUAGE") or "en").split("-")[0].lower()


def language_of(user_id: Any) -> str:
    """A person's language from their profile; the install's default when unknown."""
    if user_id is not None:
        try:
            from .database import get_conn
            with get_conn() as c:
                row = c.execute("SELECT language FROM user_profiles WHERE id = ?", (str(user_id),)).fetchone()
            if row and row["language"]:
                return str(row["language"]).split("-")[0].lower()
        except Exception as exc:  # noqa: BLE001 — a message never fails on its language
            log.debug("language_of(%s): %s", user_id, exc)
    return default_language()


_GERMAN = re.compile(r"\b(und|nicht|ich|Sie|Ihre?[mnrs]?|bitte|mit|für|Grüße|Guten|Sehr geehrte|vielen Dank|Rechnung|Kündigung)\b")
_ENGLISH = re.compile(r"\b(the|and|not|you|your|please|with|for|regards|dear|thank|invoice)\b", re.I)


def text_language(text: str, fallback: str) -> str:
    """German or English, judged from a mail's own words; `fallback` when unclear."""
    de, en = len(_GERMAN.findall(text or "")), len(_ENGLISH.findall(text or ""))
    if de >= 2 and de > en:
        return "de"
    if en >= 2 and en > de:
        return "en"
    return fallback


def tr(key: str, lang: Optional[str] = None, *, user: Optional[dict] = None,
       user_id: Any = None, **params: Any) -> str:
    if lang is None:
        if user is not None:
            lang = (user.get("language") or "") or language_of(user.get("id"))
        else:
            lang = language_of(user_id)
    lang = (lang or "en").split("-")[0].lower()
    entry = MESSAGES.get(key)
    if entry is None:
        log.warning("messages: unknown key %s", key)
        return key
    text = entry.get(lang) or entry["en"]
    for k, v in params.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text
