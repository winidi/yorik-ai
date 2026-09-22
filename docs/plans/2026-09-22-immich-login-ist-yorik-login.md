# Immich-Login = Yorik-Login (Plan)

Entscheidung Dirk, 22.09.2026: „Immich-Login sollte der gleiche sein wie
der Yorik-Login. Ganz einfach.“ Heute ist die Fotos-App ein iframe auf
Immich (Port 8443) mit Immichs eigener Anmeldung; Yorik synchronisiert
nur das Passwort beim Provisionieren. Wer im Browser als anderes
Immich-Konto angemeldet ist, sieht eine fremde Bibliothek (Audit 1.21).

## Weg

Immich kann sich an einen OpenID-Connect-Anbieter hängen (Einstellungen
→ OAuth). Yorik wird dieser Anbieter. Dann:

1. Klick auf „Fotos“ → Immich ohne eigene Login-Seite (`autoLaunch`),
   Umleitung zu Yoriks `/oidc/authorize`.
2. Yorik hat die Session-Cookie → antwortet sofort mit dem Code, kein
   zweites Formular. Ohne Session: Yorik-Login, dann zurück.
3. Immich holt Token + Userinfo, findet das Konto über die E-Mail
   (`autoRegister` aus — die Konten existieren, Yorik legt sie an).
4. Die Immich-App auf dem Handy: `mobileOverrideEnabled` mit
   `mobileRedirectUri`, gleicher Ablauf.

## Bauteile in Yorik

- `backend/oidc.py`: Discovery (`/.well-known/openid-configuration`),
  `/oidc/authorize` (Session-Cookie → Code, sonst Umleitung zu
  `/login?next=`), `/oidc/token` (Code → ID-Token RS256 + Access-Token),
  `/oidc/userinfo` (sub = Yorik-ID, email, name, preferred_username),
  `/oidc/jwks`. Schlüsselpaar beim ersten Start im Credential-Store
  (wie das Voice-Login-Secret). Codes 60 s, einmal, an `client_id` +
  `redirect_uri` gebunden (PKCE, falls Immich es schickt).
- Client-Registrierung: ein Client `immich` mit Secret im Credential-Store;
  `redirect_uri` = `<Immich-URL>/auth/login` und `app.immich:///oauth-callback`.
- Immich-Konfiguration einmalig per Admin-API (`PUT /api/system-config`):
  `oauth.enabled`, `issuerUrl` = Yoriks URL **aus Sicht des
  Immich-Containers** (Docker-Netz: `http://host.docker.internal:8000`
  oder die Tailscale-URL; das Discovery-Dokument muss von dort
  erreichbar sein und die `issuer`-Angabe muss zu der URL passen, die
  der Browser sieht — die Tailscale-URL `https://workstation…:8445`
  erfüllt beides; geprüft am 22.09.: aus dem Immich-Container antwortet
  `https://workstation.tailf0bde1.ts.net:8445/api/health` mit 200,
  `host.docker.internal` ist dort unbekannt, `172.17.0.1:8000` geht auch),
  `clientId`, `clientSecret`, `scope: openid email
  profile`, `buttonText: "Mit Yorik anmelden"`, `autoLaunch: true`,
  `autoRegister: false`, `mobileOverrideEnabled: true`.
- Fotos-App: unverändert iframe; zusätzlich „angemeldet als …“ aus
  `/api/users/me` in der Ecke, bis SSO live ist.
- Passwort-Sync beim Provisionieren bleibt als Rückfall (Handy-App ohne
  OAuth).

## Was ich vom Live-System brauche

Die Immich-Konfiguration kann ich nicht schreiben (Classifier). Ich lege
`scripts/configure_immich_oauth.py` bei, Dirk führt es mit `!` aus,
danach Neustart Yorik.

## Tests

- Discovery-Dokument, JWKS, Authorize ohne Session → 302 auf `/login`,
  mit Session → Code; Token-Tausch, ID-Token verifizierbar mit JWKS,
  Userinfo = Person; Code nur einmal; falsche `redirect_uri` → Fehler.
- Kinder (`restricted`) dürfen sich anmelden (eigene Bibliothek), disabled
  nicht.

## Aufwand

Ein Nachmittag: ~400 Zeilen Backend, ~150 Zeilen Tests, Skript, Doku.
