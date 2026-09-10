# Plan: Abendessen-Aufnahme als Yorik-Add-on

Stand 2026-09-10. Noch nichts gebaut. Dieser Plan ist die Arbeitsgrundlage
und wird beim Bau fortgeschrieben; die Anleitung "Dein erstes Add-on"
entsteht aus dem Protokoll der Umsetzung (Phase 4).

## 1. Ziel

Ein Gerät am Tisch (Tablet im Kiosk, ersatzweise Handy) nimmt das
Abendessen auf. Auf "Fertig" wertet Yorik die Aufnahme lokal aus und
liefert allen Teilnehmern einen Bericht: Beschlüsse und Aufgaben mit
Wer, schöne Momente, was nicht gut lief, offene Fragen, erwähnte
Termine. Aufgaben werden als Vorschläge angeboten; übernommene Aufgaben
fließen in die Tagesplanung. Kein Ton und kein Text verlässt den
Yorik-Rechner.

Nebenziel: der Bau zeigt, wie man Yorik erweitert. Der generische Teil
(Aufnahmen verarbeiten) wandert in den Kern, der Abendessen-Teil ist
eine App nach dem bestehenden App-Vertrag (`docs/BUILD_AN_APP.md`).

## 2. Ausgangslage im Code

Vorhanden und wiederverwendbar:

| Baustein | Wo | Stand |
|---|---|---|
| Browser-Aufnahme, Wake-Lock im Kiosk | `frontend-react/src/components/VoiceFab.tsx`, `apps/ambient/AmbientApp.tsx` | funktioniert, fünf Kopien der MediaRecorder-Logik |
| Transkription CPU | `backend/stt_parakeet.py`, `backend/voice.py` | liefert nur `{text, language}`, keine Zeitstempel |
| Stimmprofil pro Nutzer | `backend/voice_id.py` (WeSpeaker CAM++), `user_profiles.voice_embedding` | Einzelclip-Erkennung, keine Sprecherwechsel |
| Aufgaben mit Undo | `backend/skills/add_task/` | Aufgabe startet im privaten Space des Erstellers |
| Freigaben pro Zeile | `backend/spaces.py` (`row_shares`, `TABLE_AREA`, `row_filter`) | Tabelle muss in `TABLE_AREA` und im Owner-Mapping eingetragen werden |
| Bell + Web-Push | `backend/notifications.py` (`create` pusht automatisch), `backend/push.py` | fertig |
| Tagesplanung | `backend/day_plans.py`, Skills `plan_my_day`, `plan_day`, `day_review` | Aufgabenabfrage filtert nur `created_by_user_id` |
| Hintergrund-Muster | `backend/briefing_snapshots.py` (`start_scheduler`), `backend/workers.py` (Heartbeat) | kein Job-Modell, nur Scheduler pro Modul |
| App-Vertrag | `docs/BUILD_AN_APP.md`, `backend/app_loader.py`, `backend/app_schema_lifecycle.py`, `examples/*-v2/` | drei Beispiel-Apps, noch keine echte App durchlaufen |

Fehlt komplett: Sprecherwechsel-Erkennung, Transkript mit Zeitstempeln,
dauerhafte Audio-Ablage, ein Job, der Minuten läuft und Status meldet.

Technische Grundlage für das Fehlende: sherpa-onnx 1.13.7 im venv bringt
`OfflineSpeakerDiarization` (pyannote-Segmentierung als ONNX plus
Sprecher-Embedding) und Silero-VAD mit. Das vorhandene
CAM++-Modell in `data/speaker_model/` dient als Embedding-Modell. Kein
Torch, keine GPU.

## 3. Ablauf aus Nutzersicht

1. Kiosk zeigt in der Tagesansicht die Kachel "Abendessen aufnehmen".
   Antippen: Teilnehmer wählen (Vorauswahl alle Mitglieder, Kinder
   abwählbar), Hinweis "Alle am Tisch wissen, dass aufgenommen wird",
   Start.
2. Während der Aufnahme: roter Punkt mit Dauer, Pause, Stopp. Alle zwei
   Minuten wird ein Stück hochgeladen. Bildschirm bleibt an.
