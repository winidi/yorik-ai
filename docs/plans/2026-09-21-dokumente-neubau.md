# Plan: Dokumente — schlanker Neubau neben Compose

Stand 2026-09-21. Noch nichts gebaut; mit Dirk besprochen, Richtung und
die vier offenen Fragen sind entschieden ("Neubau daneben").

## Ausgangslage

Compose ist um Vorlagen mit Feldern gebaut: eine Vorlage hat ein
Jinja-Gerüst und eine Liste `ask_user_for_args`, das LLM soll im Chat
diese Felder treffen, fehlende Pflichtfelder lösen eine Nachfrage-Karte
aus. Das ist der Grund für die Unzuverlässigkeit: es wird nach Dingen
gefragt, die im Gespräch oder in den Kontakten vorhanden waren, und
`compose_draft` (1.387 Zeilen) schneidet Anrede, Gruß und Datum wieder
aus dem LLM-Text, weil die Vorlage sie selbst setzt. Umfang heute:
`ComposeApp.tsx` 4.999 Zeilen (Frontend gesamt rund 7.500), zehn
Skills, `backend/compose/` 1.512 Zeilen, rund 30 Routen.

Das lokale Modell (Qwen 27B) schreibt einen ganzen Brief inzwischen
besser frei, als es 40 Felder trifft.

Was gut ist und bleibt: PDF über Gotenberg (`compose/pdf.py`), Ablage
in Paperless (`compose/save.py`), Nummernkreise mit Vorschau/Verbrauch
und Protokoll (`compose/series.py`), der XML-Teil der
ZUGFeRD-Erweiterung, der TipTap-Editor als Baustein, das Senden per
Mail.

ZUGFeRD-Test vom 2026-09-21: XML gültig (XSD + EN 16931, Mustang 2.26),
PDF **ungültig** (kein PDF/A-3; Gotenberg liefert ein normales PDF).
Mit `pdfa=PDF/A-3b` im Gotenberg-Aufruf besteht dieselbe Rechnung
vollständig. Dazu: Profil-Etikett BASIC bei EN-16931-XML, Postleitzahl
als Zahl, stiller Rückfall auf ein normales PDF.

## Ziel

Eine kleine App "Schreiben" für drei Dinge: **Brief, Rechnung,
Angebot**. Der Chat legt immer sofort einen Entwurf an und fragt nie
nach. Jedes Dokument eines Nutzers sieht gleich aus (sein Briefpapier),
jeder Nutzer kann seines anpassen. Rechnungen sind in Deutschland
gültige E-Rechnungen, und wenn nicht, sagt Yorik es.

## Grundsätze

1. **Aussehen ist Code, Inhalt ist Text oder Daten.** Das LLM liefert
   nie HTML und fasst das Layout nie an.
2. **Brief = freier Text.** Das LLM schreibt alles zwischen Betreff und
   Unterschrift, mit Anrede und Gruß. Der Code setzt Absender,
   Empfänger, Datum, Falzmarken darum. Kein Herausschneiden mehr.
3. **Rechnung und Angebot = strukturierte Daten.** Kunde, Positionen
   (Liste, beliebig lang: Text, Menge, Einheit, Einzelpreis, Steuersatz),
   Leistungszeitraum, Zahlungsziel, Einleitungs- und Schlusstext. Summen,
   Steuer, Nummer, Fälligkeit rechnet der Code. Sichtbares PDF und XML
   entstehen aus **denselben** Daten.
4. **Entwurf zuerst, nie nachfragen.** Was fehlt, ist im Entwurf ein
   markiertes leeres Feld. Ein Brief blockiert nie. Eine Rechnung lässt
   sich erst **fertigstellen**, wenn die Pflichtangaben da sind; die
   Liste dessen, was fehlt, steht neben dem Knopf.
5. **Fertigstellen ist ein eigener Schritt.** Erst dann wird die Nummer
   verbraucht, das PDF (mit XML) erzeugt und unveränderlich abgelegt.
   Vorher ist alles Entwurf und kostet keine Nummer.
