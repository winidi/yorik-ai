"""Phase E §13 — Realtime sustained-load gate.

Subscribes to public.notifications via the Supabase Realtime
WebSocket, then INSERTs at 10/sec for 5 minutes (3000 rows).
Reports:
  - send vs receive count
  - max inter-receive gap
  - p50 / p99 latency from INSERT commit to event arrival
  - any backlog at the end

Run from the repo root so the .env file is found.

Why not the supabase-py realtime client: not installed, and we
only need a single subscription. WebSocket + Phoenix channel
protocol is ~80 lines.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from pathlib import Path

import psycopg
import websockets


def env(key: str) -> str:
    env_file = Path(__file__).resolve().parent.parent / "infra/supabase/docker/.env"
    for line in env_file.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{key} not in .env")


SERVICE_ROLE = env("SERVICE_ROLE_KEY")
PG_PW = env("POSTGRES_PASSWORD")
REALTIME_WS = "ws://localhost:8400/realtime/v1/websocket"
INSERTS = 3000
RATE_PER_SEC = 10
INTERVAL = 1.0 / RATE_PER_SEC

# Sentinel: every row we INSERT carries a unique payload string we
# can match in the WS event. Avoids cross-contamination from other
# processes writing to notifications during the test window.
RUN_TAG = f"phase-e-stress-{int(time.time())}"


received: dict[int, float] = {}   # notification id -> WS receive time
sent: dict[int, float] = {}       # notification id -> INSERT commit time


async def writer(stop: asyncio.Event) -> None:
    """Insert 3000 notifications, 10/sec, paced."""
    # Need a real user_id for the NOT NULL FK. Pick whichever
    # platform_admin user exists; the test doesn't read these back
    # via the UI so any owner works.
    conn = psycopg.connect(
        f"postgresql://postgres:{PG_PW}@127.0.0.1:5435/postgres",
        autocommit=True,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM user_profiles WHERE role='platform_admin' LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("no platform_admin user — re-seed test users first")
        owner = row[0]

    print(f"[writer] owner={owner} tag={RUN_TAG}")
    next_tick = time.monotonic()
    for i in range(INSERTS):
        await asyncio.sleep(max(0, next_tick - time.monotonic()))
        ts = time.monotonic()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notifications "
                "  (user_id, kind, title, body, payload_json) "
                "VALUES (%s, 'phase_e_stress', %s, %s, %s) RETURNING id",
                (owner, f"stress {i+1}/{INSERTS}", RUN_TAG,
                 json.dumps({"i": i, "run": RUN_TAG})),
            )
            nid = cur.fetchone()[0]
        sent[nid] = ts
        next_tick += INTERVAL
        if (i + 1) % 300 == 0:
            print(f"[writer] sent {i+1}/{INSERTS} (received so far: {len(received)})")
    print(f"[writer] done — {INSERTS} sent")
    # Give the WS up to 10s to drain
    await asyncio.sleep(10)
    stop.set()


async def subscriber(stop: asyncio.Event) -> None:
    """Subscribe to notifications INSERT events via Realtime WS."""
    async with websockets.connect(
        f"{REALTIME_WS}?apikey={SERVICE_ROLE}&vsn=1.0.0",
        ping_interval=20, ping_timeout=20,
        max_size=10 * 1024 * 1024,
    ) as ws:
        # Join topic with INSERT filter on public.notifications.
        # The Phoenix channel format Realtime uses: ref + event name.
        join_msg = {
            "topic": "realtime:public:notifications",
            "event": "phx_join",
            "payload": {
                "config": {
                    "postgres_changes": [
                        {"event": "INSERT", "schema": "public",
                         "table": "notifications"},
                    ],
                },
                "access_token": SERVICE_ROLE,
            },
            "ref": "1",
        }
        await ws.send(json.dumps(join_msg))

        # Heartbeat task — Realtime drops idle connections without one.
        async def heartbeat():
            ref = 2
            while not stop.is_set():
                await asyncio.sleep(15)
                try:
                    await ws.send(json.dumps({
                        "topic": "phoenix", "event": "heartbeat",
                        "payload": {}, "ref": str(ref),
                    }))
                except Exception:
                    return
                ref += 1
        hb = asyncio.create_task(heartbeat())

        print("[sub] joined, waiting for events…")
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                break
            now = time.monotonic()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("event") != "postgres_changes":
                continue
            payload = msg.get("payload") or {}
            # Realtime envelope shape: payload.data.record holds the row
            data = (payload.get("data") or {}).get("record") or {}
            if data.get("body") != RUN_TAG:
                continue
            nid = data.get("id")
            if isinstance(nid, int):
                received[nid] = now
        hb.cancel()


async def main() -> None:
    stop = asyncio.Event()
    await asyncio.gather(writer(stop), subscriber(stop))

    # Report
    matched = [(sent[i], received[i]) for i in received if i in sent]
    print()
    print("=" * 60)
    print(f"sent     : {len(sent)}")
    print(f"received : {len(received)}")
    print(f"matched  : {len(matched)}")
    print(f"missing  : {len(sent) - len(matched)}")

    if matched:
        latencies = [(r - s) * 1000 for s, r in matched]  # ms
        latencies.sort()
        receive_times = sorted([r for _, r in matched])
        gaps = [
            (receive_times[i+1] - receive_times[i]) * 1000
            for i in range(len(receive_times) - 1)
        ]
        print(f"latency  : "
              f"p50={statistics.median(latencies):.1f}ms "
              f"p90={latencies[int(len(latencies)*0.9)]:.1f}ms "
              f"p99={latencies[int(len(latencies)*0.99)]:.1f}ms "
              f"max={max(latencies):.1f}ms")
        if gaps:
            print(f"inter-recv: "
                  f"p50={statistics.median(gaps):.1f}ms "
                  f"p99={sorted(gaps)[int(len(gaps)*0.99)]:.1f}ms "
                  f"max={max(gaps):.1f}ms")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
