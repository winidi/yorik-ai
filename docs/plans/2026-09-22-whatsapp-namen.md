# Plan: WhatsApp-Namen so, wie WhatsApp sie zeigt

Stand 2026-09-24. Paket A aus der Recherche vom 22.; der Versionssprung
auf Baileys 7 (Paket B) ist bewusst nicht Teil davon. Die Teile 1–7 sind
gebaut, Teil 8 wartet auf Dirks Wort — siehe "Stand der Arbeit" unten.

## Ausgangslage

Yorik zeigt in der WhatsApp-App fast durchweg den **pushName** — den
Namen, den das Gegenüber sich selbst gegeben hat — statt des Namens,
unter dem WhatsApp die Person auf dem Telefon führt. Gemessen an den
50 zuletzt benutzten Chats:

| | |
|---|---|
| Gruppen (tragen ihr `subject`, richtig) | 13 |
| Einzelchats mit dem pushName als Titel | 21 |
| Einzelchats mit einem Namen, der davon abweicht | **0** |
| Einzelchats, für die nie ein pushName kam | 16 (davon 5 ohne jeden Namen) |
| LID-adressierte Chats darunter | 24 |

Kein einziger Adressbuchname ist je in der Liste angekommen. Dabei
liegen die richtigen Namen längst in der Bridge: `name-map.json` hält
716 Einträge, davon **523 für Telefon-JIDs, von denen sich kein
einziger durch einen gesehenen pushName erklären lässt** — das sind
die Namen aus dem App-State (`contactAction.fullName`), also genau
das, was das Telefon anzeigt. Sie landen nur nie dort, wo der Chat
läuft:

| `wa_chats` | Zeilen | benannt | mit Nachrichten |
|---|---|---|---|
| unter Telefonnummer (`@s.whatsapp.net`) | 532 | 523 | **23** |
| unter LID (`@lid`) | 199 | 20 | **26** |

Die Unterhaltungen laufen unter LID, die guten Namen liegen unter der
Nummer, und nichts verbindet die beiden. Die 509 leeren PN-Zeilen sind
Karteileichen: `server.js:374` schickt jeden Adressbuchkontakt aus
`contacts.upsert` als `chat`-Ereignis, das Backend legt daraus eine
Chat-Zeile an, in der nie etwas passiert.

Drei Fehler in der Bridge dahinter:

1. **`c.lid` wird weggeworfen.** Baileys 6.7.24 liefert bei
   `contacts.upsert` neben dem Adressbuchnamen die LID derselben
   Person mit (`chat-utils.js:655`: `lid: action.contactAction.lidJid`).
   `server.js:370` liest nur `c.id` und den Namen.
2. **`_learn` ist last-writer-wins** (`server.js:246`), ohne Rangfolge.
   Ein Adressbuchname hält nur bis zur nächsten Nachricht derselben
   Person, dann gewinnt ihr pushName.
3. **Eigene Nachrichten benennen den Gesprächspartner.**
   `server.js:325` lernt `participant || remote` ohne `fromMe`-Prüfung;
   im Einzelchat ist `participant` leer und `remote` der andere. In der
   Name-Map tragen dadurch sechs fremde LIDs Dirks eigenen Namen.
   `backend/whatsapp.py:148` behandelt bisher nur das Symptom.

## Ziel

Yorik zeigt dieselbe Kette wie WhatsApp selbst:

> Adressbuchname → verifizierter Business-Name → pushName → Nummer

Yoriks eigenes Kontaktbuch bleibt außen vor. Ein Chat, den WhatsApp
"Elena Müller" nennt, heißt in Yorik "Elena Müller", auch wenn der
Kontakt dort anders gespeichert ist. Die Verknüpfung zu Yoriks
Kontakten bleibt, wie sie ist, und ändert den Titel nicht.

## Aufbau

