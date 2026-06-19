/** Mirrors backend/main.py schema. */

export interface CalendarEvent {
  id: number;
  title: string;
  starts_at: string;        // ISO datetime, local time
  ends_at?: string | null;
  all_day: number;          // 0/1
  color?: string | null;
  person?: string | null;
  notes?: string | null;
  recurring?: string | null;
  // Set by the backend when this row is a synthesised occurrence of a
  // recurring series. The row's `id` still points at the series row
  // (so click-to-edit edits the series), but React keys MUST include
  // occurrence_date to stay unique across the visible window.
  occurrence_date?: string | null;
  is_recurring_instance?: boolean;
  allowed_roles: string;    // CSV
  created_at?: string;
  // Calendar overlay (migration 010)
  calendar_id?: number | null;
  owner_user_id?: number | null;
  visibility?: "default" | "private";
  // Set by the backend when the requester only has free_busy access or
  // when visibility=private hides details from a non-owner. Title is
  // already "Busy", but this flag lets the UI render a different
  // visual treatment (hatched pattern, lock icon).
  _busy_only?: boolean;
  // Travel-time + location (migration 019). Optional — older events
  // and events without a location have these as NULL.
  location?: string | null;
  location_lat?: number | null;
  location_lon?: number | null;
  travel_time_s?: number | null;
  travel_distance_m?: number | null;
  travel_provider?: string | null;
  travel_computed_at?: string | null;
  // Colour category (migration 026). One of the slugs in
  // categoryPalette.ts; NULL = use the legacy per-event `color`
  // fallback (or the default accent if neither is set).
  category?: string | null;
}

export interface Calendar {
  id: number;
  name: string;
  color: string;
  owner_user_id: number;
  kind: "personal" | "shared" | "project";
  hide_from_admin: number;        // 0/1
  archived_at: string | null;
  created_at: string;
  // Joined for the visible-to-me list
  access_level?: "free_busy" | "read" | "write";
  you_own?: boolean;
}

export interface CalendarShare {
  user_id: number;
  access_level: "free_busy" | "read" | "write";
  created_at: string;
  name?: string;
  email?: string;
  role?: string;
}

export interface EventAttendee {
  id: number;
  event_id: number;
  user_id: number | null;
  person_name: string | null;
  response_status: "needs_action" | "accepted" | "declined" | "tentative";
  proposed_time_iso: string | null;
  response_at: string | null;
  user_name?: string | null;
  user_email?: string | null;
  user_role?: string | null;
}

export type FreebusyBlocks = Record<string, Array<{ start: string; end: string }>>;

export interface Task {
  id: number;
  title: string;
  due_date?: string | null;
  done: number;
  person?: string | null;
  category?: string | null;
  notes?: string | null;
  allowed_roles: string;
  created_at?: string;
  assignees?: Array<{ user_id: number; name: string; status: string }>;
}

export interface AssignableUser {
  id: number;
  name: string;
}
