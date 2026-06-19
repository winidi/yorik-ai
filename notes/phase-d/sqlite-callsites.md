# SQLite call sites — inventory for Phase D

Generated: 2026-06-15T03:20:54+02:00

## Counts

- 107 files import sqlite3 or backend.database
- 93 files use conn_ctx() or get_conn()
- 62 migrations in schema_migrations (matches files in migrations/)

## Files importing sqlite3 directly (need translation)

```
backend/agent/conversation_io.py
backend/agent/vanna_shim.py
backend/app_loader.py
backend/app_sdk.py
backend/ask.py
backend/backup.py
backend/compose/series.py
backend/database.py
backend/documents.py
backend/email_autodraft.py
backend/email_fetcher.py
backend/main.py
backend/migrations.py
backend/skills/add_contact_channel/skill.py
backend/spaces.py
migrations/029_wa_messages_pk_owner.py
migrations/036_phase_b_spaces.py
migrations/037_drop_allowed_roles.py
migrations/055_paperless_chunks_space_id.py
migrations/056_events_space_id.py
migrations/057_connector_workspace_id.py
migrations/058_conversations_space_id.py
migrations/059_email_space_id.py
migrations/060_wa_space_id.py
migrations/061_remaining_tables_scoping.py
migrations/062_platform_admin_role.py
```

## Files using conn_ctx / get_conn (will dispatch via database.py shim)

```
backend/agent/cache.py
backend/agent/conversation_io.py
backend/agent/loop.py
backend/agent/vanna_shim.py
backend/app_sdk.py
backend/apps.py
backend/ask.py
backend/auth_sessions.py
backend/backup.py
backend/briefing_snapshots.py
backend/calendars.py
backend/compose/series.py
backend/connectors/paperless.py
backend/contact_address_scraper.py
backend/contact_autocapture.py
backend/contact_enricher.py
backend/contact_extractor.py
backend/contact_mailbox_crosslink.py
backend/contacts_dedupe_llm.py
backend/contacts.py
backend/conversation_store.py
backend/credential_store.py
backend/dashboard_routes.py
backend/database.py
backend/debug_bundle.py
backend/demo_data.py
backend/email_actions.py
backend/email_autodraft.py
backend/email_blocklist.py
backend/email_classifier.py
backend/email_fetcher.py
backend/email_routes.py
backend/email_sender.py
backend/error_log.py
backend/external_users.py
backend/household_settings.py
backend/immich_provisioning.py
backend/main.py
backend/notification_routes.py
backend/notifications.py
backend/onboarding_routes.py
backend/paperless_ingest.py
backend/paperless_provisioning.py
backend/pending_actions.py
backend/people_routes.py
backend/search_routes.py
backend/skills/add_bill/skill.py
backend/skills/add_calendar_event/skill.py
backend/skills/add_contact/skill.py
backend/skills/add_task/skill.py
backend/skills/block_travel_time/skill.py
backend/skills/calculate_travel_time/skill.py
backend/skills/check_bills/skill.py
backend/skills/check_calendar/skill.py
backend/skills/check_tasks/skill.py
backend/skills/compose_check_template_args/skill.py
backend/skills/compose_draft/skill.py
backend/skills/delete_bill/skill.py
backend/skills/delete_calendar_event/skill.py
backend/skills/delete_compose_draft/skill.py
backend/skills/delete_task/skill.py
backend/skills/email_briefing/skill.py
backend/skills/email_draft/skill.py
backend/skills/find_bill_by_name/skill.py
backend/skills/find_email_by_subject/skill.py
backend/skills/find_event_by_title/skill.py
backend/skills/find_known_provider/skill.py
backend/skills/find_photo/skill.py
backend/skills/find_provider_nearby/skill.py
backend/skills/find_recipient_address_from_documents/skill.py
backend/skills/find_task_by_title/skill.py
backend/skills/find_user/skill.py
backend/skills/list_contacts_for_picking/skill.py
backend/skills/list_subtasks/skill.py
backend/skills/read_my_profile/skill.py
backend/skills/registry.py
backend/skills/share_contact/skill.py
backend/skills/undo_last_action/skill.py
backend/skills/unshare_contact/skill.py
backend/skills/update_bill/skill.py
backend/skills/update_calendar_event/skill.py
backend/skills/update_contact/skill.py
backend/skills/update_task/skill.py
backend/skills/_web_helpers.py
backend/skills/whatsapp_draft/skill.py
backend/space_routes.py
backend/spaces.py
backend/users.py
backend/voice_id.py
backend/whatsapp_autodraft.py
backend/whatsapp_media.py
backend/whatsapp.py
backend/whatsapp_semantic.py
```

## SQLite-specific function/keyword call sites

- `datetime('now')`:  occurrences
- `date('now')`:  occurrences
- `INSERT OR IGNORE`: 9 occurrences
- `INSERT OR REPLACE`: 21 occurrences
- `AUTOINCREMENT`: 44 occurrences
- `INSTR(`:  occurrences
- `IFNULL(`:  occurrences

## FTS5 / sqlite_vec usage

```
backend/whatsapp_semantic.py:30:import sqlite_vec
backend/whatsapp_semantic.py:116:            (chunk_id, sqlite_vec.serialize_float32(vec)),
backend/whatsapp_semantic.py:155:        """, (sqlite_vec.serialize_float32(qvec), int(fetch_k))).fetchall()
backend/paperless_ingest.py:7:the `paperless_chunks` + `paperless_vec` tables in `documents.db`.
backend/paperless_ingest.py:31:import sqlite_vec
backend/paperless_ingest.py:141:        conn.execute("DELETE FROM paperless_vec WHERE rowid = ?", (cid,))
backend/paperless_ingest.py:209:                "INSERT INTO paperless_vec (rowid, embedding) VALUES (?, ?)",
backend/paperless_ingest.py:210:                (chunk_id, sqlite_vec.serialize_float32(vec)),
backend/paperless_ingest.py:273:    get their chunks + vectors wiped from paperless_chunks/paperless_vec.
backend/paperless_ingest.py:296:    # Prune stale paperless_chunks/paperless_vec first — fast, no network.
```