| Teil | Inhalt | Wo |
|---|---|---|
| 1 | **Alias Nummer ↔ LID.** `c.lid` aus `contacts.upsert` und `contacts.update` mitschreiben, in `alias-map.json` neben der Name-Map, gleiche gebündelte Speicherung. | `whatsapp-bridge/server.js` |
| 2 | **Namen mit Herkunft.** Die Name-Map hält je JID `{name, source}` mit `book` > `business`/`chat` > `push` (Gruppen: `subject` als `book`). Ein schwächerer Wert überschreibt einen stärkeren nie; gleicher Rang aktualisiert. Altes Format beim Laden als `push` migrieren. | `whatsapp-bridge/names.js` (neu), `server.js` |
| 3 | **Nachschlagen über den Alias.** Fragt jemand nach einer LID, ohne dass für sie ein `book`-Name vorliegt, antwortet die Bridge mit dem Namen der zugehörigen Nummer. Gilt für `/contact-names`, `/contact-names/:jid` und jedes ausgehende Ereignis. | `whatsapp-bridge/server.js` |
| 4 | **Eigene Nachrichten überspringen.** In `messages.upsert` und `messaging-history.set` nur lernen, wenn `!m.key.fromMe`; eigene JID und eigene LID nie als fremden Namen lernen. | `whatsapp-bridge/server.js` |
| 5 | **Keine Karteileichen mehr.** `contacts.upsert` sendet `contact` statt `chat`. Das Backend lernt daraus den Namen und aktualisiert eine vorhandene Chat-Zeile, legt aber keine an. | `whatsapp-bridge/server.js`, `backend/whatsapp.py` (`_handle_event`) |
| 6 | **Herkunft bis in die Liste.** `wa_chats.name_source` (Migration **159**, 156 war inzwischen vergeben). `_upsert_chat` setzt den Namen nur bei gleichem oder höherem Rang. Steht dort `push`, zeigt die Liste den Namen mit vorangestellter Tilde — `~Ela`, so wie WhatsApp es kennzeichnet. | `migrations_pg/159_wa_chats_name_source.sql`, `backend/whatsapp.py`, `frontend-react/src/apps/whatsapp/{WhatsAppApp.tsx,types.ts}` |
| 7 | **Bestand nachziehen.** Der Backfill nimmt Alias und Rang und schreibt **auch `wa_chats.name`** — bisher rührte er nur `contacts` und `contact_channels` an, deshalb blieb die Liste selbst nach einem Lauf unverändert. Er stößt vorher einen Adressbuch-Resync an (siehe 7b). | `backend/contact_autocapture.py` (`backfill_whatsapp_display_names`) |
| 7b | **Adressbuch neu ziehen.** `POST /users/:id/resync-contacts` ruft `resyncAppState(["critical_unblock_low"], true)`. Ohne das sieht die Bridge nach einem Neustart nur noch Änderungen, und die Alias-Tabelle bliebe leer, bis jemand einen Kontakt am Telefon bearbeitet. Kein Neu-Pairing, kein Verkehr an fremde Nummern. | `whatsapp-bridge/server.js` |
| 8 | **Optional, nur auf Dirks Wort:** einmalige `onWhatsApp`-Abfrage über die bekannten Nummern, um Aliase für LIDs zu gewinnen, die über den App-State nie kamen. 6.7.24 gibt die LID dort zurück (`chats.js:148`); in Baileys 7 fällt dieser Weg weg. | `whatsapp-bridge/server.js` |

## Regeln

- **Kein Abstieg.** Ein Name darf nur von einer gleich starken oder
  stärkeren Quelle ersetzt werden. Eine Umbenennung im Telefonbuch
  wirkt (gleicher Rang), ein neuer pushName wirft sie nicht um.
- **Vier Quellen statt drei.** Neben `book`, `business` und `push` gibt
  es `chat`: der Name, den Baileys an einer Chat-Zeile im Verlaufs-Sync
  mitliefert. Er ist meist der Adressbuchname, aber nicht nachweisbar —
  deshalb über `push` und unter `book`.
- **Eine Nachricht benennt keinen Chat.** Sie sagt, wer geschrieben hat:
  in der Gruppe das Mitglied, bei eigenen Nachrichten der Nutzer. Nur
  eine eingehende Einzelnachricht benennt ihren Chat, und nur als
  `push`.
- **Der pushName wird nicht versteckt, sondern gekennzeichnet.** Für
  Unbekannte ist er das Beste, was es gibt — die Tilde sagt, dass der
  Name vom Gegenüber stammt und nicht aus dem Telefonbuch.
- **Yoriks Kontaktbuch ändert keinen Chat-Titel.** Auch nicht nach
  einer Verknüpfung, auch nicht nach einem vCard-Import.
- **Nichts wird gelöscht.** Ausgeblendet wird zweierlei: Zeilen ohne
  Zeitstempel (die nie eine Nachricht trugen) und Zeilen ohne Namen
  *und* ohne Nachricht — Stummel aus dem Verlaufs-Sync, die nur eine
  15-stellige LID zeigen könnten (84 Stück nach dem Neu-Koppeln, von
  Dirk am 25.09. so entschieden). Ein namenloser Chat **mit**
  Nachrichten bleibt: er ist eine echte Unterhaltung. Entfernt wird
  nichts, denn die alten PN-Zeilen tragen die Namen, aus denen Teil 7
  sich bedient:
  sie tragen die Namen, an denen Teil 7 sich bedient, und an ihnen
  hängen Kontaktkanäle. Löschen später und nur, wenn es einen Grund
  gibt.
- **Kein Neu-Pairing.** Alles hier ist additiv; der Auth-Zustand in
  `/data/sessions/<id>/baileys-auth` wird nicht angefasst. Die Bridge
  muss neu gebaut und gestartet werden, mehr nicht.
- Baileys bleibt auf 6.7.24. Jeder Teil hier funktioniert auf dieser
  Version; nichts davon nimmt einen späteren Sprung auf 7 vorweg oder
  verbaut ihn.

## Stand der Arbeit (2026-09-24)

Teile 1–7 sind gebaut und getestet, aber noch nicht in Betrieb: die
Bridge läuft bis zu ihrem Neubau mit dem alten Stand, und die Migration
wird beim nächsten Backend-Start angewandt. Dazu kamen zwei Dinge, die
beim Bauen auffielen und zum selben Fehlerbild gehören:

