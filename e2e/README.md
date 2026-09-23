# Test household and crawler

A throwaway Yorik with a made-up family, and a robot that clicks through
every page as each of them. It finds what is visibly broken: pages that
crash, server errors, blank screens, console errors, phone pages wider
than the phone, and buttons a person is shown but not allowed to use.
It does not judge whether an answer is *right* — that is what the
scripted journeys and the tests in `tests/` are for.

```bash
bash e2e/run.sh                 # about 10 minutes; report in e2e/report/SUMMARY.md
bash e2e/run.sh clara phone     # one person, one device
KEEP=1 bash e2e/run.sh          # keep it running: http://127.0.0.1:8177
venv/bin/python e2e/household.py down
```

## The family

| Login | Name | Role |
| --- | --- | --- |
| anna@example.test | Anna | runs the box (platform_admin) |
| ben@example.test | Ben | second parent (member) |
| clara@example.test | Clara | child (restricted) |
| david@example.test | David | child (restricted) |

Password `testhaus-2026`, PINs 1111 … 4444. Seeded through Yorik's own
routes: tasks (own, for the children, a daily one, one overdue), events,
contacts, the demo data, and a small inbox for Anna and Ben.

## Why it cannot touch the real household

- The code runs from a copy under `e2e/.run/app` with its own `data/`
  and its own `config.env`; the real `config.env` is not read.
- Its database is `yorik_e2e`, built from `migrations_pg/` and dropped
  by `down`.
- `e2e/guard/sitecustomize.py` refuses every connection to this machine,
  the home network or the tailnet except the database, the test server,
  the fake model (`e2e/fake_llm.py`) and Gotenberg. Refusals go to
  `e2e/.run/blocked.log`. The server runs on the plain asyncio loop so
  the guard also sees async connections (uvloop would bypass it).
- The crawler's browser may only load pages from the test server, and
  `crawl.mjs` refuses to run against anything but `127.0.0.1`.
- Buttons that log out, delete, send, reset or start a backup are never
  clicked (`SKIP` in `crawl.mjs`).
