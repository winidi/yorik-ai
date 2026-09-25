# Audit: Wer sieht was über den Chat (2026-09-25)

Anlass: Der Chat zeigt Dirk Aufgaben anderer Leute. Frage von Dirk: alle
Verbindungen des Chats prüfen, dort soll genauso klar geregelt sein, wer
was sieht, wie in Kalender, Aufgaben usw.

Methode: drei unabhängige Code-Durchgänge (Kalender/Kontakte/Rechnungen/
Planung; Mail/Dokumente/Fotos/Schreiben; alles, was der Chat außerhalb der
Skills einbindet), dazu die Aufgaben-Skills selbst. Die schwersten Punkte
an den zitierten Zeilen nachgeprüft (markiert mit ✔). Nur gelesen, nichts
geändert, Live-Datenbank nicht gelesen. Zeilennummern gelten für den Stand
nach Commit `6328347`.

Maßstab sind die Regeln aus dem Audit vom 22.09. (Regel 1: keine
Admin-Ausnahme beim Sehen; Regel 3: Skills handeln als die Person, die
fragt; Regel 4: Eltern führen die Aufgaben der Kinder), dazu eine neue
Frage: **Meins oder sichtbar?** Wer etwas sehen *darf*, will im Chat bei
„meine Aufgaben“ trotzdem nur seine eigenen sehen, und bei allem anderen
wissen, wessen es ist.

## Ergebnis in einem Satz

Die Aufgaben der anderen sind kein Leck, sondern drei Lücken
hintereinander: Der Chat weiß nicht, wer fragt, `check_tasks` liefert
standardmäßig alles Sichtbare statt der eigenen Liste, und die Karten
sagen nicht, wem eine Aufgabe gehört. Daneben hat die Prüfung rund zehn
echte Lecks gefunden, vor allem bei Dokumenten, Kontakten in
Schreiben-Skills und einem Connector, der das Admin-Fotokonto nutzt.

## Warum der Chat fremde Aufgaben zeigt

| # | Stelle | Was passiert |
| --- | --- | --- |
| A1 ✔ | `backend/ask.py:1571` | Im getippten Chat wird der System-Prompt mit `user=None` gebaut. Das Modell liest dann (`ask.py:959-962`): „there's no logged-in user context — don't assume who 'me' refers to“. Es weiß nicht, wer „ich“ ist. Nur Stimme und Regenerate geben einen Namen mit. |
| A2 ✔ | `backend/skills/check_tasks/skill.py:21, 85-99`, `skill.md` | `mine_only` steht standardmäßig auf `false`, dann gilt `spaces.row_filter`: alles im Haushalts-Space, die Aufgaben der Kinder, alles, was einem zugewiesen ist. Die `skill.md` schickt „what tasks do I have today“ ohne `mine_only` los. Nur Briefings setzen es. |
| A3 ✔ | `check_tasks/skill.py:100-101`, `tasks_found`-Karte | Die Zeilen tragen nur die alte Freitext-Spalte `person`, keine Zugewiesenen, keinen Ersteller. Weder Karte noch Modell können sagen, wessen Aufgabe das ist. |
| A4 | `main.py:9551-9559` `/api/today` (Startkarten im Chat) | „N überfällige Aufgaben“ zählt ebenfalls alle sichtbaren. |
| A5 | `day_plans.py:315-323` (`plan_my_day`) | „Meine“ heißt dort: selbst erstellt **oder** zugewiesen. Eine Aufgabe, die Dirk für Beate oder ein Kind angelegt hat, landet in Dirks Tagesplan. |
| A6 | `check_tasks` Parameter `person` | Filtert auf die Freitext-Spalte `person LIKE '%Anna%'`, nicht auf `task_assignees`. „Was muss Anna noch erledigen“ findet Zuweisungen nicht. |

Die Aufgaben-App macht es anders: Sie zeigt standardmäßig nur die eigene
Liste (`TasksApp.tsx:99-105`: zugewiesen an mich, oder ohne Zuweisung und
von mir angelegt), den Rest erst mit dem Schalter „Others“, und jede Zeile
nennt die Zugewiesenen.

Dasselbe Muster gilt für den Kalender: `check_calendar`,
`propose_meeting_times`, `plan_my_day` und der Kalender-Kontext in Mail-
und WhatsApp-Entwürfen nehmen alle sichtbaren Termine (Familie, geteilte
Kalender) als „meine“, ohne Besitzer oder Kalendername. Folge: „passt mir
Dienstag?“ ist belegt, weil Beate da einen Termin hat, und ein Entwurf
schreibt „ich habe da schon was“.

## Lecks: der Chat zeigt, was die App verweigert

Schwere wie im Audit vom 22.09.: **K** = jedes Haushaltskonto (auch
Kinder) sieht fremde persönliche Daten. **H** = Admin sieht fremde Daten
oder sie landen beim Falschen. **M** = Kontext-Leck, schmaler Fall.