- Eine **Gruppennachricht benannte die Gruppe nach dem Mitglied**, das
  gerade geschrieben hatte (`_insert_message` reichte den pushName als
  Chatnamen durch, auch für `@g.us` und für eigene Nachrichten).
- Ein Chat-Ereignis ohne Vorschautext **löschte die letzte Nachricht**
  aus der Liste (`last_message_text=?` statt `COALESCE`).

Beim Start der Bridge werden außerdem einmalig die Einträge verworfen,
die den eigenen Namen des Nutzers tragen (sechs Stück, nur `push`), und
die alte `name-map.json` wird ins neue Format überführt.

Danach: erneut messen (siehe Abnahme). Erst dieser Rest entscheidet, ob
Teil 8 und ob Paket B sich lohnen.

## Abnahme — gemessen am 2026-09-25

Dieselbe Messung über die 50 zuletzt benutzten Chats:

| | 22.09. | 25.09. |
|---|---|---|
| mit Adressbuchnamen | 0 | **46** |
| mit pushName (Tilde) | 21 | 2 |
| ohne Namen | 5 | 2 |
| Zeilen in der Liste | 758 | 206 → 122 |

In der Bridge: 553 von 756 Namen aus dem Adressbuch, 8 von
Geschäftskonten, 195 selbstgewählt; **522 LID↔Nummer-Paare**, für jedes
davon ein Adressbuchname. Die sechs fremden "Dirk"-Einträge sind beim
ersten Start der neuen Bridge verschwunden; die zwei verbliebenen sind
Dirks eigene Nummer und seine eigene LID.

## Warum es ohne Neu-Koppeln nicht ging

Der Resync lief, aber WhatsApp' Antwort war nicht zu öffnen:

    resyncing critical_unblock_low from v0
    failed to sync state from version
      Error: failed to find key "AAAAAMuW" to decode mutation

Der Schnappschuss **kam an**; es fehlte der App-State-Schlüssel, mit dem
er verschlüsselt war. Diese Schlüssel teilt das Telefon einmalig beim
Koppeln (`appStateSyncKeyShare`); geht so eine Nachricht verloren — im
Log standen seit Monaten `MessageCounterError` / `failed to decrypt
message` — ist die Collection dauerhaft zu. Sichtbares Zeichen: im
Auth-Ordner lag nur `app-state-sync-version-critical_block.json`, vom
**21. Juni**. Seither kam das Adressbuch an und wurde weggeworfen,
weshalb alles aus der LID-Zeit nur pushNames trug.

Das Neu-Koppeln (Telefon → Verknüpfte Geräte → abmelden, neuer QR) hat
frische Schlüssel gebracht; danach lagen vier App-State-Versionsdateien
und fünf Schlüssel dort, und die Zahlen oben standen binnen Minuten.

**Nicht** die Baileys-Version war schuld — Paket B hätte daran nichts
geändert und ist für die Namen vom Tisch. Offen bleiben nur Menschen,
die nicht im Telefonbuch stehen: für ihre LID hat auch WhatsApp keinen
Namen, und die Telefonnummer dahinter kennen wir nicht.

## Tests

Beide neu — zu WhatsApp gab es bisher keinen Test.

`whatsapp-bridge/names.test.mjs` (12 Fälle, `node --test
whatsapp-bridge/names.test.mjs`): Rangfolge in beide Richtungen,
Umbenennung am Telefon, Auflösung über den Alias, Gleichstand bleibt
bei der gefragten Adresse, Geräte-Suffixe.

`tests/test_whatsapp_namen.py` (9 Fälle, `venv/bin/pytest
tests/test_whatsapp_namen.py`): `push` überschreibt `book` nicht und
umgekehrt, eine eigene Nachricht benennt den Chat nicht um, eine
Gruppennachricht benennt die Gruppe nicht um, das `contact`-Ereignis
benennt den LID-Chat und legt keinen an, die Liste lässt die
Karteileichen weg.

## Was ich von Dirk brauche

- Bridge neu bauen und starten (ich kann es nicht). Der Neubau ist
  nötig, weil eine zweite Datei dazugekommen ist (`names.js`, im
  Dockerfile ergänzt):
  `! cd ~/yorikai/yorik-ai && docker compose --profile bundled-whatsapp up -d --build whatsapp-bridge`
- Yorik neu starten, damit Migration 159 läuft und das Backend den
  neuen Ereignistyp kennt.
- Danach einmal **Kontakte → "WhatsApp-Namen nachziehen"** drücken
  (`POST /api/contacts/backfill-whatsapp-names`): das zieht das
  Adressbuch neu und benennt den Bestand um.
- Die Entscheidung zu Teil 8: eine einmalige Abfrage über seine
  bekannten Nummern ist echter Verkehr gegen Meta auf der Live-Session.
  Gedrosselt wie die Avatar-Abfragen, aber es bleibt sein Konto.
- Ob die 509 leeren Chat-Zeilen dauerhaft nur ausgeblendet bleiben
  (mein Vorschlag) oder irgendwann weg sollen.
