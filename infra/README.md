# Yorik infrastructure layout

Yorik bundles its data layer as Docker Compose stacks rooted here. Nothing
under `infra/supabase/` is committed — `scripts/bootstrap-supabase.sh`
fetches it on first start.

## `supabase/` — the bundled Supabase stack

**Source**: `supabase/supabase`, `docker/` directory only, pinned to the
commit in `scripts/bootstrap-supabase.sh` (`SUPABASE_REF`). Bump the pin
deliberately and re-run the test suite; the migrations in `migrations_pg/`
are written against that stack.

**Why self-hosted**: Yorik's stance is "your household data stays on your
hardware". Postgres + pgvector hold everything personal; GoTrue, PostgREST,
Realtime and Storage are the platform layer for Yorik apps, in-house chat
and (later) box-to-box — all on the same machine.

### Ports on the host

| Service                      | Upstream default | Yorik            | Why                                   |
|------------------------------|------------------|------------------|---------------------------------------|
| Kong (Supabase HTTP API)     | 8000             | **8400**         | 8000 is Yorik itself                  |
| Kong HTTPS                   | 8443             | **8453**         |                                       |
| Postgres direct (`supabase-db`) | not published | **127.0.0.1:5435** | the app's psycopg pool connects here |
| Supavisor session pooler     | 5432             | **127.0.0.1:5434** | a local PostgreSQL on 5432 is common |
| Supavisor transaction pooler | 6543             | **127.0.0.1:6544** |                                       |

The host bindings come from `infra/supabase-overlay/docker-compose.yorik.yml`
(copied in by the bootstrap); Kong's ports are stamped into
`infra/supabase/docker/.env` on first install. `POSTGRES_PORT` in that
`.env` stays `5432` — it is also what the stack's own services dial.

### Credentials

`infra/supabase/docker/.env` (`chmod 600`, never committed). The backend
reads `POSTGRES_PASSWORD` from it when `YORIK_DB_PASSWORD` is empty in
`config.env`. `JWT_SECRET`, `ANON_KEY` and `SERVICE_ROLE_KEY` are used by
the GoTrue identity provisioning and the Realtime/PostgREST layer.

### Operations

| Action                        | Command                                                  |
|-------------------------------|----------------------------------------------------------|
| Bring up + apply migrations   | `bash scripts/bootstrap-supabase.sh` (idempotent)        |
| Stop (keeps volumes)          | `cd infra/supabase/docker && docker compose -f docker-compose.yml -f docker-compose.yorik.yml stop` |
| Tear down (DESTROYS volumes)  | `… docker compose -f docker-compose.yml -f docker-compose.yorik.yml down -v` |
| Migrations that need `supabase_admin` (the `docs` schema) | run through the bootstrap, not the app's `postgres` role |

### Disk footprint

Idle stack: ~12 GB of images, ~200 MB of volumes after first start. Grows
with your data and WAL retention.
