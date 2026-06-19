"""Reading list — full-surface manifest v2 reference.

Exercises every v2 permission category:
  * pg_db()       — owned-schema CRUD
  * family-DB     — reads public.contacts via the projection view
                    (granted in manifest.permissions.reads)
  * skill calls   — could invoke find_person; the reference here
                    keeps it as a one-liner so the SDK signature is
                    visible without a separate skills config

Three operations:
  - add_item(url, title=None, recommended_by_contact_id=None)
  - mark_status(item_id, status)
  - list_items(status='unread')
"""

from yorik.app_sdk import operation, pg_db


_VALID_STATUSES = {"unread", "reading", "read", "skipped"}


@operation(role=["admin", "member"], description="Add a URL to the reading list. Optionally tag with a contact who recommended it.")
def add_item(url: str, title: str = None,
             recommended_by_contact_id: int = None) -> dict:
    url = (url or "").strip()
    if not url:
        return {"error": "url required"}
    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reading_items "
                "  (url, title, recommended_by_contact_id) "
                "VALUES (%s, %s, %s) "
                "RETURNING id, url, title, status, created_at",
                (url, title, recommended_by_contact_id),
            )
            row = cur.fetchone()
            return {
                "id": row[0], "url": row[1], "title": row[2],
                "status": row[3], "created_at": row[4].isoformat(),
            }


@operation(role=["admin", "member"], description="Mark a reading item as unread / reading / read / skipped.")
def mark_status(item_id: int, status: str) -> dict:
    if status not in _VALID_STATUSES:
        return {"error": f"status must be one of {sorted(_VALID_STATUSES)}"}
    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reading_items SET "
                "  status = %s, "
                "  read_at = CASE WHEN %s = 'read' THEN now() ELSE read_at END "
                "WHERE id = %s "
                "RETURNING id, status, read_at",
                (status, status, int(item_id)),
            )
            row = cur.fetchone()
            if row is None:
                return {"error": f"item {item_id} not found"}
            return {
                "id": row[0], "status": row[1],
                "read_at": row[2].isoformat() if row[2] else None,
            }


@operation(role=["admin", "member"], description="List reading items, filtered by status.")
def list_items(status: str = "unread") -> dict:
    if status != "all" and status not in _VALID_STATUSES:
        return {"error": f"status must be 'all' or one of {sorted(_VALID_STATUSES)}"}
    with pg_db() as conn:
        with conn.cursor() as cur:
            if status == "all":
                cur.execute(
                    "SELECT id, url, title, status, "
                    "       recommended_by_contact_id, created_at "
                    "  FROM reading_items "
                    "  ORDER BY created_at DESC LIMIT 200"
                )
            else:
                cur.execute(
                    "SELECT id, url, title, status, "
                    "       recommended_by_contact_id, created_at "
                    "  FROM reading_items "
                    "  WHERE status = %s "
                    "  ORDER BY created_at DESC LIMIT 200",
                    (status,),
                )
            rows = cur.fetchall()
    return {
        "items": [
            {
                "id": r[0], "url": r[1], "title": r[2],
                "status": r[3], "recommended_by_contact_id": r[4],
                "created_at": r[5].isoformat(),
            }
            for r in rows
        ],
    }
