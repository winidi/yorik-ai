"""One error report → one case for the search test set.

    DIAG_DB=postgresql://diag@127.0.0.1/diag python to_case.py <report-id>

Prints JSON in the shape of ~/yorikai/search-eval/cases.json; `want`
stays empty until a fixture with the report's shape exists.
"""

from __future__ import annotations

import json
import os
import sys

import psycopg


def to_case(report: dict) -> dict:
    q = (report.get("question") or {}).get("text") or ""
    fb = report.get("feedback") or {}
    facts = report.get("facts") or []
    tags = [t for t in (report.get("trigger"), fb.get("reason")) if t and t != "none"]
    note = "; ".join(
        f"{f.get('token')} {'exists' if f.get('exists') else 'absent'}, {'indexed' if f.get('indexed') else 'not indexed'}, "
        f"rank {f.get('search_rank', 0)}" for f in facts)
    tools = ", ".join(t.get("name", "") for t in report.get("trace") or [])
    return {"id": f"report-{str(report.get('report_id', ''))[:8]}", "q": q, "want": [], "tags": tags,
            "note": f"facts: {note or '-'} | tools: {tools or '-'} | answer said nothing found: "
                    f"{(report.get('answer') or {}).get('said_nothing_found')} | note: {fb.get('note') or ''}"}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with psycopg.connect(os.getenv("DIAG_DB", "postgresql://diag@127.0.0.1/diag")) as c:
        row = c.execute("SELECT payload FROM reports WHERE id = %s AND kind = 'error'", (sys.argv[1],)).fetchone()
    if not row:
        sys.exit("no such error report")
    print(json.dumps(to_case(row[0]), ensure_ascii=False, indent=1))
