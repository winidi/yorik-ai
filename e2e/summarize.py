"""One page for a whole test run: the crawl, the journeys, the on-screen
journeys. Writes e2e/report/README.md and prints a one-line verdict
(the last line of output) for the bell message.

    venv/bin/python e2e/summarize.py
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

REPORT = Path(__file__).resolve().parent / "report"


def load(name: str):
    p = REPORT / name
    return json.loads(p.read_text()) if p.exists() else None


def main() -> None:
    crawl, journeys, ui = load("crawl.json"), load("journeys.json"), load("ui_journeys.json")
    lines = [f"# Yorik test run — {datetime.now():%d.%m.%Y %H:%M}", ""]
    problems = 0

    if crawl is not None:
        findings = [f for r in crawl for f in r["findings"]]
        hard = [f for f in findings if f["kind"] in ("crash", "server", "blank", "overflow", "console")]
        refused = [f for f in findings if f["kind"] == "client"]
        clicks = sum(r["clicks"] for r in crawl)
        problems += len({(f["route"], f["message"]) for f in hard})
        lines += [f"## Crawler — {clicks} clicks, {len(crawl)} person × device runs", "",
                  f"- visibly broken (crash, server error, blank, console, phone overflow): **{len({(f['route'], f['message']) for f in hard})}**",
                  f"- buttons shown to someone who may not use them (refused calls): {len({(f['route'], f['message']) for f in refused})}",
                  "- details: [SUMMARY.md](SUMMARY.md)", ""]
    else:
        lines += ["## Crawler — did not run", ""]

    if journeys is not None:
        bad = [r for r in journeys if r["ok"] is False]
        ok = sum(r["ok"] is True for r in journeys)
        info = sum(r["ok"] is None for r in journeys)
        problems += len(bad)
        lines += [f"## Journeys — {ok} pass, {len(bad)} fail, {info} not testable/skipped", ""]
        lines += [f"- ❌ **{r['area']}**: {r['check']} — {r['detail'][:200]}" for r in bad]
        lines += ["- details: [journeys.md](journeys.md)", ""]
    else:
        lines += ["## Journeys — did not run", ""]

    if ui is not None:
        bad = [r for r in ui if not r["ok"]]
        problems += len(bad)
        lines += [f"## On screen — {len(ui) - len(bad)} of {len(ui)} pass", ""]
        lines += [f"- ❌ {r['name']} ({r['device']}) — {r['detail'][:200]}" for r in bad]
        lines += ["- details: [ui_journeys.md](ui_journeys.md)", ""]
    else:
        lines += ["## On screen — did not run", ""]

    (REPORT / "README.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(("Yorik test run: all green" if problems == 0 else f"Yorik test run: {problems} problem(s)"))


if __name__ == "__main__":
    main()
