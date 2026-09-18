# Plan: Familientafel (Wandtablet im Dæly-Stil) als Opt-in-App

Stand 2026-09-18. Skizze für Beate: https://claude.ai/artifact/6Cq49J8NGF2uKHxKCBC4Zk

## Ziel

Das Kiosk-Tablet zeigt die Familie auf einen Blick: Woche in
Personenfarben, heute pro Person zum Abhaken, Routinen der Kinder
sichtbar. Ohne Anmeldung ansehen; Abhaken nach Tipp auf den eigenen
Kreis (Kinder mit PIN). Die Fotowand ist abschaltbar. Drei Vollbild-
Modi, leicht umschaltbar: Kalender, Aufgaben, gemischt (Skizze).

Was die Recherche vorgibt (Trustpilot, Wienerin-Test, futurezone):
Farbe und Foto pro Person; lesbar quer durchs Zimmer; Kinder schauen
selbst nach; Routinen wiederholen sich sichtbar; kein Abo, EU-Daten.
Punkte und Essensplan sind auch bei Dæly die schwachen Teile und
kommen erst, wenn die Tafel zwei Wochen benutzt wird.

## Phasen

| Phase | Inhalt | Ergebnis |
|---|---|---|
| 1 | Person: Farbe und Foto im Profil (Settings → You), Upload klein und quadratisch beschnitten; persönliche Kalender übernehmen die Farbe; Avatare überall aus dem Profil (Kalender, Aufgaben nach Person, Kiosk-Anmeldung). | Kalender ist sofort personenfarbig, mit Bildern. |
| 2 | Opt-in-Apps ernst gemeint: Recordings und Familientafel als Opt-in; Skills einer ausgeschalteten App verschwinden aus Chat und MCP; Doku. | Wer das Aufnahmetool nicht will, sieht nichts davon. |
| 3 | Kiosk-Modus pro Gerät: `photos`, `calendar`, `tasks`, `board` (gemischt); Fotowand aus = anderer Modus. Umschalten am Tablet mit einem Knopf unten rechts (mit Merken pro Gerät) und in Settings → Devices. | Das Tablet zeigt, was die Familie will. |
| 4 | Die Tafel selbst: Wochenstreifen, Spalten pro Person, Routinen, Abhaken nach Avatar-Tipp, Rückkehr in den gewählten Modus nach Ruhe; Vollbild-Kalender (Woche/Monat) und Vollbild-Aufgaben als eigene Modi; Handy-Ansicht derselben Tafel. | Skizze in echt. |
| 5 | Kapitel in `docs/ADDONS.md` (zweites Beispiel: ohne Modelle, nur vorhandene Daten neu angeordnet), Hilfe-Seite, Changelog. | Anleitung für Bots und Entwickler. |

## Regeln

- Sichtbar ist auf der Tafel nur, was jede Person für den Haushalt
  freigibt (bestehendes Sharing, Bereich Kalender und Aufgaben); Kinder
  setzt der Admin. Keine Admin-Ausnahme.
- Der bestehende Kalender wird nicht umgebaut; er bekommt nur die
  Personenfarben und Bilder.
- Die Tafel ist der Kiosk, nicht ein Tab dahinter; Ruhe führt in den
  gewählten Modus zurück, nicht zwingend zur Fotowand.
- Ein Add-on nach `docs/ADDONS.md`: Migration, Modul, Routen, Seite,
  Tests, Doku; als Opt-in-App registriert.

## Offen

- Foto-Speicherort: `data/avatars/<user_id>.jpg`, 256 px, per Route
  ausgeliefert mit Sitzungs- oder Kiosk-Recht.
- Routinen der Kinder = wiederkehrende Aufgaben mit Kategorie
  „Routine“; keine neue Tabelle.
- Punktesystem: später, eigenes Modul, abschaltbar.
