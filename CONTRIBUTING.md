# Contributing to Yorik

Thanks for your interest. This document covers everything you need to send a patch.

## Quick start

1. Fork on GitHub, clone your fork
2. Create a branch: `git checkout -b your-feature`
3. Make your change
4. Run tests (they need a throwaway Postgres, see [Local development](#local-development)): `pytest tests/ -q`
5. Build the frontend if you touched it: `cd frontend-react && npm run build`
6. Commit with `-s` (signs the Developer Certificate of Origin): `git commit -s -m "Fix X in Y"`
7. Push your branch, open a PR

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a CLA. By signing your commits with `git commit -s`, you assert that:

- The contribution was created by you (or you have permission to submit it under the project's license)
- You agree to license it under AGPL-3.0-or-later (matching the project)

A signed commit has a `Signed-off-by: Your Name <your@email>` line at the bottom. CI rejects a pull request with an unsigned commit.

We deliberately chose DCO over a Contributor License Agreement because:

- DCO doesn't require re-licensing rights, only authorship attestation
- CLAs deter casual contributors
- We never plan to re-license Yorik away from AGPL, so we don't need the dual-license option that CLAs grant

## What to work on

**Beta priorities** (best path to a merged PR right now):

1. **Bug fixes** for issues with the `bug` label
2. **Translations** — there is no i18n framework yet: the UI text is English with German parts, written inline in the components. Setting one up (and moving the strings) is welcome; open an Issue first so we agree on the approach
3. **Connectors** — new ones in `backend/connectors/`. See [the connector contributor guide](docs/CONNECTORS.md)
4. **Documentation** — especially install guides for non-Ubuntu distros (Fedora, openSUSE, Arch)
5. **Templates** — invoice/letter templates for non-DE/EN countries

**Hold off on** (these need design discussion first — open an Issue before a PR):

- Major UI redesigns
- New top-level apps
- Anything that adds a new external service dependency
- Anything that adds telemetry

If you're not sure, [open an Issue](https://github.com/winidi/yorik-ai/issues/new/choose) before writing code.

## Code style

- **Python**: no formatter or linter is enforced yet; follow the style of the file you edit (4 spaces, type hints where they help). Typed code is welcome.
- **TypeScript**: the project tsconfig (`npx tsc -p tsconfig.app.json --noEmit` must pass). No ESLint or prettier yet.
- **Comments**: only where the *why* is non-obvious. Don't comment what well-named code already says.
- **Tests**: required for new backend endpoints. UI tests are optional but welcome.
- **No new dependencies** without opening an Issue first.

## Pull request checklist

- [ ] Commit messages are signed (`git commit -s`)
- [ ] Tests pass (`pytest tests/ -q`)
- [ ] Frontend builds if touched (`cd frontend-react && npm run build`)
- [ ] No new dependencies (or you opened an Issue and got the go-ahead)
- [ ] Updated CHANGELOG.md under `[Unreleased]`
- [ ] PR description: one paragraph what + why; screenshot if UI

## Local development

```bash
# Backend
cd yorik-ai
python3 -m venv venv && source venv/bin/activate
pip install -r backend/requirements.txt -r requirements-dev.txt

# Tests build a fresh database per test from a Postgres with pgvector
# (the same setup CI uses, see .github/workflows/ci.yml):
docker run -d --name yorik-test-pg -p 5435:5432 \
  -e POSTGRES_USER=supabase_admin -e POSTGRES_PASSWORD=yorik-test -e POSTGRES_DB=postgres \
  pgvector/pgvector:pg16
for r in "postgres LOGIN SUPERUSER PASSWORD 'yorik-test'" "anon NOLOGIN" "authenticated NOLOGIN" \
         "service_role NOLOGIN" "authenticator LOGIN NOINHERIT"; do
  docker exec yorik-test-pg psql -U supabase_admin -d postgres -c "CREATE ROLE $r;"
done
YORIK_DB_HOST=127.0.0.1 YORIK_DB_PORT=5435 YORIK_DB_PASSWORD=yorik-test pytest tests/ -q

uvicorn backend.main:app --reload                # :8000 with hot reload (needs a configured install)

# Frontend (only if you touch React)
cd frontend-react
npm install
npm run dev                                      # :5173 with hot reload
npm run build                                    # writes to dist/, served by backend at /r/*
```

## Releases

How a version is cut, and the rules that keep one-click updates safe
(additive migrations, settings only added): [docs/RELEASING.md](docs/RELEASING.md).

## Reporting bugs

Use the [bug report template](https://github.com/winidi/yorik-ai/issues/new?template=bug.yml). The most useful bug reports include:

- Your OS, Python version, Docker version
- Your LLM (Ollama? llama-swap? cloud?) and model
- Exact reproduction steps
- Backend log: `docker compose logs --tail 100 yorik` (Docker install) or `tail -50 /tmp/homeos-api.log` (classic setup)
- Browser console if it's a UI issue

## Security issues

**Don't open a public Issue** for security bugs. Email [hi@yorik.ai](mailto:hi@yorik.ai) with the details. See [SECURITY.md](SECURITY.md) for our disclosure policy.

## Code of Conduct

This project follows the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md). Unacceptable behavior gets you removed without a second warning.

## License

By contributing you agree your work is licensed under [AGPL-3.0-or-later](LICENSE).

**One exception:** the App SDK (`backend/app_sdk.py` and any module
explicitly marked with the SPDX header `AGPL-3.0-or-later WITH
Yorik-App-SDK-Exception-1.0`) carries an additional linking permission
documented in [LICENSE-EXCEPTION-APP-SDK](LICENSE-EXCEPTION-APP-SDK).
Patches to the SDK are still accepted under AGPL-3.0-or-later — the
exception governs how third-party apps may combine with the SDK, not
how the SDK itself is licensed.

## Building apps on top of Yorik

If you want to write a Yorik app (not contribute to the core), see
the short [App SDK README](backend/APP_SDK_README.md) — hello-world
manifest + connector + the AGPL linking exception. The SDK is
pre-1.0; please open an issue before building anything significant
so we can flag what's about to move.
