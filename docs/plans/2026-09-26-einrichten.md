# Einrichten, das jeder schafft (2026-09-26)

Ziel: Wer Yorik nicht kennt, kommt ohne Hilfe hinein, und jeder Anschluss
geht ohne Fachwissen. Drei Momente, drei Lösungen:

| Moment | Wer | Lösung |
|---|---|---|
| Box aufsetzen | die technische Person, einmal | Installer ohne Fragen (Phase B), später Image (Phase C) |
| Haushalt einrichten | Admin, im Browser | Checkliste auf Home, kurze Tour, schlanker Assistent (A4) |
| Hineinkommen | jede Person, am Handy | QR-Einladung, PIN statt Passwort (A3) |

Entscheidungen: Fernzugriff nur über Tailscale (kostenloser Personal-Plan,
6 Nutzer), Einladungen per Geräte-Freigabe (nur diese Maschine), öffentliche
Einladungsseite als statische Datei über Tailscale Funnel. Familie meldet
sich mit Gerät + 4-stelliger PIN an. Cloud-KI nur als klar gekennzeichnete
Option, nie vorausgewählt. Texte vorerst Englisch; Übersetzung am Ende.

## Phase A: für die Endnutzer

- **A1 Aufräumen**: Hilfe in der App (`/api/help`, Dock-„?“), tote Verweise
  auf „Settings → Connectors → Paperless“ ersetzt („Reconnect documents“ in
  Einstellungen › System), Anschluss-Fehler in Sätzen, Hilfeseiten passen
  wieder zur App; install.sh: Enter installiert ein Modell statt es zu
  überspringen, NVIDIA-Container-Toolkit wird mitinstalliert.
- **A3 QR-Einladung** (`backend/member_invites.py`, Migration 164):
  Einladungs-Token (einmalig, 24 h, nur als sha256 gespeichert), Link auf
  Yoriks eigene Tailnet-Adresse, optional Tailscale-Geräte-Freigabe über die
  API. `/r/join`: Name, Farbe, PIN, Handy einrichten. Das Handy wird ein
  vertrautes Gerät (Cookie `yorik_device`); die Anmeldung fragt dort nur die
  PIN. Beitritt und PIN-Anmeldung nur aus Heimnetz oder Tailnet.
  `deploy/join-page/index.html` ist die öffentliche Hälfte: keine Daten, alles im
  `#`-Teil der URL, wartet bis Yorik über Tailscale antwortet.
- **A4 Checkliste und Tour** (`backend/setup_checklist.py`, Migration 165):
  Schritte aus echten Daten, pro Rolle; Tour mit Hinweisen an den echten
  Knöpfen, „fertig“ pro Person auf dem Server (das alte Onboarding-Fenster
  kam bei jedem Neuladen wieder); ein Hinweis pro App beim ersten Besuch;
  Assistent nur noch Willkommen + Region.
- **A5 Anschlüsse**: E-Mail in drei Schritten mit Direktlink zum
  App-Passwort, Server nur unter „Advanced“; Kalender-Import mit Anbieter-
  Auswahl und Direktlinks; WhatsApp-Kopplung per Code (am selben Handy);
  „Back up this phone“ für Immich mit kopierbarer Adresse und Anmeldung;
  Sicherung per Laufwerk mit erzeugter Passphrase und Notfallblatt.

## Phase B: der Installer

- **B1** `install.sh` fragt nichts mehr (`--ask` holt die Fragen zurück),
  richtet Tailscale ein (Anmelde-QR im Terminal, HTTPS für Yorik :443,
  Fotos :8443, Dokumente :8444, Einladungsseite :10000 per Funnel) und
  endet mit einem QR-Code fürs Handy. Downloads setzen nach Abbruch fort.
- **B2** README und `docs/INSTALL.md` beschreiben genau diesen einen Weg.
- **B3** `.install-record` merkt sich, was der Installer eingerichtet hat;
  `uninstall.sh` nimmt genau das wieder weg (auch die Supabase-Daten, die
  früher liegen blieben), Tailscale/Docker/Ollama bleiben.