6. **Kein stiller Rückfall.** Kann die E-Rechnung nicht erzeugt werden,
   sieht der Nutzer das und entscheidet.
7. **Compose bleibt bis zum Umschalten unangetastet benutzbar.**

## Aufbau

| Teil | Inhalt | Wo |
|---|---|---|
| 1 | **Briefpapier pro Nutzer**: Logo, Akzentfarbe, Schrift (kleine Auswahl, eingebettet), Absenderblock, Fußzeile (Bank, Steuernummer, USt-ID), Standardtexte (Zahlungsziel, Kleinunternehmer §19, Gruß), Land. Startwerte aus dem Profil (`business_name`, `tax_id`, `iban`, Adresse). Mehrere Briefpapiere pro Person möglich (privat / Firma), eines ist Standard. | `migrations_pg/NNN_letterheads.sql`, `backend/documents/letterhead.py`, Settings → You → Briefpapier mit Live-Vorschau |
| 2 | **Drei Layouts** als Jinja-Dateien im Code: `letter`, `invoice`, `quote` (= invoice ohne Nummernkreis-Pflicht, mit Gültig-bis). DIN-5008-Fenster für DE, schlichtes Layout sonst. Nehmen Briefpapier + Inhalt, geben HTML. | `backend/documents/layouts/` |
| 3 | **Datenmodell** `documents`: Besitzer, Art, Status (`draft` / `final`), Briefpapier, Empfänger (Kontakt-ID + eingefrorene Adresse), `content_json` (Brief: Betreff + Text als HTML aus dem Editor; Rechnung: die Daten aus Grundsatz 3), Nummer, PDF-Pfad, Paperless-ID, Versionen. Sichtbarkeit wie überall: Besitzer und wem geteilt. | `migrations_pg/NNN_documents.sql`, `backend/documents/store.py` |
| 4 | **Rechnen und Prüfen**: Summen in `Decimal`, Steuer je Satz, Pflichtangaben nach §14 UStG als Prüfliste (`missing()`), Kleinunternehmer-Fall. Eine Funktion, die PDF **und** XML füttert. | `backend/documents/invoice.py` |
| 5 | **PDF**: `compose/pdf.py` bekommt `pdfa`. Rechnungen in DE immer PDF/A-3b. | `backend/compose/pdf.py` (bleibt, wird von beiden benutzt) |
| 6 | **E-Rechnung**: `build_xml` aus der Erweiterung weiterverwenden, gefüttert aus Teil 4 statt aus Jinja-Feldern; Etikett = tatsächliches Profil (EN 16931); PLZ als Text; nach dem Einbetten Selbstprüfung (XSD + Schematron aus `factur-x`), Ergebnis am Dokument gespeichert und sichtbar. Bleibt Erweiterung (Land DE), aber mit deutlichem Hinweis, wenn sie fehlt. | `extensions/zugferd/`, `backend/documents/einvoice.py` |
| 7 | **Nummernkreise**: `compose/series.py` unverändert weiterverwenden; Verbrauch nur beim Fertigstellen. | — |
| 8 | **Routen** `/api/documents`: anlegen, lesen, ändern, Vorschau-PDF, fertigstellen, senden, nach Paperless, löschen (nur Entwürfe), Liste. Rund zehn statt dreißig. | `backend/documents/routes.py` |
| 9 | **Skills**: `write_letter(recipient, subject, text)` und `write_invoice(customer, lines, …)` (mit `kind=quote`). Beide legen einen Entwurf an und geben die Karte "Öffnen" zurück. Empfänger wird über die Kontakte aufgelöst; nicht gefunden = Name steht drin, Adresse leer markiert. Ersetzt die zehn Compose-Skills. | `backend/skills/write_letter`, `backend/skills/write_invoice` |
| 10 | **App** `/documents`: Liste links (Entwürfe, Fertige), rechts das Blatt. Brief: TipTap im Blatt. Rechnung: Positionstabelle zum Tippen, Summen rechnen live mit, Text davor und danach. Rechte Spalte: Empfänger (Kontaktwahl), Briefpapier, was fehlt. Unten: PDF, Fertigstellen, Senden, Paperless. "Frag Yorik" auf markiertem Text (`compose/polish.py`) bleibt. Ziel: unter 1.500 Zeilen in mehreren Dateien. | `frontend-react/src/apps/documents/` |
| 11 | **Umschalten**: Dock zeigt "Dokumente"; alte Entwürfe (`compose_drafts`) werden als Briefe übernommen (HTML bleibt HTML); Nummernkreise sind dieselben. Danach Compose-App, ihre Skills, Vorlagen, Community-Vorlagen, Feld-Extraktion entfernen. | eigener Commit, erst nach Dirks Wort |