3. Ende per Tipp auf "Fertig" oder per Stimme ("Yorik, das Abendessen
   ist fertig", ein Satz im Kiosk-Sprachpfad). Kiosk zeigt "wird
   ausgewertet" und kehrt zur Tagesansicht zurück.
4. Nach 3 bis 8 Minuten (Workstation) Push an alle Teilnehmer:
   "Abendessen vom 10.9.: 3 Aufgaben, 2 schöne Momente."
5. Bericht im Handy: Abschnitte, Aufgaben als Vorschläge mit
   "Übernehmen" pro Zeile (Zugewiesener = genannte Person), Transkript
   aufklappbar, Sprecher mit Namen, unbekannte Stimmen als "Sprecher 3".
6. Am nächsten Morgen: "Plan meinen Tag" enthält die übernommenen
   Aufgaben, auch die, die jemand anders für mich übernommen hat.

Auf dem Handy statt Tablet: gleiche App, Aufnahme über den Browser,
Wake-Lock per Web-API, Hinweis wenn der Browser die Aufnahme im
Hintergrund beendet.

## 4. Architektur

### 4.1 Kern: Aufnahmen (`backend/recordings.py`)

Generisch, ohne Abendessen-Wissen. Später nutzbar für Elterngespräche,
Handwerkertermine, Vereinssitzungen.

Ablage: `data/recordings/<id>/` mit `audio.webm` (zusammengesetzt aus
den Stücken), `segments.json` (Sprecherabschnitte), `transcript.json`
(Abschnitte mit Zeit, Sprecher, Text). Größe pro Stunde etwa 15 MB.

Tabellen (Migration `136_recordings.sql`):

```
recordings(id, owner_user_id, title, kind, status, started_at, ended_at,
           duration_s, participants_json, space_id, error, created_at)
  status: recording | uploaded | processing | done | failed
recording_segments(id, recording_id, start_s, end_s, speaker_label,
                   user_id NULL, text, confidence)
```

`recordings` kommt in `TABLE_AREA` (Bereich "recordings") und ins
Owner-Mapping von `row_filter`; Teilnehmer bekommen `row_shares`-Zeilen
mit `level='read'`. Damit greift die geschlossene Admin-Ausnahme
automatisch: wer nicht Teilnehmer war, sieht nichts.

Pipeline (eine asyncio-Task pro Aufnahme, Heartbeat über
`backend/workers.py`, Status in der Tabelle):

1. Stücke zusammensetzen, mit ffmpeg nach 16 kHz Mono WAV.
2. Silero-VAD, dann `OfflineSpeakerDiarization` über die ganze Datei.
   Ergebnis: Abschnitte mit anonymen Sprechernummern.
3. Pro Sprechernummer ein Embedding aus den längsten Abschnitten, Abgleich
   mit `user_profiles.voice_embedding` der Teilnehmer (Schwelle wie
   `voice_id.MATCH_THRESHOLD`). Treffer bekommt `user_id`, Rest bleibt
   "Sprecher N".
4. Jeden Abschnitt einzeln mit Parakeet transkribieren (Abschnitte
   unter 30 s, das mag Parakeet; keine Zeitstempel-Ausrichtung nötig).
5. `transcript.json` schreiben, Status `done`, Aufrufer-Callback
   (der Bericht-Skill) anstoßen.

Aufwand ohne GPU, grob, für 60 Minuten Audio:

| Rechner | Diarisierung | Transkription | gesamt |
|---|---|---|---|
| Workstation 32 Kerne | 1 bis 3 min | 2 bis 5 min | 3 bis 8 min |
| Laptop 8 GB | 5 bis 10 min | 10 bis 20 min | 15 bis 30 min |

Routen (`/api/recordings`): `POST` (anlegen), `POST /{id}/chunk`
(Stück anhängen, `seq` fortlaufend), `POST /{id}/finish`, `GET /{id}`
(Status, Metadaten), `GET /{id}/transcript`, `DELETE /{id}`.
Größenschutz über `YORIK_MAX_UPLOAD_MB` pro Stück, Gesamtdauer über
`HOMEOS_RECORDING_MAX_MINUTES` (Standard 120).

Skills (für Chat, MCP und Apps gleichermaßen):

- `start_recording(title, kind, participants)`: legt die Aufnahme an,
  gibt `recording_id` zurück. Die Audio-Stücke schickt die App direkt an
  die Route, nicht über den Skill.
- `finish_recording(recording_id)`: schließt ab, startet die Pipeline.
- `recording_status(recording_id)`: Status, Fortschritt, Fehler.
- `recording_report(recording_id, template)`: LLM-Durchlauf über das
  Transkript mit einer Vorlage (siehe 4.2), speichert den Bericht,
  benachrichtigt die Teilnehmer.

Skill-Beschreibungen werden nach den bestehenden Regeln geschrieben
(ein Satz Beschreibung, eine Regel pro Zeile) und vor dem Einbau
vorgelegt.

### 4.2 Kern: Bericht-Vorlagen

`recording_report` arbeitet mit einer Vorlage, die Abschnitte und ihre
Bedeutung festlegt. Die erste Vorlage ist `dinner`:

```
decisions   Beschlüsse, Vereinbarungen (Wer, Was, Bis wann)
tasks       Aufgaben, die jemand übernommen hat oder übernehmen sollte
highlights  Schöne Momente, Lob, Erfolge
friction    Was nicht gut lief, Konflikte, ungeklärter Ärger
open        Offene Fragen, vertagte Themen
dates       Erwähnte Termine mit Datum
```

Ausgabe als JSON über Tool-Call (ninfer kann kein `json_schema`), in
Yoriks eigenem LLM. Für diesen einen Schritt wird Thinking eingeschaltet,
weil es ein langer Text mit Zuordnungen ist; bei Delegation an Hermes
über `ask_agent` mit `HOMEOS_AGENT_REASONING` regelbar. 60 Minuten
Gespräch sind etwa 15.000 Tokens und passen in beide Modelle.

Aufgaben aus dem Bericht werden nicht automatisch angelegt. Sie liegen
als Vorschläge im Bericht; "Übernehmen" ruft `add_task` mit `person`
auf, mit dem üblichen Undo.

### 4.3 Tagesplanung

`day_plans.context_for` wählt heute nur Aufgaben mit
`created_by_user_id = ich`. Erweiterung: zusätzlich Aufgaben, bei denen
ich Zugewiesener bin (bestehende Assignee-Verknüpfung). Sonst sieht
Beate eine Aufgabe, die Dirk aus dem Bericht für sie übernommen hat,
nicht in ihrem Plan.

### 4.4 Add-on: App "Abendessen" (`examples/dinner-v2/` oder eigener Ordner)

Fünf Dateien nach `docs/BUILD_AN_APP.md`:

- `manifest.json`: `invokes_skills: [start_recording, finish_recording,
  recording_status, recording_report, add_task]`, `reads` auf die
  Mitgliederliste, Kiosk-Sichtbarkeit (neues Manifest-Feld, siehe 4.5).
- `schema.sql` / `policies.sql`: `dinners(id, recording_id, date,
  participants, report_json, created_by)`, RLS auf Teilnehmer.
- `connector.py`: `@operation` für anlegen, Status, Bericht laden,
  Aufgabe übernehmen; ruft die Kern-Skills.
- `app.js`: drei Ansichten. Start (Teilnehmer, Aufnahmeknopf),
  Aufnahme (Dauer, Pause, Fertig, Chunk-Upload), Bericht (Abschnitte,
  Aufgaben mit Übernehmen, Transkript aufklappbar).

Der App-Teil enthält keine Audio-Verarbeitung und keine
Hintergrundarbeit. Genau das ist der Punkt, der in der Anleitung
gezeigt wird: die schwere Arbeit liegt im Kern, die App kombiniert.

### 4.5 Erwartete Reparaturen an der App-Plattform

Die Plattform hat noch keine echte App durchlaufen. Was der Bau
voraussichtlich aufdeckt:

- Mikrofonzugriff im Sandbox-Iframe (`allow="microphone"` in der
  Iframe-Policy, CSP für Blob-Upload).
- Datei-Upload aus dem Iframe an eine Kern-Route mit dem App-Token.
- Kiosk: installierte Apps erscheinen heute nicht in der Ambient-Ansicht.
  Manifest-Feld `kiosk: {tile: true}` und eine Kachelzeile im Kiosk.
- Push aus einer App: läuft über den Kern-Skill, die App braucht keinen
  eigenen Weg.
- Uninstall wischt heute die App-Daten. Für die Anleitung reicht das,
  für echte Nutzung braucht es "Update ohne Datenverlust" (Backlog,
  nicht Teil dieses Plans).

## 5. Datenschutz

- Aufnahme nur nach Bestätigung am Gerät; sichtbares Aufnahmesymbol die
  ganze Zeit; Teilnehmerliste wird gespeichert.
- Audio bleibt auf dem Yorik-Rechner. Aufbewahrung Standard 30 Tage
  (`HOMEOS_RECORDING_RETENTION_DAYS`), danach bleibt nur der Bericht.
  Option pro Aufnahme: Audio sofort nach dem Bericht löschen.
- Bericht und Transkript sehen nur Teilnehmer. Keine Admin-Ausnahme.
- Kinder ohne Stimmprofil bleiben anonym ("Sprecher 3"). Ob Kinder den
  Bericht sehen, regelt die bestehende Freigabe des Admins.
- `docs/PRIVACY.md` bekommt einen Abschnitt "Aufnahmen".

## 6. Risiken

- **Tonqualität.** Tablet zwei Meter entfernt, Geschirr, Überlappungen.
  Sprecherzuordnung wird schwanken. Darum ist der erste Meilenstein ein
  echtes Abendessen mit rohem Transkript, vor jeder UI-Arbeit. Falls die
  Zuordnung unbrauchbar ist: Bericht ohne Namen, Aufgaben mit "jemand
  soll", Zuordnung erst beim Übernehmen.
- **Browser-Aufnahme über eine Stunde.** Chunk-Upload alle zwei Minuten
  und Wake-Lock fangen Abstürze und Bildschirm-Aus ab. Auf iOS Safari
  ist Hintergrund-Aufnahme nicht möglich; das wird in der App angesagt.
- **CPU-Zeit auf kleinen Rechnern.** 15 bis 30 Minuten auf dem
  Laptop sind hinnehmbar, weil der Bericht per Push kommt. Die Pipeline
  darf Yoriks Chat nicht blockieren: eigener Thread-Pool, niedrige
  Priorität.
- **Plattform-Reparaturen.** Phase 3 kann länger dauern als geplant, weil
  der App-Vertrag zum ersten Mal ernsthaft benutzt wird. Das ist
  eingepreist und gewollt.

## 7. Phasen

| Phase | Inhalt | Ergebnis | Aufwand |
|---|---|---|---|
| 1 | Kern-Aufnahmen: Migration, Ablage, Chunk-Routen, Pipeline (VAD, Diarisierung, Profilabgleich, Parakeet je Abschnitt), Skills `start/finish/status` | rohes Transkript eines echten Abendessens mit Sprechern | 2 Tage |
| 2 | `recording_report` mit Vorlage `dinner`, Aufgaben-Vorschläge, Push an Teilnehmer, Tagesplanung berücksichtigt Zugewiesene, Aufbewahrung | Bericht in der Bell und per Push, Aufgaben übernehmbar im Chat | 1 Tag |
| 3 | App "Abendessen" nach App-Vertrag, Kiosk-Kachel, Handy-Ansicht, Plattform-Reparaturen aus 4.5 | Aufnahme und Bericht ohne Chat bedienbar | 1 bis 2 Tage |
| 4 | Anleitung `docs/help/18-add-ons.md` aus dem Bau-Protokoll, Beispiel-App im Repo, Video-Drehbuch | "Dein erstes Yorik-Add-on" | 1 Tag |

Nach jeder Phase: Tests grün, Workstation neu gestartet, ein echter
Durchlauf am Tisch.

## 8. Tests

- Pipeline-Test mit einer synthetischen Aufnahme: zwei TTS-Stimmen
  (Supertonic) im Wechsel, erwartete Sprecherabschnitte und Text.
- Profilabgleich: enrolltes Profil wird erkannt, fremde Stimme bleibt
  "Sprecher N".
- Freigabe: Nicht-Teilnehmer bekommt 404 auf Bericht und Transkript,
  auch als Admin.
- Bericht-Skill mit festem Transkript: alle sechs Abschnitte, Aufgaben
  mit Person, keine erfundenen Termine.
- Tagesplanung: zugewiesene Aufgabe eines anderen Erstellers erscheint.
- App: Manifest lädt, Install und Uninstall laufen, Connector ruft die
  Skills mit dem App-Token.

## 9. Offene Entscheidungen

1. Bericht-LLM: Yoriks eigenes Modell oder Delegation an Hermes per
   `ask_agent`? Vorschlag: eigenes Modell mit Thinking für diesen
   Schritt, Hermes als Option in der Vorlage.
2. Audio-Aufbewahrung 30 Tage oder sofort löschen als Standard?
   Vorschlag: 30 Tage, weil man Zuordnungen nachhören will.
3. Ordner der App: `examples/dinner-v2/` (Teil der Anleitung) oder ein
   eigenes Repo `yorik-dinner`? Vorschlag: erst `examples/`, später
   herauslösen, wenn der Marktplatz Quellen außerhalb des Repos kann.
4. Name der Kachel und der App: "Abendessen", "Tischgespräch", oder
   neutral "Aufnahme"? Für Firmen-Haushalte wäre "Besprechung" passender
   (Yorik dient auch Teams; kein Familien-Branding im Kern).