- **B4** Einstellungen › System › Updates: „Neue Version da“ und ein Knopf.
  `yorik-update.service` (per polkit startbar) führt `yorik upgrade` aus
  und startet Yorik neu. Kopien mit lokalen Änderungen werden nie aus der
  App aktualisiert.
- **B5** `scripts/test-fresh-install.sh`: echte Neuinstallation in einer
  frischen Ubuntu-24.04-VM (KVM), 18 Prüfungen bis zum Deinstallieren.
- **B6** Was der VM-Test fand: der Frontend-Fingerabdruck hing von der
  Spracheinstellung ab (jede Neuinstallation startete danach nicht unter
  systemd), `uninstall.sh` ließ die Profil-Container stehen.

## Phase C: Container und fertiges Gerät

- **C1** `install.sh --container`: Yorik selbst läuft aus einem Image
  (`Dockerfile`, `docker-compose.app.yml`, Host-Netzwerk, läuft als der
  Nutzer, startet mit Docker neu); auf dem Rechner kein Python von Yorik.
  Der klassische Weg bleibt Standard.
- **C2** `scripts/build-appliance.sh`: Ubuntu-Server-24.04.5-Stick, der
  die Platte eines Mini-PCs übernimmt, beim ersten Start Yorik einrichtet
  und auf dem Bildschirm erst den Tailscale-Code, dann den QR-Code fürs
  Handy zeigt (`appliance/`).
- **C3** Image baut (2,35 GB); Container findet das DB-Passwort.

## Übernahme und Zurückrollen (für den Admin)

Jede Phase ist ein eigener Branch (`setup/a` → `setup/b` → `setup/c`,
aufeinander aufbauend). Vor jeder Übernahme:

```bash
docker exec supabase-db pg_dumpall -U supabase_admin | gzip > ~/yorik-db-vor-setup-$(date +%F-%H%M).sql.gz
cd ~/yorikai/yorik-ai && git tag -f vor-setup-a   # bzw. vor-setup-b / -c
```

Übernehmen (Beispiel A; für B/C den Branch tauschen):

```bash
cd ~/yorikai/yorik-ai && git merge --ff-only setup/a \
  && (cd frontend-react && npm ci && npm run build) \
  && git add frontend-react/dist && git commit -m "setup A: build" \
  && sudo systemctl restart yorik \
  && docker compose up -d --build whatsapp-bridge
```

Der Neustart wendet die Migrationen 164/165 an (nur neue Tabellen und
eine neue Spalte). Zurück:

```bash
cd ~/yorikai/yorik-ai && git reset --hard vor-setup-a \
  && (cd frontend-react && npm ci && npm run build) && sudo systemctl restart yorik
```

Die neuen Tabellen bleiben dann ungenutzt stehen; es gehen keine Daten
verloren. Der Dump ist die Versicherung für den Fall, dass trotzdem etwas
schiefgeht; zurückgespielt wird er nur in eine leere Datenbank, nicht in
die laufende (Ablauf in `docs/RESTORE.md`, am besten gemeinsam).

## Was der Admin einmal tun muss

1. In der Tailscale-Admin-Konsole Funnel für die Maschine erlauben
   (Access controls, `nodeAttrs` mit `funnel`).
2. `sudo tailscale funnel --bg --https=10000 <repo>/deploy/join-page`
   (Einstellungen › System › Phones zeigt den genauen Befehl).
3. Optional: OAuth-Client mit Schreibrecht auf Geräte anlegen und in
   Einstellungen › System › Phones eintragen. Dann bringt jede Einladung
   ihren eigenen Freigabe-Link mit.

## Offen / nicht verifiziert

- Tailscale-API für Geräte-Einladungen ist gegen die Doku gebaut, nicht mit
  einem echten Schlüssel getestet (Antwortformat wird tolerant gelesen).
- Ob `tailscale serve` die Identitäts-Header auch für freigegebene Nutzer
  setzt, ist ungeprüft.
- Bank mit TAN-Pflicht bleibt ungelöst; Finanzen bleibt eine Opt-in-App.

## Zurückrollen

Jede Phase hat vor der Übernahme ein Tag (`before-setup-a`, …). Die
Migrationen sind additiv (neue Tabellen, eine neue Spalte); ein Zurückrollen
des Codes lässt sie ungenutzt stehen, es gehen keine Daten verloren.
