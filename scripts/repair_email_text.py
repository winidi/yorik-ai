#!/usr/bin/env python3
"""Give mails whose stored text is CSS/HTML their words back, now rather
than at the next start (backend/email_fetcher.py: repair_stored_markup).
The old text is kept in email_body_text_backup_20261005; `--undo` puts it
back. Run from yorik-ai/ with config.env loaded:

    set -a; . ./config.env; set +a
    venv/bin/python scripts/repair_email_text.py          # repair, in batches
    venv/bin/python scripts/repair_email_text.py --undo   # the way back
"""
import sys
import time

sys.path.insert(0, ".")
from backend import email_fetcher as ef  # noqa: E402

if "--undo" in sys.argv:
    print(f"restored {ef.undo_stored_markup_repair()} mail(s) from the backup table")
    sys.exit(0)
t0, total = time.perf_counter(), 0
while True:
    n = ef.repair_stored_markup(batch=200)
    if n == 0:
        break
    total += n
    print(f"  looked at {total} mail(s) …", flush=True)
print(f"done: {total} mail(s) checked in {time.perf_counter() - t0:.0f} s; the search index re-reads the changed ones in the background")
