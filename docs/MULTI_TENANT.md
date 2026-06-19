# Multi-tenant Yorik on Hetzner — operator runbook

Living document. Treat each checkbox as a stop sign — don't move on
until that line passes. When something fails, dig into it before
continuing; later steps build on earlier guarantees.

The target box is a Hetzner CX43 at `91.99.103.65`. Replace
`yourdomain.com` everywhere with whatever you actually own; replace
`isee` with the Hetzner OS user you create.

---

## 0. Pre-launch on the workstation (~30 min)

Before touching Hetzner. The point is to leave the workstation in a
known-good state and verify the bundle you'll deploy actually works.

- [ ] All session work committed and pushed to `winidi/yorik-ai-private`
  ```bash
  cd /home/isee/yorikai/yorik-ai
  git status   # clean
  git log --oneline origin/main..HEAD   # empty
  ```
- [ ] Workstation `/api/health` returns `ok` with `llm_reachable: true`
  and `immich_reachable: true`
  ```bash
  curl -s http://127.0.0.1:8000/api/health | jq
  ```
- [ ] Run one backup+restore drill on workstation (tenant-aware)
  - [ ] Create a throwaway tenant `drill`
  - [ ] Add a distinguishable marker (a chat message)
  - [ ] Trigger a backup via Settings → Backup or `start.sh`
  - [ ] Verify the snapshot contains `tenants/drill/`
  - [ ] Drop the tenant
  - [ ] `bash scripts/restore.sh <snapshot>`
  - [ ] Confirm marker is back
