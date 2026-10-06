"""The diagnostics collector — the small receiver Yorik's makers run.

Not part of the Yorik image. One table, two routes, no IP addresses:

    POST /v1/reports                    JSON {"report": {...}, "delete_token_sha256": "..."} → 202
    POST /v1/reports/{id}/delete        JSON {"delete_token": "..."} → 204 when the token's sha256 matches
    GET  /v1/stats                      counts only, from ≥ 5 installs

Second layer of scrubbing on arrival with the same regexes Yorik uses;
reports older than 90 days are deleted by `python app.py prune` (cron).
Run behind a web server with the access log off for this host:

    DIAG_DB=postgresql://... uvicorn app:app --host 127.0.0.1 --port 8800

Keep the reports where only you can read them; what you publish is
aggregated (see README).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

DB = os.getenv("DIAG_DB", "postgresql://diag@127.0.0.1/diag")
MAX_BYTES = 256 * 1024
RETENTION_DAYS = int(os.getenv("DIAG_RETENTION_DAYS", "90"))
MIN_INSTALLS = 5

app = FastAPI(title="Yorik diagnostics collector", docs_url=None, redoc_url=None)

# the same shapes Yorik scrubs; here as a second net, values become placeholders
_SCRUB = [
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[email]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b"), "[iban]"),
    (re.compile(r"(?<![\d\w])(?:\+\d{1,3}[\s\-()]?\d[\d\s\-()]{7,}\d|0\d[\d\s\-()]{7,}\d)(?![\d\w])"), "[phone]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
    (re.compile(r"https?://[^\s<>\"']+"), "[url]"),
    (re.compile(r"\b(?:sk|or|pk|rk)-[A-Za-z0-9_-]{16,}\b"), "[secret]"),
]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def scrub(obj: Any) -> Any:
    if isinstance(obj, str):
        for pat, repl in _SCRUB:
            obj = pat.sub(repl, obj)
        return obj
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    return obj


def conn():
    return psycopg.connect(DB)


SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id            UUID PRIMARY KEY,
    install_id    UUID NOT NULL,
    kind          TEXT NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    delete_sha256 TEXT,
    payload       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_kind_received ON reports (kind, received_at DESC);
CREATE INDEX IF NOT EXISTS reports_install ON reports (install_id);
"""


@app.on_event("startup")
def _schema() -> None:
    with conn() as c:
        c.execute(SCHEMA)


@app.post("/v1/reports")
async def receive(request: Request, x_yorik_install: str = Header(default="")) -> Response:
    if not UUID_RE.match(x_yorik_install or ""):
        raise HTTPException(400, "install id")
    raw = await request.body()
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "too large")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "json")
    report = body.get("report") if isinstance(body, dict) else None
    if not isinstance(report, dict) or report.get("kind") not in ("error", "usage_daily"):
        raise HTTPException(400, "report")
    rid = str(report.get("report_id") or "")
    if not UUID_RE.match(rid):
        rid = str(uuid.uuid4())
    clean = scrub(report)
    with conn() as c:
        c.execute("INSERT INTO reports (id, install_id, kind, delete_sha256, payload) VALUES (%s, %s, %s, %s, %s) "
                  "ON CONFLICT (id) DO NOTHING",
                  (rid, x_yorik_install, report["kind"], str(body.get("delete_token_sha256") or "")[:64], json.dumps(clean)))
    return JSONResponse({"ok": True, "id": rid}, status_code=202)


@app.post("/v1/reports/{report_id}/delete")
async def delete(report_id: str, request: Request) -> Response:
    body = await request.json()
    token = str((body or {}).get("delete_token") or "")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with conn() as c:
        cur = c.execute("DELETE FROM reports WHERE id = %s AND delete_sha256 = %s", (report_id, digest))
        n = cur.rowcount
    return Response(status_code=204 if n else 404)


@app.get("/v1/stats")
def stats() -> Dict[str, Any]:
    """Counts only, and only when enough installs contributed."""
    with conn() as c:
        installs = c.execute("SELECT count(DISTINCT install_id) FROM reports").fetchone()[0]
        if installs < MIN_INSTALLS:
            return {"installs": "<5", "note": "aggregates appear from five installations on"}
        by_kind = c.execute("SELECT kind, count(*) FROM reports GROUP BY kind").fetchall()
        triggers = c.execute("SELECT payload->>'trigger', count(*) FROM reports WHERE kind = 'error' GROUP BY 1").fetchall()
        reasons = c.execute("SELECT payload->'feedback'->>'reason', count(*) FROM reports WHERE kind = 'error' GROUP BY 1").fetchall()
    return {"installs": installs, "by_kind": dict(by_kind), "triggers": dict(triggers), "reasons": dict(reasons)}


def prune() -> int:
    with conn() as c:
        cur = c.execute("DELETE FROM reports WHERE received_at < %s",
                        (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS),))
        return cur.rowcount


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "prune":
        print(f"deleted {prune()} report(s) older than {RETENTION_DAYS} days")
    else:
        print(__doc__)
