"""One-off repair, 2026-09-22: the default-owner workflow had taken the
four existing documents away from the people who uploaded them. Repairs
the workflow trigger (the service does the same at start) and gives the
documents back: 1 and 2 to Dirk (Paperless user 8), 3 and 4 to Beate (11).

Run from the repo root as the service user:
    set -a; . ./config.env; set +a
    PYTHONPATH=. venv/bin/python scripts/fix_paperless_owners_2026-09-22.py
"""
import logging

import requests

logging.basicConfig(level=logging.INFO, format="%(message)s")
from backend import paperless_visibility as pv  # noqa: E402

OWNERS = {1: 8, 2: 8, 3: 11, 4: 11}

s = pv._settings()
base, headers = s["base_url"], pv._admin_headers()
wid = pv._ensure_default_owner_workflow(base, headers, pv._first_superuser_id(base, headers))
w = requests.get(f"{base}/api/workflows/{wid}/", headers=headers, timeout=20).json()
print("workflow", wid, "triggers now:", [(t["type"], t["sources"]) for t in w["triggers"]])
for doc, owner in OWNERS.items():
    r = requests.patch(f"{base}/api/documents/{doc}/", headers={**headers, "Content-Type": "application/json"},
                       json={"owner": owner}, timeout=20)
    print("doc", doc, "owner ->", owner, r.status_code)
for d in requests.get(f"{base}/api/documents/?page_size=10", headers=headers, timeout=20).json()["results"]:
    print("  ", d["id"], d["title"][:40], "owner=", d["owner"])
