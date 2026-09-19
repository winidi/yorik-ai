# Plan: Suche über ganz Yorik (Stichwort + Bedeutung)

Stand 2026-09-19.

## Ausgangslage

`/api/search` und der Skill `universal_search` fragten fünf Quellen ab:
Paperless (semantisch), Immich (CLIP), E-Mail und WhatsApp (tsvector,
`simple`, kein Stemming), Kalender (LIKE, **ohne Sichtbarkeitsfilter**).
Aufgaben, Kontakte, Aufnahmen und Entwürfe fehlten. Der semantische
WhatsApp-Index (`docs.wa_chunks`) bedient nur den Entwurfs-Generator.

## Ziel

Eine Anfrage in der Suchpalette, im Chat oder über MCP findet alles,
was die Person sehen darf, egal wo es liegt, auch wenn das Wort nicht
wörtlich vorkommt ("Stromrechnung" findet die Mail der Stadtwerke).

## Aufbau

| Teil | Inhalt | Wo |
|---|---|---|
| 1 | Kalendersuche mit `visible_event_filter`; private Termine anderer fallen weg. | `backend/search_routes.py` |
| 2 | Neue Quellen mit Stichwortsuche: Aufgaben, Kontakte, Aufnahmen (Titel, Transkript, Bericht), Entwürfe. Sichtbarkeit über `spaces.row_filter` bzw. Besitzer. | `backend/search_routes.py` |
| 3 | Ein semantischer Index `search_chunks` (Quelle, Zeile, Stück, Text, Hash, `vector(384)`), gefüllt aus E-Mail, WhatsApp, Aufgaben, Kontakten, Terminen, Aufnahmen, Entwürfen. Paperless und Immich behalten ihre eigenen Indizes. | `migrations_pg/141_search_chunks.sql`, `backend/search_index.py` |
| 4 | Hintergrund-Indexer: Erstlauf über den Bestand, danach alle fünf Minuten Neues, Geändertes (Hash) und Gelöschtes. Auf dem Startbildschirm als Worker sichtbar. | `backend/search_index.py`, `backend/main.py` |
| 5 | Hybride Suche: pro Quelle erst Stichworttreffer, dann semantische Treffer unter einer Distanzschwelle, ohne Dubletten. | `backend/search_routes.py` |

## Regeln

- **Sichtbarkeit wird beim Suchen geprüft, nicht im Index gespeichert.**
  Der Index kennt nur Quelle und Zeile; jede Abfrage joint die
  Quelltabelle mit derselben Regel wie die App (Besitzer, Space,
  `row_shares`). Eine geänderte Freigabe wirkt sofort, ohne Neuaufbau.
- Texte werden in Stücke von etwa 600 Zeichen geteilt (der Embedder
  schneidet bei 128 Tokens ab); pro Mail höchstens drei Stücke.
- Unveränderliches (Mails, WhatsApp, fertige Aufnahmen) wird einmal
  indexiert; Kleines und Veränderliches (Aufgaben, Termine, Kontakte,
  Entwürfe) wird per Hash verglichen.
- Embedder ist der vorhandene lokale MiniLM (mehrsprachig, CPU, etwa
  200 Texte pro Sekunde); der Erstlauf dauert wenige Minuten.
- Fällt der Embedder aus, bleibt die Stichwortsuche.

## Offen

- Deutsches Stemming für die tsvector-Spalten (eigene Migration, die
  generierten Spalten müssen neu gebaut werden).
- Chat-Verläufe und Notizen/Homebase als weitere Quellen.
- Ein ANN-Index lohnt erst ab einigen hunderttausend Stücken.