| # | S | Stelle | Befund | Richtig wäre |
| --- | --- | --- | --- | --- |
| L1 | K | ✔ `skills/read_document/skill.py:40-51`, `read_document_vision/skill.py:165-170` | Hochgeladene Dokumente: keine Prüfung. Jedes Konto bekommt über die Nummer den vollen Text jedes Uploads. Die App-Route (`main.py:13015`) prüft wenigstens die Rolle. | Besitzer/Freigabe prüfen wie Paperless-Pfad |
| L2 | K | ✔ `ask.py:608`, `connectors/__init__.py:125-134`, `connectors/immich.py:84-87` | `trigger_connector` ist für alle Rollen freigegeben. Nur Paperless bekommt die Zugangsdaten der Person; Immich fällt auf den globalen Admin-Schlüssel zurück → Dateinamen, Daten und IDs aus Dirks Fotobibliothek für jedes Konto. Mail-IMAP und Banking genauso, falls installiert (live nicht geprüft). `/api/connectors/{name}/invoke` (`main.py:7418`) hat dieselbe Lücke. | Connector nur mit Zugangsdaten der Person, sonst Fehler; oder aus dem Chat nehmen |
| L3 | K | ✔ `skills/compose_check_template_args/skill.py:240-251` | Liest `compose_drafts` per ID ohne Besitzer: Namen, Adressen, Beträge aus fremden Entwürfen werden in die eigenen Felder gemischt. IDs sind fortlaufend. | `AND user_id = ?` |
| L4 | K | ✔ `skills/find_known_provider/skill.py:70-75, 139-143` | Kontakte ohne Rolle/Person (`contacts._visibility_clause` filtert dann gar nicht) und Termine ohne Sichtbarkeitsfilter: alle Kontakte mit Telefon, Mail, Adresse und alle Termin-Titel/Orte des Haushalts, private eingeschlossen. | `C.search(role, user_id)`, `visible_event_filter` |
| L5 | K | `compose_check_recipient:137, 161-175`, `compose_draft:463`, `find_recipient_address_from_documents:316`, `whatsapp_draft:74` | `contacts.get(id)` ohne Person: Name, Anschrift, WhatsApp-Nummer jedes Kontakts landen im Entwurf. `write_letter`/`write_invoice` machen es richtig (`recipient.resolve(role, user_id)`). | `C.get(id, role, user_id)` |
| L6 | H | `find_recipient_address_from_documents:116-163, 339, 451` | Adress-Cache pro Kontakt statt pro Person: Beates Scan ihrer privaten Dokumente liefert Dirk Adresse, 200 Zeichen Auszug und Dokument-ID. | Cache pro Person |
| L7 | H | ✔ `skills/check_calendar/skill.py` (kein `downgrade_for_privacy`), `find_event_by_title:45`, `day_plans.py:306-313` | Private Termine anderer auf geteilten Kalendern kommen mit echtem Titel; die App zeigt „Busy“. `find_event_by_title` öffnet bei fehlender Person alles. | downgrade wie `main.py:3387`; fail closed |
| L8 | H | `compose_draft:173-189` | Fotos in Briefen: ohne eigenes Immich-Konto Rückfall auf den Admin-Schlüssel (Audit 1.19, Paket 12 hat nur die Proxys repariert). | kein Rückfall |
| L9 | H | `main.py:9394-9407` `/api/chat/mentions` (Dokument-Zweig), `search_documents` (`documents.py:378, 569`) | Dokumente nach Rolle statt Person (Audit 2.12, noch offen): Admin sieht alle Titel, Mitglieder alles mit `member`. | wie `/api/documents` mit Personen-Token |
| L10 | H | `main.py:7356-7372` `/api/web/visits`, `main.py:9996-10013` `/api/compose/saved-drafts`, `delete_compose_draft:40-45` | Admin-Ausnahmen: Websuchen aller Personen, Schreiben-Entwürfe aller. | nur Besitzer |
| L11 | M | `block_travel_time:432-446, 336-342` | Wer ein fremdes Ereignis schreiben darf, sieht beim Puffer den nächsten Termin und Konflikte des Besitzers aus dessen privaten Kalendern. | Sichtbarkeitsfilter |
| L12 | M | `add_contact:82-103`, `add_contact_channel:37-44` | Bei gleicher Mail/Nummer wird ein unsichtbarer fremder Kontakt ergänzt und zurückgegeben bzw. benannt. | Treffer nur unter sichtbaren |
| L13 | M | `set_document_visibility:44-62` | Absage nennt den Titel eines fremden Dokuments. | schlichtes 403 |
| L14 | M | `main.py:7332-7340` `/api/saved-queries` | Alte zwischengespeicherte Fragen und Antworten aller Personen, ohne Filter (wird nicht mehr beschrieben; Zeilenzahl live nicht geprüft). | Zeilen löschen, Route weg |

## Der Chat darf schreiben, was die App verweigert

