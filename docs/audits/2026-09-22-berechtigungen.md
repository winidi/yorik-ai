# Audit: Berechtigungen und Besitz (2026-09-22)

Anlass: Beates Mail-Anhang landete beim Paperless-Admin, Dirk sieht die
Worker aller Mailkonten, Immich-Uploads schienen im falschen Konto zu
liegen. Frage von Dirk: einmal komplett prüfen, wem was gehört und wer
was sieht.

Methode: vier unabhängige Code-Durchgänge (Paperless/Immich; Kerntabellen;
Mail/WhatsApp/Suche/Worker; Skills/Agent/Kiosk/externer Zugriff), danach
Stichproben der schwersten Punkte an den zitierten Zeilen. Nur gelesen,
nichts geändert. Zeilennummern gelten für den Stand nach Commit `c737dcc`.

Die Regeln, gegen die geprüft wurde (aus `docs/HANDOFF.md`):

1. Keine Admin-Ausnahme beim Sehen. Persönliches sieht der Besitzer und
   wer es geteilt bekommen hat. Admin-Rechte sind für Einstellungen,
   Nutzer, Backups.
2. Jede Person hat ihr eigenes Paperless- und Immich-Konto. Der
   Admin-Token ist nur für Yoriks Hausarbeit (Gruppen, Tags, Workflows).
3. Skills handeln als die Person, die fragt (`ctx.user_id`).
4. Eltern führen die Aufgaben der Kinder.

## Ergebnis in einem Satz

Die REST-Kernpfade (Aufgaben, Kalender, Kontakte-CRUD, Aufnahmen,
Mail, Chat-Anhänge, Schreiben, Benachrichtigungen, Universalsuche) sind
sauber nach Person gefiltert. Drumherum gibt es rund 60 Stellen, die
das nicht sind. Sie folgen fünf Mustern, und die Reparatur ist
mechanisch, wenn man Muster für Muster vorgeht.

## Die fünf Muster

| Muster | Beispiel | Wirkung |
| --- | --- | --- |
| **A. Filter nach Rolle statt Person** | Chat-Konversationen: `user_role = ?` | Beate und jedes andere `member` können sich gegenseitig lesen; Admins alle |
| **B. Admin-Token / Admin-Superuser** | Paperless-Suche, WhatsApp-Medien, Dirk = Paperless-Superuser | Dokumente gehören dem Admin oder Admin sieht alles |
| **C. Route ohne Besitzer-Prüfung** | `/api/today`, `/api/chat/mentions`, Kontakt-Kanäle, WhatsApp discard/reprocess | Jedes Konto (auch Kinder) sieht/ändert Haushaltsdaten |
| **D. Standard auf Nutzer 1 / Rolle admin** | `user_id or 1`, `role="admin"` in Hintergrundjobs | Fehlende Identität wird still zum Admin |
| **E. Kontext-Leck in KI-Entwürfe** | WhatsApp-Semantik, Paperless-Hinweise, Kalender-Kontext | Fremde Nachrichten/Dokumente landen in Beates Entwürfen |

## Befunde

Schwere: **K** = jedes Haushaltskonto sieht oder ändert fremde
persönliche Daten (auch Kinder). **H** = Admin sieht/ändert fremde Daten
oder Daten landen beim falschen Besitzer. **M** = Kontext-Leck, Kiosk,
unsauberer Fallback. **N** = Notiz (Fail-closed-Bug, toter Code,
irreführender Kommentar).

### 1. Paperless und Immich

