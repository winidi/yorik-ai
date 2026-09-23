# Test household, crawler and journeys

A throwaway Yorik with a made-up family, and three testers that use it
the way the family would:

- **Crawler** (`crawl.mjs`): opens all 16 pages as parent, second parent
  and child, on desktop and phone, and clicks every button, tab,
  checkbox and select it may. It reports what is visibly broken: crashes,
  server errors, blank screens, console errors, phone pages wider than
  the phone, and buttons someone is shown but not allowed to use.
- **Journeys** (`journeys.py`): about 60 checks that Yorik does the
  *right* thing. A child sees only her chores, Ben cannot open Anna's
  mail, a daily chore comes back, the chat creates what it says it
  created, an outside agent can read tasks, and so on. Known bugs stay in
  as failing checks until they are fixed.
- **On screen** (`ui_journeys.mjs`): logging in, adding a task, a child
  ticking a chore on her phone, and the chat on a phone, done through the
  real UI.

```bash
bash e2e/run.sh                 # everything, about 15 minutes → e2e/report/README.md
bash e2e/run.sh clara phone     # crawler only, one person, one device
KEEP=1 bash e2e/run.sh          # keep it running afterwards: http://127.0.0.1:8177
FAKE_LLM=1 bash e2e/run.sh      # never use the real model (chat checks are skipped)
venv/bin/python e2e/household.py down
```

Each run is kept under `e2e/history/<date-time>/` (the last 30 runs).
When the real model answers on `127.0.0.1:8080`, the chat checks use it:
inference only, and whatever the chat creates lands in the throwaway
database.

## Every night

On the workstation, a user timer runs `e2e/run.sh` at 02:30:

```bash
systemctl --user status yorik-e2e.timer        # when it runs next
systemctl --user start yorik-e2e.service       # run it now
journalctl --user -u yorik-e2e.service -n 50   # the last run's output
```

The verdict ("all green" or "N problems") can also land in your Yorik
bell. Create a token in Yorik under Settings → Profile → API tokens,
then store it where only you can read it:

```bash
mkdir -p ~/.config/yorik-e2e && install -m 600 /dev/stdin ~/.config/yorik-e2e/token <<< 'yk_…'
```

## The family

| Login | Name | Role |
| --- | --- | --- |
| anna@example.test | Anna | runs the box (platform_admin) |
| ben@example.test | Ben | second parent (member) |
| clara@example.test | Clara | child (restricted) |
| david@example.test | David | child (restricted) |

Password `testhaus-2026`, PINs 1111 … 4444. The family is seeded through
Yorik's own routes: tasks (their own, chores for the children, a daily
one, an overdue one), events, contacts, the demo data, and a small inbox
for Anna and Ben.

## Why it cannot touch the real household

- The code runs from a copy under `e2e/.run/app`, with its own `data/`
  and its own `config.env`. The real `config.env` is never read.
- Its database is `yorik_e2e`, built from `migrations_pg/` and dropped by
  `down`.
- `guard/sitecustomize.py` refuses every connection to this machine, the
  home network or the tailnet, except the database, the test server, the
  model and Gotenberg. Refusals go to `.run/blocked.log`. The server runs
  on the plain asyncio loop so the guard also sees async connections
  (uvloop would bypass it).
- `guard/bin/docker` stands in for `docker` and refuses everything. The
  WhatsApp page would otherwise inspect and restart the real containers.
- The browser only loads pages from the test server, and every script
  refuses to run against anything but `127.0.0.1`.
- Buttons that log out, delete, send, reset or start a backup are never
  clicked (`SKIP` in `crawl.mjs`).
