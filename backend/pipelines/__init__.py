"""Pipelines — Yorik follows a matter until it is done.

See docs/plans/2026-09-25-pipelines.md. The engine (engine.py) is
general; kinds (kinds/), sources (sources/) plug into it. Stage 1:
the kind `nachfassen` with mail as its source, accompanied mode only.
"""

from __future__ import annotations

import logging

log = logging.getLogger("yorik.pipelines")


def poke(owner_user_id) -> None:
    """Something new arrived for this person (a mail, later a document,
    a message, a booking): look at their waiting pipelines on the next
    tick instead of at the next planned time. Cheap, never raises —
    called from the mail fetcher's thread."""
    try:
        from ..database import conn_ctx
        with conn_ctx() as c:
            c.execute(
                "UPDATE pipelines SET next_run_at = now() WHERE owner_user_id = ? "
                "AND state = 'laeuft' AND (attention IS NULL OR attention IN "
                "('schritt_faellig', 'kann_nicht_pruefen'))",
                (str(owner_user_id),),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("pipelines.poke failed: %s", exc)