| # | S | Stelle | Befund | Richtig wäre |
| --- | --- | --- | --- | --- |
| 1.1 | H | `backend/external_users.py:523-541`, `:473-481` | Yorik-Admins werden Paperless-**Superuser**, beim Re-Provisionieren wird das Flag wieder gesetzt. Dirk und Test Butler sehen jedes private Dokument. Live geprüft: Dirks Token sieht alle 4 Dokumente | Niemand wird Superuser; Hausarbeit läuft über den separaten Admin-Token |
| 1.2 | H | `backend/external_users.py:716-736` | Yorik-Admins werden Immich-**Admins** (können jedem das Passwort setzen und sich als ihn anmelden) | Kein Yorik-Rolle wird Immich-Admin |
| 1.3 | H | `backend/paperless_proxy.py:108` | `platform_admin` läuft in der Paperless-Oberfläche als Paperless-`admin` | Eigenes Konto wie alle anderen (für `admin` ist das schon so) |
| 1.4 | K | `backend/main.py:12171` `GET /api/paperless/search` | Semantische Suche über **alle** Dokumente, ohne Token, ohne Space-Filter; liefert Textausschnitte | Token der Person und `visible_space_ids` |
| 1.5 | K | `backend/main.py:13042-13062` `POST /api/documents/search` | Suchfeld der Dokumente-App: `search_hybrid` ohne Token/Spaces → FTS läuft mit Admin-Token | dito |
| 1.6 | K | `backend/paperless_ingest.py:587-609` | `search()` gibt Chunk-Text auch für Dokumente zurück, die der Token nicht sehen darf (nur der Titel wird zu „Document #N“) | Zeilen ohne Treffer in `docs_meta` verwerfen |
| 1.7 | K | `backend/paperless_ingest.py:211-218` | `space_id` in `paperless_chunks` wird **nie** geschrieben → der Space-Filter ist wirkungslos (`OR space_id IS NULL`) bzw. wirft bei FTS alles weg | `space_id` beim Ingest setzen, Escape-Klausel entfernen |
| 1.8 | K | `backend/skills/read_document/skill.py`, `read_document_vision/skill.py` | Kein `ctx.user_id` in der Datei; jede Doc-ID wird gelesen bzw. mit Admin-Token heruntergeladen. Auch über MCP erreichbar | Sichtbarkeit über Token der Person prüfen (404 sonst) |
| 1.9 | K | `backend/skills/search_documents/skill.py:56`, `:73` | „Zeig meine letzten Dokumente“ → `recent_with_count` mit Admin-Token; Suche ohne `creds_override` | Token der Person |
| 1.10 | K | `backend/connectors/paperless.py:86-183` + `find_recipient_address_from_documents/skill.py` | Der Paperless-Connector läuft komplett mit Admin-Token, ist als LLM-Tool und über `/api/connectors/{name}/invoke` erreichbar | `creds_override` wie beim Immich-Connector |
| 1.11 | H | `backend/whatsapp_media.py:272-315`, `:320-369` | Eingehende WhatsApp-PDFs → Paperless und Fotos → Immich mit **Admin**-Credentials; Besitzer wird der Admin, die Empfängerin sieht nichts | `get_user_paperless_creds/immich_creds(owner_user_id)` |
| 1.12 | H | `backend/compose/save.py:41-61` | Compose-Ablage immer mit Admin-Token (nur Admin-Routen, daher begrenzt) | `_push_to_paperless(user_id=…)` wie Schreiben |
| 1.13 | H | `backend/dashboard_routes.py:36-49` | `photos_today` auf der Startseite kommt aus dem Admin-Immich-Konto und aus einem Cache für alle | Key der Person, Cache pro Person |
| 1.14 | H | `backend/main.py:12581-12591` | `_push_to_paperless` fällt auf Admin-Token zurück, wenn die Person kein Konto hat → Dokument gehört dem Admin | Fehler zurückgeben statt still ablegen |
| 1.15 | H | `backend/main.py:12235`, `:12657`, `:12792` | Dokumente-Liste/Facetten/Einzelabruf: Admin ohne eigenes Konto → Admin-Token („legacy admin-sees-all“) | Fallback streichen |
| 1.16 | H | `backend/paperless_provisioning.py:283-289` | `set_document_space` (shared) setzt `owner: None`; der Startup-Backfill vergibt das Dokument dann an den Admin | Besitzer behalten, nur Gruppen setzen |
| 1.17 | N | `backend/main.py:12349-12356`, `set_document_visibility/skill.py:38-43` | Beide lesen `creds["paperless_user_id"]`, das die Funktion nie liefert → **Besitzer kann die Sichtbarkeit seines eigenen Dokuments nie ändern**, nur Admins | `paperless_user_id` aus `user_profiles` lesen |
| 1.18 | H | `set_document_visibility/skill.py:36` | Admin darf jedes Dokument umstellen (auch privat → Familie) | Nur Besitzer |
| 1.19 | M | `backend/main.py:4544`, `:4705`, `:4760`, `compose_draft/skill.py:167` | Foto-Proxys fallen auf den Admin-Immich-Key zurück; `find_photo` macht es richtig (fail closed) | Fail closed |
| 1.20 | N | `backend/paperless_proxy.py:110` vs `external_users.py:431` | Zwei verschiedene Username-Ableitungen; Paperless legt bei Abweichung still ein leeres Konto an (`RemoteUserBackend`) | Eine gemeinsame Funktion |
| 1.21 | N | `frontend-react/src/apps/photos/PhotosApp.tsx` | Die Fotos-App ist ein iframe auf Immich mit **eigenem Login**. Yorik meldet nicht an. Dirks Uploads liegen korrekt auf `dirk@winiecki.ai`; wer im Browser als anderes Immich-Konto eingeloggt ist, sieht eine andere Bibliothek | Entweder SSO (Immich OAuth) oder wenigstens die Anzeige „angemeldet als …“ |

### 2. Kerntabellen

| # | S | Stelle | Befund | Richtig wäre |
| --- | --- | --- | --- | --- |
| 2.1 | K | `backend/spaces.py:79` | Bereichs-Freigabe (z. B. nur „Kalender“) wirkt in jeder Tabelle **ohne** Bereich (`bills`, `recordings`, alle `area=None`-Aufrufer) als volle Freigabe | `area is None` = nur ungescopte Mitgliedschaft (wie `_scoped_level`) |
| 2.2 | K | `backend/main.py:8924`, `:9010`, `:9045`, `:9266` | Konversation lesen/löschen/anpinnen/neu ausführen nach **Rolle**: `member` liest `member`, Admins lesen alle. Root: `agent/conversation_io.py:74-82` | `user_id` prüfen, kein Admin-Zweig |
| 2.3 | K | `backend/main.py:9079-9131` | Konversations-Stash (Anhänge) gleich gelagert | dito |
| 2.4 | K | `backend/main.py:8868` | Legacy-`conversations` nach Rolle gelistet; `agent_conversations` mit `user_id IS NULL` ebenso | Rollen-Fallback streichen |
| 2.5 | K | `backend/main.py:9324` `GET /api/today` | Termine, überfällige Aufgaben (mit Titeln), Geburtstage des ganzen Haushalts, ohne Filter | `visible_event_filter`, `row_filter` |
| 2.6 | K | `backend/main.py:9147` `GET /api/chat/mentions` | @-Vorschläge: alle Kontakte, alle Termine, Dokumente nach Rolle | Filter der Person |
| 2.7 | K | `backend/main.py:1250` `GET /api/ambient/board`, `:1381` timetable | Gate `_kiosk_or_session` = jede Session. Liefert Titel, Ort und **Notizen** aller zustimmenden Personen, ohne `downgrade_for_privacy` | Session-Zweig durch die Sicht der Person filtern; Notizen nicht auf die Wand |
| 2.8 | K | `backend/briefing_snapshots.py:45`, `:141-149`, `briefing_routes.py:122`, `:159` | Tages-Snapshots werden **als Admin** erzeugt, nur nach `(template, date)` gespeichert und jedem ausgeliefert (`period=yesterday`). Jeder kann den globalen Snapshot mit seinem eigenen überschreiben | Snapshot pro Person |
| 2.9 | K | `backend/main.py:6141` `/contacts/{id}/timeline`, `:5703` `/proposals`, `:6383` `by-channel`, `:5534` `_counts` | Kontakt ohne Sichtbarkeitsprüfung, Termine-Leg ohne ACL, Enrichment-Snippets (aus fremden WhatsApp-Chats) für alle | `require_contact_access` wie `patch_contact` |
| 2.10 | K | `backend/main.py:6053`, `:6073`, `:6313`, `:6324`, `:6346`, `:6355`, `:6374` | Kontakt promote/pin/spam, Kanäle und Adressen anlegen/löschen: `user` wird angenommen und ignoriert | dito |
| 2.11 | K | `backend/main.py:4818` `PATCH /api/bills/{id}` | Kein Besitzer-Check | `_ensure_row_writable` |
| 2.12 | K | `backend/documents.py:370`, `:519`, `main.py:12831`, `:12852` | Lokaler Dokument-Spiegel filtert nach **Rolle** (`allowed_roles`), Admin sieht alles, `/raw` ohne `user` | `owner_user_id` (Spalte existiert) + `can_view_row` |
| 2.13 | H | `backend/main.py:12871`, `:12879` | Dokument löschen/reindex nur Admin, Besitzer darf nicht | Besitzer + Admin-Ausnahme raus |
| 2.14 | H | `backend/main.py:3628` `_ensure_row_writable` | `platform_admin` (und `user is None`) umgehen `can_write_row` | Beide Zweige streichen |
| 2.15 | H | `backend/calendars.py:194` `require_row_owner_or_admin` | Chat-Skills (update/delete task, event, share_contact, block_travel_time): `admin` schreibt jede Zeile; Eltern-Regel und Freigaben werden ignoriert | `spaces.can_write_row` |
| 2.16 | H | `backend/calendars.py:744-807` `freebusy` | Fragt die gelöschte Tabelle `calendar_shares` ab; übrig bleibt ein Admin-Zweig, der dem Admin alle Busy-Blöcke gibt | `effective_access >= free_busy` |
| 2.17 | K | `backend/main.py:6973` attendees, `:6762` calendar shares | Wer auf welchem Termin ist / mit wem ein Kalender geteilt ist, für jede ID | ACL |
| 2.18 | K | `backend/people_routes.py:230` `_events_by_names` | nimmt `user_id`, benutzt ihn nicht | Filter |
| 2.19 | N | `backend/main.py:13203` `GET /api/settings/{key}`, `:13229` voice-profiles | Ohne Rolle lesbar (PUT ist Admin) | Admin |
| 2.20 | N | `backend/main.py:4254` `list_tasks` vs `spaces.py:263` | Eltern-Regel fehlt im `row_filter` (Eltern sehen Kinder-Aufgaben in der Liste nicht, können sie aber ändern) | `_is_guardian` in `row_filter` |
| 2.21 | N | `backend/spaces.py:150-174`, `:322` | Admins bekommen Level `admin` auf jedem geteilten (nicht-persönlichen) Space; Docstring behauptet einen globalen Bypass, den es nicht gibt | Mitgliedschaft als einziger Weg; Kommentar korrigieren |

### 3. Mail, WhatsApp, Suche, Worker

| # | S | Stelle | Befund | Richtig wäre |
| --- | --- | --- | --- | --- |
| 3.1 | K | `backend/main.py:6936`, `dashboard_routes.py:54`, `email_fetcher.py:182-193`, `WorkersStatus.tsx:117` | **Dirks Beobachtung.** Alle Worker an alle; `email_account_<id>` trägt die **Mailadresse** und rohe IMAP-Fehler in der Detailzeile, auf der Startseite sichtbar | Konto-Worker nur dem Besitzer; Haushalts-Worker ohne Detail |
| 3.2 | K | `backend/whatsapp.py:2066` `POST /drafts/{jid}/discard` | Kein `current_user`, kein Besitzer: jeder verwirft die Entwürfe aller; Broadcast an alle Tabs | wie die Nachbarrouten |
| 3.3 | K | `backend/whatsapp.py:1142` + `whatsapp_media.py:425-479` `reprocess` | Ohne Auth/Besitzer: fremde Sprachnotiz-Transkripte lesen, Routing-Status löschen, fremde Medien mit `force=True` ins Admin-Paperless/Immich zwingen | Besitzer prüfen, Credentials des Besitzers |
| 3.4 | M | `backend/whatsapp_semantic.py` (kein `owner`), `whatsapp.py:1651` `_semantic_hints` | Semantischer WhatsApp-Index ist haushaltsweit; 140 Zeichen fremder Nachrichten landen in Entwürfen (`sources_json`) und werden angezeigt. Im Code bewusst so kommentiert | `owner_user_id` in `wa_chunks` |
| 3.5 | M | `whatsapp.py:1673` `_paperless_hints`, `:1697` `_calendar_context`, `email_draft/skill.py` | Entwurfs-Kontext aus allen Dokumenten (Admin-Token) und allen Terminen (auch privaten) | Token der Person, `visible_event_filter` |
| 3.6 | M | `suggestions/retrievers/email_history.py:38`, `calendar.py:41`, `tasks.py:32` | Retriever ignorieren `ctx.owner_user_id`; Evidenz aus fremden Mails/Terminen/Aufgaben wird gespeichert und angezeigt | `owner_user_id = ?` |
| 3.7 | K | `contact_enricher.py:151-157`, `:295` | Enricher liest WhatsApp-Verläufe **aller** Nutzer; Snippets über die ungeschützte Proposals-Route (2.9) für alle lesbar. Mail-Zweig wirft wegen falscher Spaltennamen (`from_addr`) still einen Fehler; wer den fixt, öffnet ein Mail-Leck | Quellen pro Person scopen |
| 3.8 | M | `whatsapp.py:895` `POST /import`, `:1519` `/draft` | Import schreibt nach `DEFAULT_OWNER = 1` (bricht heute wegen UUID); Draft-Route fällt bei Bearer-Auth auf `user_id=1, role=admin` | `current_user`, keine Defaults |
| 3.9 | M | `whatsapp.py:866-892`, `:1124-1140`, `:617` | Settings, Status-Import-Schalter (haushaltsweit), Backfill über alle, `docker inspect`: jedes Konto | `require_admin` |
| 3.10 | K | `backend/n8n_proxy.py:84`, `:147` | n8n-Editor für jedes Konto (auch `restricted`), inklusive aller gespeicherten Credentials; außerhalb `/api/` | Admin-Gate |
| 3.11 | N | `email_autodraft.py:96`, `suggestions/engine.py:188` | Hintergrund-Entwürfe laufen mit `role="admin"` | echte Rolle des Besitzers |
| 3.12 | N | `email_classifier.py:306` | `int(owner_user_id)` auf UUID → Rechnungs-/Termin-Benachrichtigung wird nie erzeugt (fail closed) | `str()` |
| 3.13 | N | `whatsapp.py:699`, `:724` | Bridge-Start prüft `== "admin"`, sperrt `platform_admin` aus | `normalize_role` |
| 3.14 | N | `database_pg.py:87`, `migrations_pg/102_phase_e_rls_policies.sql` | Verbindung als `postgres`, kein `FORCE ROW LEVEL SECURITY` → die RLS-Policies greifen nie; Python-Filter sind die einzige Schranke | Bewusst so lassen oder RLS scharf stellen |

### 4. Skills, Agent, Stimme, Kiosk, extern

| # | S | Stelle | Befund | Richtig wäre |
| --- | --- | --- | --- | --- |
| 4.1 | K | `backend/voice_login_tokens.py:48-58`, `:89-123`, `main.py:1120-1185` | Voice-Login: Secret ist eine **Konstante** (`HOMEOS_VOICE_LOGIN_SECRET` nirgends gesetzt), `verify()` prüft die Session-Bindung `s` nicht. Im LAN mit bekannter Wall-Device-UUID lässt sich ein Token für jede `profile_id` bauen → volle Session als diese Person | Secret beim ersten Start erzeugen und speichern; `s` prüfen |
| 4.2 | K | `backend/main.py:13427`, `:13436` `/api/ask-voice` | Sprecher-Erkennung liefert `profile_id`, Code liest `.get("id")` → **Rolle des Sprechers auf Daten des Cookie-Nutzers**; erkannter Admin am Kinder-Tablet bekommt Admin-Rechte. Streaming-Pfad (`:13730`) macht es richtig | `profile_id` oder wie im Stream-Pfad nur am Kiosk + PIN-Picker |
| 4.3 | M | `backend/main.py:13380-13407` | Nicht-Streaming-Voice erkennt auf **jedem** Gerät und fällt bei Unbekannt still auf den Cookie-Nutzer | wie Stream-Pfad |
| 4.4 | M | `backend/main.py:1564-1581`, `ambient.py:266-304` | Kiosk-Slideshow `show_today`: **alle** Nutzer, `kiosk_agenda_consent` ignoriert; unveröffentlichte Fotos auf der Wand | Consent prüfen (eigenes Foto-Bit) |
| 4.5 | M | `backend/main.py:4693-4711` | Thumbnail-Proxy: `?u=<uid>` für Kiosk-Aufrufer liest jedes Asset jedes Nutzers; Admin-Key-Fallback | Asset-IDs an die Slideshow binden; kein Fallback |
| 4.6 | H | `backend/skills/check_calendar/skill.py:132` | `platform_admin` überspringt `visible_event_filter` (Ausnahme, die `calendars.py` am 09-09 entfernt hat); kein `downgrade_for_privacy` | Zweig streichen |
| 4.7 | M | `whatsapp_draft/skill.py:64`, `universal_search/skill.py:10`, `email_briefing/skill.py:11`, `find_email_by_subject/skill.py:30`, `compose_draft/skill.py:430ff`, `pending_actions.py:592`, `ask.py:1563` | `user_id or 1`: fehlende Identität wird still zu Nutzer 1 (Admin), entgegen `registry.py:133-137` | `raise` wie `notify`, `read_email` |
| 4.8 | N | `search_documents/skill.py:39-46`, `check_tasks`, `find_task_by_title`, `list_subtasks` | Bei `user_id is None` fällt der Filter weg (haushaltsweit) | fail closed (`[]`) |
| 4.9 | N | `find_photo/skill.py:488-514` | Personen-Cache mit `id(dict)` als Key → Kollision zwischen Nutzern innerhalb 60 s | Key `user_id` |
| 4.10 | N | `main.py:2081`, `whatsapp.py:1532` | `/api/skills/{name}/invoke`: `current_user_optional` mit Default `admin`/`1` (heute durch Middleware unerreichbar) | `Depends(current_user)` |
| 4.11 | N | `backend/main.py:1273` | Termine auf geteilten Kalendern erscheinen auf der Wand auch ohne Consent des Besitzers (vertretbar, aber nicht der „eine Schalter“ aus dem Docstring) | dokumentieren |

## Was korrekt ist

Als Referenz für die Reparatur, denn hier steht das Muster schon:

- **Mail** (`email_routes.py`, `email_actions.py`, `email_fetcher.py`): alle ~40 Routen nach `owner_user_id`, Anhänge über `fetch_attachment_binary(user_id)`, keine Admin-Ausnahme.
- **Universalsuche** (`search_routes.py`): jede Quelle mit eigener Sichtbarkeitsklausel in Keyword- und Semantik-Zweig; Paperless/Immich mit Token der Person. Die Command-Palette erbt das.
- **Aufgaben, Kalender, Kontakte-CRUD, Aufnahmen, Chat-Anhänge, Schreiben, Briefköpfe, Freigaben, Spaces**: `row_filter` / `can_view_row` / `can_write_row` / `owner_user_id = ?`, keine Admin-Ausnahme.
- **Benachrichtigungen, Push, Vorschläge (Routen und Engine)**: pro Person.
- **MCP** (`mcp_server.py`, `api_tokens.py`): ein Token = eine Person, `SkillContext` mit deren Rolle. Die Lecks kommen aus den Skills (1.8, 1.9, 1.10, 4.6), nicht aus MCP.
- **`notify`, `ask_agent`, `find_photo`, `read_email`, `email_briefing`, `file_attachment`, `read_attachment`, Aufnahme-Skills**: scopen oder werfen bei fehlendem `user_id`.
- **`spaces.user_visible_space_ids`**: die eine Stelle, die Regel 1 wörtlich umsetzt (fremder persönlicher Space unsichtbar für `admin` und `platform_admin`).
- **Backup**: nur Admin, keine Download-Route. Bewusste Admin-Ausnahme.

## Stand

- **Erledigt 22.09.:** Paket 1 (2.2–2.4, Commit `da47b65`), Paket 2 (2.1, 2.21
  Docstring), Paket 3 (2.5, 2.6, 2.7 Board, 3.1 Worker; Commit `968588a`),
  Paket 4 (2.9, 2.10, 2.11, 2.17, 2.18, 3.2, 3.3, 3.9, 3.10: Kontakt-Routen
  über `_contact_for`, Rechnungen über `_ensure_row_writable`, Teilnehmer
  und Kalender-Freigaben nur für Sichtbare, WhatsApp discard/reprocess auf
  den Besitzer, WhatsApp-Settings/Backfill/Bridge-Info und n8n nur Admin).
  Timetable (2.7) bleibt consent-basiert: der Stundenplan eines Kindes, das
  auf der Wand ist, ist für den Haushalt. 2.19 (`GET /api/settings/{key}`)
  offen, weil das Frontend Haushalts-Einstellungen für alle liest.
- **Entschieden von Dirk 22.09.:** Notfall-Zugriff wie vorgeschlagen (ja);
  Consume-Ordner-Dokumente sichtbar für `parents`; Immich-Login = Yorik-Login
  (OIDC-Provider in Yorik, eigenes Paket nach den Berechtigungs-Paketen).

## Reparatur in Paketen

Reihenfolge nach Blast-Radius, jedes Paket einzeln testbar und
deploybar:

1. **Konversationen nach Person** (2.2–2.4): fünf Handler und
   `conversation_io` von `user_role` auf `user_id`. Kleinster Eingriff,
   größte Wirkung.
2. **`spaces.py:79`** (2.1): eine Zeile.
3. **Ungefilterte Feeds** (2.5, 2.6, 2.7, 3.1): `today`, `mentions`,
   `board`/`timetable` im Session-Zweig, Worker-Liste. Danach sieht kein
   Kind mehr die Woche der Eltern und Dirk nicht mehr Beates Worker.
4. **Ungeschützte Routen** (2.9–2.11, 2.17, 2.18, 3.2, 3.3, 3.9, 3.10):
   mechanisch die Gate-Funktion der Nachbarroute einsetzen; n8n auf Admin.
5. **Voice** (4.1, 4.2, 4.3): Secret erzeugen, `s` prüfen,
   `profile_id`, Erkennung nur am Kiosk.
6. **Paperless-Suchpfade** (1.4–1.10): `space_id` beim Ingest schreiben,
   `creds_override` in Connector und Skills, un-hydrierte Zeilen
   verwerfen. Danach 1.17 (Besitzer darf Sichtbarkeit ändern).
7. **WhatsApp-Medien und Compose** (1.11, 1.12, 1.14): Credentials des
   Besitzers, kein Admin-Fallback.
8. **Briefing-Snapshots pro Person** (2.8): Schema-Änderung.
9. **Entwurfs-Kontext** (3.4–3.7): `owner_user_id` in `wa_chunks`,
   Retriever und Hints scopen.
10. **Admin-Ausnahmen** (1.1–1.3, 1.15, 1.16, 1.18, 2.13–2.16, 4.6):
    Superuser/Immich-Admin zurücknehmen, Proxy auf eigenes Konto,
    `require_row_owner_or_admin` → `can_write_row`, `_ensure_row_writable`
    ohne `platform_admin`. **Voraussetzung:** der Notfall-Zugriff aus dem
    nächsten Abschnitt, sonst verliert Dirk den Weg zu den
    Consume-Ordner-Dokumenten.
11. **Defaults auf Nutzer 1 / Rolle admin** (3.8, 3.11, 4.7, 4.8, 4.10):
    `raise` statt Fallback.
12. **Kiosk-Fotos** (4.4, 4.5) und die Notizen (1.19–1.21, 2.19–2.21,
    3.12–3.14, 4.9, 4.11).

## Entscheidungen für Dirk

1. **Notfall-Zugriff.** Wunsch: Dirk und Beate können im Ernstfall alles
   sehen, aber nicht „mal eben“, und die anderen erfahren davon.
   Vorschlag: ein ausdrücklicher Modus „Zugriff auf alles“ in
   Einstellungen, nur für Erwachsene, mit Begründungspflicht, zeitlich
   begrenzt (z. B. 24 h), beim Einschalten eine Benachrichtigung an alle
   Erwachsenen und ein Eintrag im Verlauf. Technisch: für die Dauer
   werden alle Filter mit `emergency_until > now()` auf der eigenen
   Person umgangen, Paperless/Immich über temporäre Gruppen-Rechte statt
   Superuser. Solange das nicht existiert, bleibt 1.1 bewusst offen.
2. **Consume-Ordner-Dokumente** gehören dem Paperless-`admin`, den
   niemand im Haushalt ist. Vorschlag: der Workflow gibt sie der Gruppe
   `parents` zur Ansicht (oder `household`), Besitzer bleibt `admin`.
3. **Immich-Login.** Bleibt die Fotos-App ein iframe mit eigenem
   Immich-Login, oder soll Yorik anmelden (Immich kann OAuth; Yorik
   müsste OIDC-Provider werden oder Immich-Passwörter weiter
   synchronisieren)? Kurzfristig: in der Fotos-App anzeigen, als wer man
   in Immich angemeldet ist.
4. **RLS** (3.14): Postgres-Policies scharf stellen als zweites Netz, oder
   entfernen, damit niemand glaubt, sie würden schützen.
