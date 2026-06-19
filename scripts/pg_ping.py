#!/usr/bin/env python3
"""Smoke test the Yorik Postgres pool.

Run from the project root:
    YORIK_DB_PASSWORD=... ./venv/bin/python3 scripts/pg_ping.py

Or with a full URL:
    YORIK_DB_URL=postgres://... ./venv/bin/python3 scripts/pg_ping.py

Exits 0 if the pool opens + a SELECT 1 + a pgvector probe succeed. Used
by Phase D smoke gates before more invasive backend work.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `from backend.database_pg import …` when running directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.database_pg import conn_ctx_pg, close_all_pools


def main() -> int:
    # Auto-pick up password from infra/supabase/docker/.env if the
    # env var isn't set — saves one bash sourcing step.
    if not os.getenv("YORIK_DB_PASSWORD") and not os.getenv("YORIK_DB_URL"):
        env_path = Path(__file__).resolve().parent.parent / "infra/supabase/docker/.env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("POSTGRES_PASSWORD="):
                    os.environ["YORIK_DB_PASSWORD"] = line.split("=", 1)[1]
                    break

    try:
        with conn_ctx_pg("main") as c:
            row = c.execute("SELECT current_user AS who, version() AS v").fetchone()
            print(f"  user      : {row['who']}")
            print(f"  server    : {row['v'].split(' on ')[0]}")

            row = c.execute(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('vector', 'pgcrypto') ORDER BY extname"
            ).fetchall()
            for r in row:
                print(f"  {r['extname']:10}: {r['extversion']}")

            # Functional probe: round-trip an embedding.
            c.execute("CREATE TEMP TABLE _ping_vec (id int, v vector(3))")
            c.execute("INSERT INTO _ping_vec VALUES (1, %s)", ([0.1, 0.2, 0.3],))
            got = c.execute("SELECT v FROM _ping_vec").fetchone()
            print(f"  vec rt    : {got['v']}")

        print("OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        close_all_pools()


if __name__ == "__main__":
    sys.exit(main())
