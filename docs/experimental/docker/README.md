# Containerised Yorik backend — parked, not supported

These files are the unfinished "Yorik itself in a container" path. They
were moved out of the repo root on 2026-08-24 because, as shipped, they
could not work and their presence made the README lie:

- `docker-compose.yorik.yml` declares `profiles: ["yorik"]`, so the
  documented `docker compose -f docker-compose.yml -f docker-compose.yorik.yml up -d`
  starts nothing.
- `ghcr.io/winidi/yorik-ai:latest` is not publicly pullable.
- The image cannot reach the Supabase Postgres that is bound to the host's
  loopback (`127.0.0.1:5435`), and the Dockerfile omits `migrations_pg/`,
  `briefings/`, `templates/`, `extensions/` and `infra/`.
- The arm64 build under QEMU has to compile torch and does not fit a
  GitHub Actions job.

The supported way to run Yorik is the native path: `bash install.sh`,
which runs `start.sh`, which brings up the bundled services
(Supabase, Immich, Paperless, WhatsApp bridge) with Docker and runs
the backend on the host.

To pick this up again: fix the four points above, move the files back,
restore `docker.workflow.yml` to `.github/workflows/docker.yml`, and
rename `dockerignore` to `.dockerignore`.
