# Yorik infrastructure layout

Yorik bundles its data-layer dependencies as Docker Compose stacks rooted here. Each subdirectory is either a sparse-cloned upstream project or a Yorik-authored compose file. Nothing in `infra/` ships with the Yorik wheel — these are operator-side prerequisites every install needs running before the FastAPI backend starts.

## `supabase/` — local self-hosted Supabase (Phase D)

**Source**: sparse clone of <https://github.com/supabase/supabase>, only the `docker/` subdir. Not committed (see `.gitignore`); fresh clone instructions below.

### Why local-self-hosted?

Yorik's data-sovereignty stance is "your household data stays on your hardware." That rules out Supabase Cloud as a default. Self-hosting Supabase on the same box as Yorik means Postgres + GoTrue auth + PostgREST + Studio all run alongside the FastAPI service, on the same Tailscale-private network. We get Postgres-grade durability, pgvector for embeddings, and the Studio UI for ad-hoc DB inspection — without sending a single row off the box.

### Port allocation on workstation

| Service          | Default port | Yorik override | Reason                                  |
|------------------|--------------|----------------|-----------------------------------------|
| Kong (HTTP API)  | 8000         | **8400**       | 8000 is the FastAPI/uvicorn port        |
| Studio (via Kong)| 3000         | (8400/Kong)    | reachable via Kong; no direct expose    |
| Postgres direct  | 5432         | **5433**       | 5432 is held by `database-postgres-1`   |
| Analytics        | 4000         | 4000           | free                                    |
| SMTP             | 2500         | 2500           | free                                    |
| Pooler (Supavisor)| 6543        | 6543           | free                                    |

The Yorik backend will connect to Postgres at `localhost:5433` with the credentials in `infra/supabase/docker/.env`.

### Fresh-install runbook

```sh
cd infra
git clone --depth 1 --filter=blob:none --sparse https://github.com/supabase/supabase.git
cd supabase
git sparse-checkout set docker
cd docker
cp .env.example .env
chmod 600 .env
sh utils/generate-keys.sh --update-env

# Apply the Yorik port overrides + COMPOSE_FILE override:
sed -i 's/^POSTGRES_PORT=.*/POSTGRES_PORT=5432/' .env   # internal listen — leave 5432
sed -i 's/^KONG_HTTP_PORT=.*/KONG_HTTP_PORT=8400/' .env  # 8000 is uvicorn
sed -i 's/^KONG_HTTPS_PORT=.*/KONG_HTTPS_PORT=8453/' .env # 8443 may also conflict
sed -i 's|^SUPABASE_PUBLIC_URL=.*|SUPABASE_PUBLIC_URL=http://localhost:8400|' .env
sed -i 's|^API_EXTERNAL_URL=.*|API_EXTERNAL_URL=http://localhost:8400|' .env
sed -i 's|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml:docker-compose.yorik.yml|' .env

# Write the Yorik DB-exposure override:
cat > docker-compose.yorik.yml <<'EOF'
services:
  db:
    ports:
      - "127.0.0.1:5435:5432"
EOF

docker compose up -d
```

The override expose-on-5435 step is what lets Yorik's psycopg pool talk directly to Postgres without going through Supavisor's tenant-aware pooler. Without it, only Kong (REST) on 8400 reaches the DB.

### Operations

| Action                           | Command                                |
|----------------------------------|----------------------------------------|
| Start                            | `cd infra/supabase/docker && docker compose up -d`     |
| Stop (preserves volumes)         | `cd infra/supabase/docker && docker compose stop`      |
| Tear down (DESTROYS volumes!)    | `cd infra/supabase/docker && docker compose down -v`   |
| Tail logs                        | `sh run.sh logs`                       |
| Reset to factory                 | `sh reset.sh` (deletes volumes!)       |

### Credentials

All secrets live in `infra/supabase/docker/.env` — `chmod 600`, never committed. Bring them into the Yorik backend via the systemd unit's `EnvironmentFile=` directive. The keys Yorik actually consumes:

- `POSTGRES_PASSWORD` — used by Yorik's `YORIK_DB_URL=postgres://supabase_admin:$POSTGRES_PASSWORD@localhost:5433/postgres`
- `JWT_SECRET` — only relevant if/when Yorik adopts Supabase Auth (Section 5 of Phase D plan; not active yet)
- `SERVICE_ROLE_KEY` — admin-equivalent for Supabase REST APIs; only used by migration / backfill scripts that need to bypass RLS

### Disk footprint

Idle Supabase: ~2 GB Docker images, ~200 MB volumes after first start. Grows with your data + WAL retention. The dev box should have **at least 5 GB free** before first start; healthy household usage tops out around 10 GB after a few months of Paperless + Immich metadata accumulation.
