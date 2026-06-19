-- Migration 019: events location + travel time
--
-- Adds:
--   location           Free-text address as typed by the user (e.g. "Zahnarzt
--                      Praxis Dr. Müller, Hauptstr. 7, Hannover"). Source of
--                      truth — the geocoded lat/lon may go stale.
--   location_lat/lon   Cached geocode result for travel-time calculation
--                      and (later) map display.
--   travel_time_s      Seconds of driving time from the user's home address
--                      to this event's location, cached at create/update so
--                      the calendar list view doesn't have to recompute.
--                      NULL = unknown (no routing provider, or location empty).
--   travel_distance_m  Cached driving distance in metres.
--   travel_provider    Which routing provider answered, for cache invalidation
--                      when the user switches providers ("osrm" | "ors" |
--                      "google" | …).
--   travel_computed_at ISO timestamp of the last successful travel calc.
--                      Used to expire stale caches (e.g. > 30 days).

ALTER TABLE events ADD COLUMN location TEXT;
ALTER TABLE events ADD COLUMN location_lat REAL;
ALTER TABLE events ADD COLUMN location_lon REAL;
ALTER TABLE events ADD COLUMN travel_time_s INTEGER;
ALTER TABLE events ADD COLUMN travel_distance_m INTEGER;
ALTER TABLE events ADD COLUMN travel_provider TEXT;
ALTER TABLE events ADD COLUMN travel_computed_at TEXT;