| # | Stelle | Befund |
| --- | --- | --- |
| W1 | `add_calendar_event:80-152` | Ein übergebenes `calendar_id` wird nicht geprüft: Termine in fremde persönliche Kalender oder schreibgeschützte Google-Spiegel. Die App antwortet 403 (`main.py:3650`). |
| W2 | `day_plans.py:146-172` (`plan_day` → `apply_plan`) | Aufgaben-ID vom Modell wird ohne `can_write_row` umgeschrieben (Titel, Fälligkeit); `:462-483` schreibt in jeden Aufnahme-Bericht. |
| W3 | `update_/delete_calendar_event` | Schreibgeschützte Spiegel-Termine lassen sich im Chat ändern und löschen. |
| W4 | `delete_calendar_event:63-67`, `block_travel_time:106-112` | Verknüpfte Puffer werden per `notes LIKE '[LINKED_TO=n]'` ohne Besitzer gesucht und gelöscht/verschoben. |
| W5 | `/api/ask` mit fremder `conversation_id` (`agent/conversation_io.py:283-302`) | Überschreibt das Entity-Ledger (landet im nächsten Prompt der anderen Person). Lesen bleibt gesperrt. |

## Notizen

- `ask.py:958-970`: wegen `user=None` wird die Skill-Liste im Prompt nicht nach Rolle gefiltert; Kinder sehen Admin-Skills gelistet (Ausführen verweigert `registry.invoke`).
- `universal_search`, `find_user`, `find_photo`: fehlende Rolle/Person wird zu `admin` bzw. Admin-Schlüssel (Muster D, heute nicht erreichbar).
- `compose_check_template_args:285, 296`: übergibt `role` statt `user_id`, liefert immer `[]` (Funktionsfehler, kein Leck).
- Die fünf Rechnungs-Skills (`_check_bills` usw.) haben gar keinen Filter, sind aber nicht geladen (`registry.py:579` überspringt `_`-Ordner). Vor dem Wiedereinschalten reparieren.

## Was korrekt ist

- Identität: `/api/ask` und `/api/ask/stream` nehmen Rolle und Person aus der Sitzung, nicht aus dem Body. Stimme, MCP und `SkillContext` ohne Nutzer-1-Default.
- Unterhaltungen, Chat-Anhänge: alles über `owns()` / `owner_user_id`, keine Admin-Ausnahme.
- Kein verstecktes Kontext-Leck: Der System-Prompt enthält nur Datum, Sprache, Identität und Skill-Liste; kein RAG, keine Haushaltsliste, keine Mails.
- Mail- und WhatsApp-Skills, `file_attachment`, `read_attachment`, `write_letter`, `write_invoice`, Aufnahme-Skills, `notify`, `undo_last_action`, `ask_agent`: sauber.
- Aufgaben schreiben (`update_task`, `delete_task`): dieselbe Regel wie die App (`spaces.can_write_row`, Eltern für Kinder).
- `/api/today`: Termine gefiltert und privat abgeblendet; nur die Besitzer-Angabe fehlt (A4).

## Reparatur in Paketen

1. **Wer fragt** (A1): `build_system_prompt(user=user)` im getippten Chat. Klein, und Voraussetzung für alles „meins“.
2. **Meins zuerst** (A2–A6, Kalender): `check_tasks` standardmäßig `mine_only`, andere nur auf Nachfrage („die der Familie“, „was muss Anna …“) mit Filter auf `task_assignees`. Karten und Rückgaben tragen Zugewiesene/Besitzer bzw. Kalendername, damit das Modell „Beates Termin“ sagen kann. `plan_my_day` wie die Aufgaben-App. Termine anderer bei „passt mir?“ nur, wenn gewollt.
3. **K-Lecks** (L1–L5): Uploads nach Besitzer, `trigger_connector` nur mit Personen-Zugangsdaten, Entwurf per `user_id`, `find_known_provider` und alle `contacts.get` mit Person.
4. **H-Lecks** (L6–L10): Adress-Cache pro Person, Privat-Abblendung in allen Kalender-Skills, kein Admin-Immich-Rückfall, Dokumente nach Person (2.12), Admin-Ausnahmen streichen.
5. **Schreiben** (W1–W5): `can_write_row` bzw. Kalender-Schreibrecht und `read_only` in allen Termin-Skills und `apply_plan`, Ledger-Schreiben mit `user_id`.
6. **Rest** (L11–L14, Notizen).

## Entscheidungen für Dirk

- **Was heißt „meine Aufgaben“?** Vorschlag: wie die Aufgaben-App, also zugewiesen an mich, oder ohne Zuweisung und von mir angelegt. Die Aufgaben, die du für ein Kind angelegt hast, gehören dann dem Kind; sie kommen bei „was müssen die Kinder noch machen“.
- **Familientermine bei „passt mir?“**: Vorschlag: nur deine eigenen Kalender und Termine, zu denen du eingeladen bist; der Familienkalender zählt, Beates persönlicher nicht.
- **`trigger_connector` im Chat**: ganz herausnehmen (die Skills decken Fotos, Dokumente, Mail ab) oder nur mit Personen-Zugangsdaten?