## Reihenfolge

Jede Stufe ist für sich benutzbar und einzeln committet; die ganze
Testsuite läuft vor jedem Commit.

1. **Fundament**: Teile 1–3 und 5 (Briefpapier, Layouts, Tabelle,
   PDF/A). Test: ein Brief aus festen Daten wird ein PDF mit dem
   Briefpapier des Nutzers; zwei Nutzer, zwei Aussehen.
2. **Brief ganz**: Teile 8–10 für den Brief, Skill `write_letter`.
   Abnahme im Chat: "Schreib der Hausverwaltung, dass …" ergibt ohne
   Rückfrage einen Entwurf; fehlende Adresse ist markiert.
3. **Rechnung und Angebot**: Teile 4, 6, 7, Rechnungs-Oberfläche, Skill
   `write_invoice`. Abnahme: Mustang-Validator besteht (PDF und XML) in
   einem Test, der im CI übersprungen wird, wenn Java fehlt; die
   Selbstprüfung aus `factur-x` läuft immer.
4. **Umschalten** (Teil 11), wenn Dirk und Beate eine Woche damit
   gearbeitet haben.

## Regeln

- Beträge nie als `float`; `Decimal`, kaufmännisch gerundet je Zeile
  und je Steuersatz, so wie die XML-Regeln (BR-CO-*) es verlangen.
- Eine fertiggestellte Rechnung ändert sich nicht mehr. Korrektur =
  Storno + neue Rechnung (Stornorechnung als vierte Art erst, wenn
  gebraucht; das Modell lässt sie zu).
- Empfängeradresse wird beim Fertigstellen eingefroren; spätere
  Änderungen am Kontakt ändern alte Dokumente nicht.
- Kein Nutzer muss `config.env` anfassen: Briefpapier, Nummernkreise,
  Erweiterung installieren — alles in den Einstellungen.
- Andere Länder: Layout ohne DIN-Fenster, keine §14-Prüfliste, kein
  XML. Die Prüfliste ist pro Land eine kleine Funktion, DE zuerst.
- Eigenes HTML-Gerüst hochladen bleibt möglich, aber nur für Briefe und
  erst nach Stufe 4; für Rechnungen nicht (Pflichtangaben).

## Was wegfällt

Vorlagen mit `ask_user_for_args`, Nachfrage-Karten, Community-Vorlagen
und deren Installation, `compose_extract_args`,
`compose_check_recipient`, `compose_check_template_args`,
`pick_compose_template`, `view_compose_template`,
`list_compose_templates`, das Herausschneiden von Anrede und Gruß, die
Textvorlagen für Kündigungen. E-Mails schreibt der Mail-Composer
(`email_draft`), nicht mehr Compose.

## Entschieden (Dirk, 2026-09-21)

1. Datenmodell für mehrere Briefpapiere pro Person, die Oberfläche
   zeigt vorerst eines.
2. "Angebot → Rechnung" mit einem Klick gehört zu Stufe 3.
3. Eingeschränkte Konten (Kinder): Briefe ja, Rechnungen und Angebote
   nein.
4. Die App heißt **"Schreiben"** (`/write`, Code unter `documents`).
