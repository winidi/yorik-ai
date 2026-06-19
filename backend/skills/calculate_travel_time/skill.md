---
name: calculate_travel_time
description: "How long from A to B? Returns driving/cycling/walking time + distance via the maps connector."
when_to_use: |
  Whenever the user asks "wie lange brauche ich nach X", "how long to
  get to Y", "wie weit ist Z", "kann ich in 30 Minuten in Hannover sein".
  Also called internally by add_calendar_event when a location is set —
  you don't need to call it manually for that case (it happens server-side).

  Defaults:
    - `from` defaults to the user's home address (read from their profile)
      when omitted. Useful for "wie lange nach Hamburg" without the user
      having to specify their start point.
    - `mode` defaults to `driving`. Pass `cycling` / `walking` for those
      modes when the user mentioned bike or foot.

  If maps connector isn't reachable or returns no route, the skill
  returns a clear "couldn't compute" hint — surface that to the user
  honestly ("ich konnte die Fahrzeit gerade nicht berechnen") instead
  of making up a duration.
inputs:
  to:
    type: string
    required: true
    description: Destination — address, city, or POI name. The skill geocodes it via Nominatim.
  from:
    type: string
    required: false
    description: Start point. Same format as `to`. Omit to use the user's home address from their profile.
  mode:
    type: string
    required: false
    default: driving
    description: "'driving' (default) | 'cycling' | 'walking'."
outputs:
  duration_min:
    type: integer
    description: Travel time in minutes.
  duration_human:
    type: string
    description: Pretty form, e.g. "1h 25 min" or "23 min".
  distance_km:
    type: number
    description: Distance in kilometres (rounded to 1 decimal).
  provider:
    type: string
    description: Which backend answered — "osrm" or "ors".
permissions: [admin, member, restricted]
side_effects: none — read only. Hits the maps connector (Nominatim + OSRM/ORS).
tags: [maps, routing, travel]
---

# calculate_travel_time

Thin wrapper around `maps.directions` that defaults the `from` address
to the user's home (so "wie lange nach Hamburg" works without the user
specifying a starting point) and returns a flat shape the chat can
quote naturally.