- [ ] Verify Paperless admin token configured in workstation
  Settings → Connectors → Paperless (currently noted as missing —
  fix on Hetzner instead if you haven't done it here)
- [ ] Note workstation versions (so Hetzner can match)
  ```bash
  python3 --version; node --version; docker --version
  ```

---

## 1. Hetzner provisioning (~1 hr including DNS prop)

### Step 0 — operator user (fresh Hetzner is root-only)

The default Hetzner Ubuntu 24.04 image only opens SSH for `root` and
ships with no other accounts. Yorik refuses to run as root by design.
Do this first; everything below assumes you log in as the `yorik`
user.

- [ ] SSH in as root
  ```bash
  ssh root@<your-ip>
  ```
- [ ] Create the operator user + sudo + SSH key
  ```bash
  useradd -m -s /bin/bash yorik
  usermod -aG sudo yorik
  echo "yorik ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/yorik
  chmod 440 /etc/sudoers.d/yorik
  mkdir -p /home/yorik/.ssh
  cp /root/.ssh/authorized_keys /home/yorik/.ssh/authorized_keys
  chown -R yorik:yorik /home/yorik/.ssh
  chmod 700 /home/yorik/.ssh
  chmod 600 /home/yorik/.ssh/authorized_keys
  ```
- [ ] Test from your workstation
  ```bash
  ssh yorik@<your-ip>   # should land you on the box without password
  sudo -n whoami        # should print 'root'
  ```
- [ ] Disable root SSH (later, after the install is solid — leave on
  for now so you have a recovery path)

### Step 1 — rest of provisioning

- [ ] CX43 instance running, you have SSH access as `yorik`
  ```bash
  ssh yorik@91.99.103.65
  ```
- [ ] Non-root user with sudo (verified above)
  ```bash
  groups   # includes sudo
  ```
- [ ] DNS records pointing at the box
  - [ ] `A     yourdomain.com           91.99.103.65`
  - [ ] `A     *.yourdomain.com         91.99.103.65`
  - [ ] `AAAA  yourdomain.com           2a01:4f8:1c18:1e19::1`  (use your /64)
  - [ ] `AAAA  *.yourdomain.com         2a01:4f8:1c18:1e19::1`
- [ ] DNS propagated
  ```bash
  dig +short yourdomain.com
  dig +short any.yourdomain.com   # any subdomain — wildcard
  ```
- [ ] Firewall: ports 80, 443, 22 open inbound; everything else closed
- [ ] Docker installed and the user in the docker group
  ```bash
  docker run --rm hello-world
  ```
- [ ] Time sync: `chronyc tracking` shows last offset under 100ms

---

## 2. Initial install (~30 min)

- [ ] Clone repo
  ```bash
  cd ~
  git clone https://github.com/winidi/yorik-ai-private.git yorikai/yorik-ai
  cd yorikai/yorik-ai
  ```
- [ ] Pre-edit `config.env` (these matter at first start)
  ```bash
  # Critical for multi-tenant:
  YORIK_DB_BACKEND=postgres
  YORIK_TENANT_ROOT=yourdomain.com
  # Recommended:
  YORIK_TRUSTED_ORIGINS=https://yourdomain.com
  ```
- [ ] Run installer
  ```bash
  bash install.sh
  ```
  Watch for:
  - [ ] supabase-db boots clean
  - [ ] Migrations apply (you'll see `applying 100_..., 101_..., etc.`)
  - [ ] Frontend dist fingerprint matches src
- [ ] First `bash start.sh` exits 0 and binds 8000
- [ ] `curl -s http://127.0.0.1:8000/api/health | jq` returns ok
- [ ] `ls -l data/internal_token` exists, mode `-rw-------`

---

## 3. Reverse proxy (~20 min)

- [ ] Install Caddy
  ```bash
  sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt update
  sudo apt install -y caddy
  ```
- [ ] Write `/etc/caddy/Caddyfile`
  ```caddy
  # Host Yorik on the apex
  yourdomain.com {
      reverse_proxy 127.0.0.1:8000
  }

  # Per-tenant subdomains — picked up automatically as you create
  # tenants via Settings → Households.
  import /home/isee/yorikai/yorik-ai/infra/caddy/tenants/*.caddy
  ```
- [ ] Reload Caddy
  ```bash
  sudo systemctl reload caddy
  sudo systemctl status caddy   # active (running)
  ```
- [ ] Hit the host via the domain
  ```bash
  curl -s https://yourdomain.com/api/health | jq
  ```
  Caddy auto-fetched the cert. If this fails, check `journalctl -u caddy`.

---

## 4. systemd templates (~15 min)

- [ ] Edit `infra/systemd/yorik-tenant@.service` and replace
  `User=isee` / `Group=isee` / `/home/isee/yorikai/yorik-ai` with
  your Hetzner user + path
  ```bash
  sed -i.bak 's|isee|<your-user>|g; s|/home/isee/yorikai/yorik-ai|/home/<your-user>/yorikai/yorik-ai|g' \
    infra/systemd/yorik-tenant@.service
  diff -u infra/systemd/yorik-tenant@.service.bak infra/systemd/yorik-tenant@.service
  ```
- [ ] Install the template
  ```bash
  sudo cp infra/systemd/yorik-tenant@.service /etc/systemd/system/
  sudo systemctl daemon-reload
  ```
- [ ] Verify it's recognised
  ```bash
  systemctl cat yorik-tenant@probe.service | head
  ```
- [ ] Check the host `yorik.service` exists and is enabled
  ```bash
  systemctl status yorik.service
  ```

---

## 5. Host admin (~10 min)

- [ ] Open `https://yourdomain.com/` in your normal browser
- [ ] Setup wizard appears
- [ ] Create admin account (REAL email, strong password — this is YOUR
  account, not a test one)
- [ ] Login works
- [ ] `/api/auth/me` returns your user with role `admin` or `platform_admin`

---

## 6. External services (~20 min)

These unlock photos and documents for every tenant.

- [ ] Generate Immich admin API key
  - [ ] Open `http://localhost:2283/` (or wherever Immich binds)
    while SSH-tunneled (`ssh -L 2283:127.0.0.1:2283 isee@host`)
  - [ ] First-run admin account → set strong password
  - [ ] Account Settings → API Keys → "New" → name "Yorik host admin"
    → permissions "all"
  - [ ] Copy the secret
- [ ] Paste in Yorik
  - [ ] Settings → Connectors → Immich
  - [ ] base_url `http://127.0.0.1:2283`
  - [ ] api_key (paste)
- [ ] Same for Paperless
  - [ ] `http://localhost:8010/` → admin / generated password
  - [ ] Profile → Auth Tokens → create
  - [ ] Settings → Connectors → Paperless paste base_url + api_key
- [ ] Both `Settings → Connectors → credentialed` show both green

---

## 7. Backup (~15 min)

- [ ] Settings → Backup
- [ ] Set encryption passphrase (write it down somewhere SAFE that is
  NOT this box — the passphrase is the only key)
- [ ] Set target — default `data/backups/` is OK for now; later
  mount external storage and switch
- [ ] Run one manual backup
- [ ] Verify the file lands
  ```bash
  ls -lh data/backups/
  ```
- [ ] Smoke-test decrypt
  ```bash
  ls -lh data/backups/*.age | tail -1
  ./venv/bin/python -c "
  from pyrage import passphrase
  from pathlib import Path
  raw = Path('data/backups/<your-file>.age').read_bytes()
  out = passphrase.decrypt(raw, '<your-passphrase>')
  print('decrypted bytes:', len(out))
  "
  ```

---

## 8. First tenant — dogfood as yourself (~15 min)

Don't invite anyone else until you've been a tenant for at least a day.

- [ ] Settings → Households → "Add household"
- [ ] Slug `test`, display label "Test Tenant"
- [ ] Get the invite URL — `https://test.yourdomain.com/?invite=...`
- [ ] On the box, enable + start the systemd unit
  ```bash
  sudo systemctl enable --now yorik-tenant@test.service
  ```
- [ ] Reload Caddy so it picks up the new snippet
  ```bash
  sudo systemctl reload caddy
  ```
- [ ] Verify the tenant is up
  ```bash
  sudo systemctl status yorik-tenant@test
  journalctl -u yorik-tenant@test -n 20
  curl -s https://test.yourdomain.com/api/health | jq
  ```
- [ ] Open invite URL in an INCOGNITO browser window
- [ ] Orange "Set up your household" screen
- [ ] Set password — different from host's
- [ ] Login lands on the tenant Yorik
- [ ] Tenant's `/api/auth/me` shows role `platform_admin` for this DB only
- [ ] Host can no longer see this tenant's data (different DB)

---

## 9. Verification tests

### 9.1 Health probes

- [ ] Host
  ```bash
  curl -s https://yourdomain.com/api/health | jq
  ```
  - [ ] `status: ok`
  - [ ] `llm_reachable: true`
  - [ ] `immich_reachable: true`
  - [ ] `model` is the Qwen 3.5 9B identifier you expect
- [ ] Tenant
  ```bash
  curl -s https://test.yourdomain.com/api/health | jq
  ```
  - [ ] Same shape as host
- [ ] All systemd units active
  ```bash
  systemctl status yorik.service
  systemctl status 'yorik-tenant@*'
  ```

### 9.2 LLM call tests

Probe the LLM end-to-end without going through the chat. These should
all complete in under 30s on a warm LLM; longer means the model isn't
loaded yet.

- [ ] Quick math (host)
  ```bash
  curl -s -X POST -H 'content-type: application/json' \
    -b /tmp/host.cookie -c /tmp/host.cookie \
    -d '{"email":"<your-email>","password":"<your-pw>"}' \
    https://yourdomain.com/api/auth/login
  curl -s -X POST -H 'content-type: application/json' \
    -b /tmp/host.cookie \
    -d '{"message":"What is 17 times 23? One sentence."}' \
    https://yourdomain.com/api/ask | jq .response
  ```
  - [ ] Returns "391" (the number, possibly in a sentence)
- [ ] Multilingual (German)
  ```bash
  curl -s -X POST -H 'content-type: application/json' \
    -b /tmp/host.cookie \
    -d '{"message":"Übersetze ins Englische: Guten Morgen, wie geht es dir?"}' \
    https://yourdomain.com/api/ask | jq .response
  ```
  - [ ] English translation present
- [ ] Context recall in a single turn (long context)
  ```bash
  curl -s -X POST -H 'content-type: application/json' \
    -b /tmp/host.cookie \
    -d '{"message":"Remember the word PINEAPPLE. Now describe the colour of the sky in two sentences. Then tell me the word you were asked to remember."}' \
    https://yourdomain.com/api/ask | jq .response
  ```
  - [ ] Response includes PINEAPPLE (or pineapple) at the end
- [ ] Vision: upload a photo containing readable text and ask Yorik to
  describe it. Use Settings → Connectors → Immich → upload, then chat:
  "Describe the photo I just uploaded."
  - [ ] Response references actual content of the photo
- [ ] Document parsing: drop a PDF into Compose
  ("Upload doc" in any slot)
  - [ ] Field extraction completes without 30s hang
  - [ ] If LLM is OFF: returns 503 immediately (no 30s wait)

### 9.3 End-to-end multi-turn chat — Admin user

Run on the **host** Yorik (as the maintainer you). All three turns in a
single conversation (don't open a new chat between turns).

- [ ] **Turn 1**: "What's on my calendar this week?"
  - [ ] Returns a list (even if empty)
  - [ ] No errors, no stack trace
- [ ] **Turn 2**: "Add a haircut next Tuesday at 3 PM."
  - [ ] Confirms or asks for confirmation
  - [ ] If confirm-mutations is ON, click confirm
  - [ ] Event appears via `curl https://yourdomain.com/api/calendar/today` or via UI
- [ ] **Turn 3**: "Move it to Wednesday at 4 PM."
  - [ ] LLM remembered which event (the haircut) — didn't ask
  - [ ] Event time updates
- [ ] **Turn 4**: "Cancel it."
  - [ ] Event deletes (or marks cancelled)
- [ ] **Turn 5**: "What's on my calendar this week now?"
  - [ ] No haircut listed
- [ ] **Turn 6**: "Show me my last five chat conversations."
  - [ ] Lists actual conversations (this verifies the conversations
    skill is wired correctly post-Phase-E)

### 9.4 End-to-end multi-turn — Household member

Create a non-admin user on the test tenant first.

- [ ] On `test.yourdomain.com` log in as the tenant admin
- [ ] Settings → Users → Add user
  - [ ] Name "Member", email `member@test.local`, role `member`
  - [ ] Set password
- [ ] Log out, log in as Member (incognito window)
- [ ] **Turn 1**: "What's on the household calendar today?"
  - [ ] Lists shared events (if any), no error
- [ ] **Turn 2**: "Add buy milk to my task list."
  - [ ] Task created on Member's own list
  - [ ] Verify via Settings → Users (admin only — skip if Member can't see)
- [ ] **Turn 3**: "Mark the first task done."
  - [ ] Task toggles
- [ ] **Turn 4**: "Show me my photos from last weekend."
  - [ ] Returns Member's own Immich photos (likely empty for a fresh user)
  - [ ] Does NOT return another user's photos
- [ ] **Turn 5**: "Add an event to the household calendar."
  - [ ] If household member is read-only on household cal: politely
    refused with a useful message
  - [ ] If household member has write: event added; visible to other
    members via shared cal

### 9.5 End-to-end multi-turn — Child / restricted

- [ ] Create user role `restricted`
  - [ ] Name "Child", email `child@test.local`
- [ ] Log in as Child
- [ ] **Turn 1**: "What's on the calendar today?"
  - [ ] Reads shared cal, returns events
- [ ] **Turn 2**: "Add an event called 'play time' for this afternoon."
  - [ ] Adds to Child's PERSONAL calendar OR refuses based on
    workspace policy
  - [ ] If refused, refusal is graceful and explains why
- [ ] **Turn 3**: "Show me the household chat history."
  - [ ] Either returns Child's own conversations, or refuses with a
    sensible reason
  - [ ] Does NOT return another user's conversations
- [ ] **Turn 4**: "Delete the calendar event I just added."
  - [ ] Works on Child's own event
  - [ ] If asked to delete a shared event: refused
- [ ] **Turn 5**: "Show me Mom's photos from last week."
  - [ ] Refuses or returns only photos Child has been explicitly
    shared on
  - [ ] Importantly: NO 500 errors; the refusal is intentional

### 9.6 Permission boundary tests (negative — these MUST fail)

- [ ] Member calls `/api/users` (admin-only) → 403
  ```bash
  curl -s -o /dev/null -w "%{http_code}\n" -b /tmp/member.cookie \
    https://test.yourdomain.com/api/users
  ```
- [ ] Restricted user calls `/api/tenants` (host-only) → 400 ("itself a tenant")
- [ ] Cross-tenant from test to host's admin endpoints → 401 (no
  cookie) or 403 (with stolen cookie from wrong domain — should be
  rejected by cookie scope)
- [ ] Tenant Yorik directly calls `/api/internal/provision` with a
  DIFFERENT tenant's name in the body → 403 "bearer is bound to
  tenant '<self>'"
  ```bash
  TEST_TOK=$(cat data/tenants/test/internal_token)
  curl -s -X POST -H "Authorization: Bearer $TEST_TOK" \
    -H 'content-type: application/json' \
    -d '{"tenant_name":"different","service":"immich","yorik_user_id":"x","name":"x","email":"x@x.com","password":"xxxxxxxx"}' \
    http://127.0.0.1:8000/api/internal/provision
  ```

---

## 10. Backup + restore round-trip (~30 min)

- [ ] Make a distinguishable change on the tenant — a chat message
  saying "RESTORE PROOF <today's-timestamp>"
- [ ] Trigger a backup via Settings → Backup → Run now
- [ ] Confirm the bundle contains the tenant
  ```bash
  ls -lh data/backups/ | tail -1
  ./venv/bin/python -c "
  from pyrage import passphrase
  from pathlib import Path
  import io, tarfile
  raw = Path('data/backups/<file>.age').read_bytes()
  out = passphrase.decrypt(raw, '<pass>')
  with tarfile.open(fileobj=io.BytesIO(out)) as t:
      for m in t.getmembers():
          if 'tenants/' in m.name: print(m.name)
  "
  ```
- [ ] Drop the test tenant: `bash scripts/drop-tenant.sh test`
- [ ] Confirm it's gone (DB + Caddy snippet + manifest)
- [ ] Run restore: `bash scripts/restore.sh data/backups/<file>.age`
- [ ] Confirm tenant comes back
  ```bash
  ls data/tenants/test/
  sudo systemctl restart yorik-tenant@test
  sudo systemctl reload caddy
  curl -s https://test.yourdomain.com/api/health
  ```
- [ ] Log back in as the tenant admin (same credentials)
- [ ] Find your "RESTORE PROOF ..." message in chat history

---

## 11. Inviting family

DO NOT do this before the dogfood phase passes. You want to find
bugs while you're the only one affected.

For each family member:

- [ ] Settings → Households → Add household — slug = a memorable
  name (`mom`, `dad`, `alex`), display label = their first name
- [ ] Generate invite, copy the URL
- [ ] `sudo systemctl enable --now yorik-tenant@<name>.service`
- [ ] `sudo systemctl reload caddy`
- [ ] Send the link via a SECURE channel (Signal, in-person, encrypted
  email). NOT plain SMS or unencrypted email — the link is the
  capability.
- [ ] They open it, set their password, you're done

After they're in:

- [ ] Walk them through chat, calendar, photos, docs
- [ ] Show them how to add a family member of their own (if
  applicable)
- [ ] Tell them: "I (host) can technically see your data because I run
  this box. I won't, and it would be an obvious audit-log event if I
  did. If you want full privacy, you need to run your own box."

---

## 12. Operational reference (bookmark this section)

| Action | Command |
|---|---|
| Restart host | `sudo systemctl restart yorik` |
| Restart one tenant | `sudo systemctl restart yorik-tenant@<name>` |
| Restart all tenants | `sudo systemctl restart 'yorik-tenant@*'` |
| Tail one tenant's logs | `journalctl -u yorik-tenant@<name> -f` |
| Tail host logs | `journalctl -u yorik -f` |
| List tenants | `ls -d data/tenants/*/` or `curl ...api/tenants` |
| Create tenant from CLI | `bash scripts/create-tenant.sh <name>` |
| Drop tenant from CLI | `bash scripts/drop-tenant.sh <name>` |
| Issue reset link | UI: Settings → Households → Reset; or `POST /api/tenants/<name>/issue-reset-invite` |
| Apply new migrations to tenants | `bash scripts/migrate-tenants.sh` |
| Run backup now | UI: Settings → Backup → Run, or `./venv/bin/python -c "from backend import backup; backup._run_backup_sync()"` |
| Restore from snapshot | `bash scripts/restore.sh <path-to-.age>` |
| Check Caddy config | `sudo caddy validate --config /etc/caddy/Caddyfile` |
| Reload Caddy | `sudo systemctl reload caddy` |
| Pull new Yorik release | `cd ~/yorikai/yorik-ai && git pull && bash scripts/migrate-tenants.sh && sudo systemctl restart yorik 'yorik-tenant@*'` |

---

## 13. Known remaining issues (from the launch-readiness audit)

If you hit one of these in production, you'll recognise it. None
ship-block but each is a paper cut.

- [ ] `User=isee` may still appear in `/etc/systemd/system/yorik-tenant@.service`
  if you forgot the sed in step 4 — fix in place + `daemon-reload`
- [ ] `MemoryMax=1G` per tenant — tune up if you see OOM kills in
  `journalctl -k`
- [ ] Invalid / expired invite shows a generic error toast — user can
  see "invite token not found"; relay them a fresh link
- [ ] HouseholdsTab "this Yorik is itself a tenant" panel string-matches
  the error wording — if backend changes, message degrades silently
- [ ] PhotosApp says "Immich down" on any `/api/health` failure
  (incorrect cause attribution when it's a different upstream)
- [ ] BriefingApp dumps raw JSON for unknown render types — should
  show "(unsupported card)"
- [ ] Tasks PATCH not yet wired — the checkbox in Briefing is
  read-only display
- [ ] `.ics` calendar import not wired — UI rejects with a toast
- [ ] `pg_dump` schema-public sed-strip pattern may need an update
  when supabase-db ticks past Postgres 15

---

## 14. Things to do later (not blocking)

- [ ] Write a one-page "How to use your Yorik" tenant-facing doc
  (calendar, chat, photos, docs)
- [ ] Tenant monitoring — periodic ping per tenant, surface in
  Households UI ("Mom's Yorik: up · last seen 2 min ago")
- [ ] Per-tenant data export endpoint (GDPR Article 20 ergonomics)
- [ ] Per-tenant Immich library carve-out for defense-in-depth
- [ ] "Send invite via email" button on Households tab
- [ ] First-class tenant suspension flow for non-payment / abuse
- [ ] Audit log for host-admin access to tenant data (for the trust
  conversation with your family)
- [ ] Document the trust model on a `docs/SECURITY.md` page

---

## 15. If you have to roll back

You ran `git pull`, things broke, you want to undo.

- [ ] Note the commit hash you were on before pull:
  `git reflog | head -5`
- [ ] Stop everything:
  `sudo systemctl stop 'yorik-tenant@*' yorik`
- [ ] Hard-reset:
  `git reset --hard <previous-commit>`
- [ ] Re-apply tenant schema if a migration ran:
  - If migrations went FORWARD only: tenants still work, just on the
    new schema. No action.
  - If migrations are NOT backwards-compatible: restore from latest
    backup (`bash scripts/restore.sh ...`).
- [ ] Restart:
  `sudo systemctl restart yorik 'yorik-tenant@*'`
- [ ] Verify the test tenant still logs in.

If migrations have run forward and the rollback can't apply them,
restore is your only path — that's why step 7's backup matters.

---

## 16. When in doubt

The audit in commit a19a9b7's message has the punch list. The
launch-readiness report is in this session's history; if you lost
it, regenerate by reading `git log --oneline -15` and the commit
bodies between 5ef3a95 (Phase F-lite skeleton) and HEAD.
