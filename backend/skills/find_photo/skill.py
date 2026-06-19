"""find_photo skill — Immich CLIP search / recent / by-person.

Uses the calling user's per-user Immich API key (wave 3) when available
so each user only sees photos in their own Immich library. The Immich
admin can still grant cross-user album access via Immich's own sharing
UI — that flows through their ACL naturally.
"""

from __future__ import annotations
import asyncio
from typing import Any, Optional


async def execute(
    ctx,
    query: Optional[str] = None,
    op: str = "search",
    person: Optional[str] = None,
    # Multi-person AND filter — pass a list OR a comma-separated string
    # ("Sara, Tom"). Photos must contain ALL listed people. Use this
    # for "Fotos von mir und Sara" / "photos of me and X". The LLM
    # should substitute "me" with the logged-in user's first name (see
    # the WHO 'ME' IS block in the system prompt).
    people: Optional[Any] = None,
    days: int = 7,
    take_count: int = 12,
    start_iso: Optional[str] = None,
    end_iso: Optional[str] = None,
    # `date` is a convenience alias the LLM often reaches for first.
    # "Fotos vom 23. März 2007" → date="2007-03-23". We expand it to
    # start_iso + end_iso spanning the whole calendar day.
    date: Optional[str] = None,
    # ── Metadata filters (any combo, all routed through op='filter') ──
    location: Optional[str] = None,         # "Berlin", "Italy", "Hamburg"
    favorites_only: bool = False,
    album: Optional[str] = None,            # album name (case-insensitive substring)
    media_type: Optional[str] = None,       # "image" | "video" | None=both
    camera_make: Optional[str] = None,      # "Canon", "Apple"
    camera_model: Optional[str] = None,     # "iPhone 14 Pro"
) -> dict[str, Any]:
    from backend.connectors.immich import immich

    # Normalise `people` to a list[str]. LLMs sometimes pass it as a
    # comma-separated string ("Sara, Tom") — fine, we split. Empty
    # entries dropped. Single-element list is fine too.
    people_list: list[str] = []
    if people:
        if isinstance(people, str):
            people_list = [p.strip() for p in people.split(",") if p.strip()]
        elif isinstance(people, list):
            people_list = [str(p).strip() for p in people if str(p).strip()]

    # Resolve "me" / "ich" / "I" → the logged-in user's first name.
    # skill.md asks the LLM to do this substitution via the WHO 'ME' IS
    # block in the system prompt, but smaller models keep passing
    # person="me" literally → Immich gets a face name it can't match
    # and the people-picker fires for a name that doesn't exist.
    # Resolve server-side so the LLM can't get this wrong.
    uid = getattr(ctx, "user_id", None)
    if uid:
        first_name = _user_first_name(uid)
        if first_name:
            person = _resolve_me(person, first_name)
            people_list = [_resolve_me(p, first_name) or p for p in people_list]

    # Expand the `date=YYYY-MM-DD` shortcut into a full-day window.
    if date and not (start_iso and end_iso):
        d = date.strip()[:10]  # tolerate "2007-03-23T..." too
        start_iso = f"{d}T00:00:00"
        end_iso   = f"{d}T23:59:59"

    # Pre-route rescue: "LLM stuffed multiple person names into the
    # CLIP query" pattern. skill.md tells the model to use
    # `person`/`people` for face searches and explicitly warns "DON'T
    # put the name in `query`", but small models keep packing names
    # into `query` anyway — caught in practice with the query
    # 'Anna Dirk Yorik' which CLIP can't match (it doesn't know
    # those faces).
    #
    # The post-result fallback at the bottom of this file handles the
    # SINGLE-name case (it walks prefixes 1..3 tokens and re-routes
    # to of_person), but it picks the first match and treats the
    # remainder as CLIP content — which for "Anna Dirk Yorik" ends
    # up as person=Anna, query="Dirk Yorik" → CLIP-searches the
    # text "Dirk Yorik" filtered by Anna's face, which still returns
    # garbage. Detect the multi-name case upfront and reroute to
    # `filter` with the right people list so the connector AND's the
    # personIds in one Immich call. Splits on whitespace, commas, &,
    # and the standalone "und"/"and" connectors.
    if query and not person and not people_list:
        import re as _re
        _chunks = _re.split(
            r"\s+(?:und|and)\s+|[,&]+|\s+",
            query.strip(),
            flags=_re.IGNORECASE,
        )
        _chunks = [c.strip() for c in _chunks if c.strip()]
        # 2-8 tokens, each name-shaped (≤20 chars). Anything longer or
        # wordier is almost certainly real content for CLIP.
        if 2 <= len(_chunks) <= 8 and all(len(c) <= 20 for c in _chunks):
            _creds_for_check = None
            if getattr(ctx, "user_id", None):
                from backend.external_users import get_user_immich_creds
                _creds_for_check = get_user_immich_creds(ctx.user_id)
            _known = [c for c in _chunks if _is_known_person(c, _creds_for_check)]
            if len(_known) >= 2:
                people_list = _known
                query = None

    # Translate `query` to English when it isn't already. Immich's
    # CLIP model (ViT-B-32 OpenAI) was trained on English captions —
    # German / French / Spanish queries return semantically-random
    # photos. The skill.md tells the LLM to translate before calling,
    # but smaller models keep passing the user's native-language word
    # straight through ("Anzug" instead of "suit"). Catch it server-
    # side so the LLM can't get this wrong either. Best-effort: any
    # failure (model down, slow) falls through to the original text.
    #
    # MUST run BEFORE auto-route so the of_person branch below sees
    # the post-scrub query state (otherwise a query like "Ella" that
    # the LLM also put in `people` would keep op='search' and the
    # face filter would never run).
    if query and not _looks_english(query):
        query = await _translate_query_to_english(query)

    # Defensive scrub: even with the skill.md "names go in person/people,
    # never query" rule, the LLM keeps slipping names into `query`
    # alongside the content ("Dirk Anzug", "Sara beach"). CLIP doesn't
    # know who Dirk or Sara is, so the name tokens either add noise to
    # the embedding or push the result toward whatever the model
    # associates with the literal word "dirk" — neither does what the
    # user asked for. Strip any token (case-insensitive) that matches a
    # name in `person` / `people` before handing the query to Immich.
    #
    # MUST run BEFORE auto-route — see translation note above.
    if query and (person or people_list):
        import re as _re3
        names_to_strip = [n for n in [person, *people_list] if n]
        cleaned = query
        for nm in names_to_strip:
            # \b...\b makes "Sara" not eat the "sar" in "sarcophagus".
            cleaned = _re3.sub(
                rf"\b{_re3.escape(nm)}\b",
                "",
                cleaned,
                flags=_re3.IGNORECASE,
            )
        cleaned = " ".join(cleaned.split())  # collapse whitespace
        # Only adopt the scrubbed version if something is left. An
        # all-names query like "Dirk Anna" becomes empty — let it
        # become None so the call falls through to op='of_person' or
        # 'filter' as the face-filter path the user actually wanted.
        query = cleaned or None

    # Auto-route: any metadata filter (location/favorite/album/camera/
    # media_type) AND/OR combinations OR multi-person (people_list has
    # >= 2) win — they all funnel into the 'filter' op which does one
    # combined Immich query (personIds is AND'd). Pure date alone still
    # goes to 'taken_on'; pure single-person still goes to 'of_person'.
    has_meta_filter = bool(
        location or favorites_only or album or media_type
        or camera_make or camera_model or len(people_list) >= 2
    )
    if has_meta_filter:
        op = "filter"
    elif (start_iso or end_iso) and person:
        # Single-person + date together — _taken_on can't filter by
        # person (its connector signature is date-only), so the name
        # would silently drop and the user would get every photo on
        # that date instead of theirs. _filter takes both in one
        # query (takenAfter/Before + personIds AND).
        op = "filter"
    elif (start_iso or end_iso) and op in ("search", "recent"):
        op = "taken_on"
    elif person and not query and op == "search":
        # The LLM frequently calls find_photo(person="X") with no op and
        # no query — the skill.md asks for op='of_person' explicitly,
        # but in practice the model drops it. Without this auto-route,
        # op stays at the default "search" → empty CLIP query →
        # Immich returns generic recent photos that have nothing to do
        # with X. Symptom from a user: "Fotos von Anna" returned the
        # same recent photos as "Fotos von Dirk" — because both
        # queries collapsed to the same empty-query search.
        # Mirrors the existing date → 'taken_on' auto-route: same
        # shape of safety net for a routing mistake the LLM keeps
        # making.
        op = "of_person"
    elif (people_list and len(people_list) == 1 and not query
          and not person and op == "search"):
        # Same routing fix but for the people=["me"] / people=["X"] call
        # shape. After _resolve_me swaps "me" for the user's first name
        # we have people_list=["Dirk"] but `person` is still None — the
        # of_person branch above only checks `person`. Without this
        # extra arm, "find photos of me" lands in plain search with no
        # query and falls back to recent photos library-wide. Promote
        # the single name to `person` + flip to of_person so the face
        # filter actually runs.
        person = people_list[0]
        people_list = []
        op = "of_person"

    args = {"op": op, "take_count": int(take_count)}
    if query: args["query"] = query
    # Single-name back-compat: collapse a 1-element people_list into
    # `person` so op='of_person' keeps working unchanged.
    if person and not people_list:
        args["person"] = person
    elif len(people_list) == 1 and op != "filter":
        args["person"] = people_list[0]
    if people_list:
        args["people"] = people_list  # passed straight through to connector
    if days: args["days"] = int(days)
    if start_iso: args["start_iso"] = start_iso
    if end_iso:   args["end_iso"] = end_iso
    if location:       args["location"]       = location
    if favorites_only: args["favorites_only"] = True
    if album:          args["album"]          = album
    if media_type:     args["media_type"]     = media_type
    if camera_make:    args["camera_make"]    = camera_make
    if camera_model:   args["camera_model"]   = camera_model
    # Per-user key override — see backend/connectors/immich.py for how
    # this is read. None falls back to the global admin key for backward
    # compat.
    #
    # Phase C T12: a logged-in user without per-user Immich creds used to
    # fall through to the admin key, which meant a workspace member or
    # workspace admin without an Immich account saw the WHOLE
    # installation's library (every other workspace's photos). Fail
    # closed instead — only platform_admin gets the fallback.
    if getattr(ctx, "user_id", None):
        from backend.external_users import get_user_immich_creds
        uc = get_user_immich_creds(ctx.user_id)
        if uc:
            args["creds_override"] = uc
        elif (getattr(ctx, "role", None) or "").lower() != "platform_admin":
            return {
                "ok":       False,
                "op":       op,
                "photos":   [],
                "error":    "No Immich account is connected for you yet. "
                            "Ask your admin to provision your Immich access "
                            "in Settings → Users.",
                "degraded": True,
            }

    # Pre-check named-people resolution. If the user asked for someone
    # ("Sara", or multi: "Tom, Sara") whose face isn't labeled in
    # Immich yet, the query will return nothing useful. Surface a face-
    # picker card so the user can name the unrecognised cluster(s)
    # right from chat — no detour through Immich's People UI required.
    creds = args.get("creds_override")
    requested_names: list[str] = list(people_list)
    if person and person not in requested_names:
        requested_names.append(person)
    missing_names: list[str] = []
    if requested_names:
        for nm in requested_names:
            if not _is_known_person(nm, creds):
                missing_names.append(nm)

    if missing_names:
        from backend.connectors.immich import _people_for_picker
        candidates = await asyncio.to_thread(_people_for_picker, 36, creds)
        if candidates:
            # Build the resume args the picker uses to re-invoke find_photo
            # once the user has labeled the face(s). Strips creds_override
            # (rebuilt per-call) and any internal flags.
            resume_args: dict[str, Any] = {}
            if query:          resume_args["query"]          = query
            if op != "filter": resume_args["op"]             = op  # original op
            if requested_names:
                # Use the multi-name shape if there are >= 2 names; single
                # name re-uses `person` to keep the cheaper of_person path.
                if len(requested_names) >= 2:
                    resume_args["people"] = ", ".join(requested_names)
                else:
                    resume_args["person"] = requested_names[0]
            if days != 7:      resume_args["days"]      = days
            if take_count != 12: resume_args["take_count"] = take_count
            if start_iso:      resume_args["start_iso"] = start_iso
            if end_iso:        resume_args["end_iso"]   = end_iso
            if date:           resume_args["date"]      = date
            if location:       resume_args["location"]  = location
            if favorites_only: resume_args["favorites_only"] = True
            if album:          resume_args["album"]     = album
            if media_type:     resume_args["media_type"] = media_type
            if camera_make:    resume_args["camera_make"] = camera_make
            if camera_model:   resume_args["camera_model"] = camera_model

            from backend.ui_tools import _append
            _append({
                "type":          "people_picker",
                "missing_names": missing_names,
                "candidates":    candidates,
                "resume_skill":  "find_photo",
                "resume_args":   resume_args,
            })
            # Skip the (useless) photo render — picker is the main event.
            llm_hint = (
                f"shown_to_user:people_picker for {missing_names} "
                f"({len(candidates)} face cluster(s) shown — mix of unlabeled and "
                f"already-labeled, in case the user's spelling differs from an existing label). "
                f"Tell the user briefly that they need to identify the face(s) "
                f"to continue — don't list the candidates yourself, the picker does. "
                f"After they pick, the search re-runs automatically."
            )
            return {
                "_llm_hint":     llm_hint,
                "photos":        [],
                "missing_people": missing_names,
            }

    result = await asyncio.to_thread(immich, **args)
    photos = result.get("photos") or []

    # Auto-route fallback: small LLMs often pack the person name into
    # the CLIP query instead of using `person`. Three cases handled:
    #
    #   1. Single token, known person ("dirk")        → op='of_person'
    #   2. Multi-word "name + content" ("dirk anzug") → op='search'
    #      with personIds AND'd into the CLIP query (Immich does both
    #      in one call via SmartSearchDto.personIds — verified against
    #      the server DTOs).
    #   3. Single token name, NOT a known person ("yarik") → surface
    #      the face-picker so the user can name the cluster.
    #
    # Longest known-name prefix wins ("Hans Müller wedding" before
    # "Hans"). Cap at 3 tokens — Immich names are rarely longer.
    if op == "search" and query and not person:
        creds = args.get("creds_override")
        q_stripped = query.strip()
        tokens = q_stripped.split()

        def _name_shaped(t: str) -> bool:
            return (2 < len(t) <= 30
                    and t.replace("-", "").replace("'", "").isalpha())

        matched_person: Optional[str] = None
        matched_remainder: str = ""
        for prefix_len in range(min(3, len(tokens)), 0, -1):
            prefix_tokens = tokens[:prefix_len]
            if not all(_name_shaped(t) for t in prefix_tokens):
                continue
            candidate = " ".join(prefix_tokens)
            if _is_known_person(candidate, creds):
                matched_person = candidate
                matched_remainder = " ".join(tokens[prefix_len:]).strip()
                break

        if matched_person and matched_remainder:
            # person + content: keep op='search', connector now AND's
            # personIds into the CLIP query.
            person = matched_person
            query = matched_remainder
            retry_args = dict(args, op="search",
                              person=person, query=query)
            result = await asyncio.to_thread(immich, **retry_args)
            photos = result.get("photos") or []
        elif matched_person:
            # pure name — standard of_person path
            person = matched_person
            op = "of_person"
            retry_args = dict(args, op="of_person", person=person)
            retry_args.pop("query", None)
            query = None
            result = await asyncio.to_thread(immich, **retry_args)
            photos = result.get("photos") or []
        elif (
            len(tokens) == 1
            and _name_shaped(tokens[0])
            and tokens[0][0].isupper()
            and len(tokens[0]) <= 15
        ):
            # Single-token query that LOOKS like a person name and
            # isn't yet labeled in People — surface the face-naming
            # picker. The capitalization gate is the load-bearing
            # filter: the LLM emits content queries in lowercase
            # ("autos", "sunset", "beach") but person names with a
            # leading capital. Without this check, "zeige mir
            # bilder von autos" hit CLIP, returned 0 (no car photos
            # in the library), and the rescue cheerfully treated
            # "autos" as a maybe-unlabeled person — jarring UX.
            # Length cap is belt-and-braces: real first names are
            # ≤15 chars, capitalised non-name nouns can be longer.
            # German nouns ("Hochzeit", "Berlin") are still capit-
            # alised + short and will produce occasional false
            # positives here; admin can dismiss the picker.
            from backend.connectors.immich import _people_for_picker
            candidates = await asyncio.to_thread(_people_for_picker, 36, creds)
            if candidates:
                from backend.ui_tools import _append
                _append({
                    "type":          "people_picker",
                    "missing_names": [q_stripped],
                    "candidates":    candidates,
                    "resume_skill":  "find_photo",
                    "resume_args":   {"person": q_stripped, "op": "of_person",
                                      "take_count": int(take_count)},
                })
                llm_hint = (
                    f"shown_to_user:people_picker for '{q_stripped}' "
                    f"(name-shaped query, no labeled face). "
                    f"Tell the user briefly that you need help identifying "
                    f"who {q_stripped!r} is — picker will let them name a "
                    f"face. Don't list candidates yourself."
                )
                return {"_llm_hint": llm_hint, "photos": [],
                        "missing_people": [q_stripped]}

    # Emit a ui_action so the chat renders the photos inline as cards
    # (same pattern as search_documents → documents_found). Skip when
    # there's nothing to show — empty grids look broken.
    if photos:
        from backend.ui_tools import _append
        compact = [{
            "id":            p.get("id"),
            "thumbnail_url": p.get("thumbnail_url"),
            "original_name": p.get("original_name"),
            "taken_at":      p.get("taken_at") or p.get("date_taken"),
            "type":          p.get("type") or "IMAGE",
        } for p in photos]
        _append({
            "type":    "photos_found",
            "op":      op,
            "person":  person,
            "query":   query,
            "photos":  compact,
        })

    # Short factoid for the LLM — it'll naturally narrate around it
    # without echoing instructions back at the user. Keep this BRIEF
    # and prose-like; longer or more directive hints get copy-pasted
    # verbatim by small models. The actual photos render via the
    # photos_found ui_action above.
    if photos:
        if op == "filter":
            # Compose a short human description of which filters bit.
            bits: list[str] = []
            if location:       bits.append(f"in {location}")
            if people_list:    bits.append(f"with {' & '.join(people_list)}")
            elif person:       bits.append(f"with {person}")
            if favorites_only: bits.append("favorites")
            if album:          bits.append(f"from album '{album}'")
            if media_type:     bits.append(f"({media_type.lower()})")
            if start_iso and end_iso:
                bits.append(f"between {start_iso[:10]} and {end_iso[:10]}")
            llm_hint = f"shown_to_user:{len(photos)} photos {' '.join(bits) or '(filtered)'}"
        elif op == "of_person" and person:
            llm_hint = f"shown_to_user:{len(photos)} photos of {person}"
        elif op == "taken_on":
            window = (start_iso or "")[:10] or "date range"
            llm_hint = f"shown_to_user:{len(photos)} photos taken on {window}"
        elif query:
            llm_hint = f"shown_to_user:{len(photos)} photos matching '{query}'"
        else:
            llm_hint = f"shown_to_user:{len(photos)} recent photos"
    elif op == "stats":
        llm_hint = (f"library has {result.get('total_photos', 0)} photos, "
                    f"{result.get('total_videos', 0)} videos")
    else:
        llm_hint = f"no photos found"

    return {
        "_llm_hint":    llm_hint,
        "photos":       photos,
        "op":           result.get("op"),
        "warning":      result.get("warning"),
        # Library-wide counts returned by the connector on search/recent/stats.
        # Lets the LLM answer "how many photos do I have" without a second tool call.
        "total_photos": result.get("total_photos"),
        "total_videos": result.get("total_videos"),
        "total_assets": result.get("total_assets"),
    }


# Tiny in-process cache of named Immich people. 60s TTL is plenty —
# the user labels new faces in Immich infrequently and any query that
# misses a fresh name just falls through to CLIP search.
import time as _time
_known_people_cache: dict = {"at": 0.0, "names": set(), "key": None}


def _is_known_person(name: str, creds_override: Optional[dict] = None) -> bool:
    """Return True if `name` (case-insensitive) is a labelled person
    in Immich. Used by the of_person auto-route so a vague LLM call
    like find_photo(op='search', query='anna') becomes a face filter
    when 'anna' is in Immich's People."""
    cache_key = id(creds_override) if creds_override else "global"
    now = _time.time()
    if (_known_people_cache["key"] != cache_key
            or now - _known_people_cache["at"] > 60):
        try:
            from backend import credential_store as cs
            import requests as _req
            creds = creds_override or (cs.get("immich") or {})
            base = (creds.get("base_url") or "http://localhost:2283").rstrip("/")
            key = creds.get("api_key")
            if not key:
                _known_people_cache.update({"at": now, "key": cache_key, "names": set()})
                return False
            r = _req.get(f"{base}/api/people",
                         headers={"x-api-key": key, "Accept": "application/json"},
                         params={"size": 500}, timeout=2.0)
            r.raise_for_status()
            body = r.json()
            people = body.get("people", []) if isinstance(body, dict) else body
            names = {(p.get("name") or "").strip().lower()
                     for p in people if p.get("name")}
            _known_people_cache.update({"at": now, "key": cache_key, "names": names})
        except Exception:
            return False  # Immich unreachable or weird shape — skip the route
    return name.strip().lower() in _known_people_cache["names"]


# ─────────── "me" → user first name ───────────

# Tokens the LLM uses as a stand-in for the logged-in user. The
# skill.md asks for substitution upfront via the system prompt's
# WHO 'ME' IS block, but smaller models keep passing these literally.
_ME_TOKENS = {"me", "i", "mich", "ich", "self", "selbst", "myself", "moi"}


def _resolve_me(name: Optional[str], first_name: str) -> Optional[str]:
    """If `name` is a stand-in for the user ("me" / "ich"), return the
    user's first name; otherwise return `name` unchanged. None stays
    None so the auto-route's `if person` checks behave the same."""
    if not name:
        return name
    if name.strip().lower() in _ME_TOKENS:
        return first_name
    return name


def _user_first_name(user_id: str) -> Optional[str]:
    """First name from user_profiles for the "me" → name substitution.
    Returns None when the user has no profile / no name on file, in
    which case _resolve_me leaves the value untouched."""
    try:
        from backend.database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT first_name, name FROM user_profiles WHERE id=?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        first = (row["first_name"] or "").strip()
        if not first and (row["name"] or "").strip():
            first = row["name"].split()[0]
        return first or None
    except Exception:
        return None


# ─────────── German → English query translation ───────────

# Common ASCII-only German words the user's prompts pass straight through.
# Extend as we hit new cases — keeps the fast path fast (no LLM call)
# for the 95% of queries that hit one of these patterns. Anything that
# doesn't trigger here AND looks German-shaped falls through to the
# LLM translator.
_GERMAN_HINTS = {
    "anzug", "kleid", "hose", "schuhe", "hemd", "mantel", "hut", "mütze",
    "strand", "meer", "see", "berg", "wald", "garten", "haus", "auto",
    "fahrrad", "rad", "motorrad", "zug", "flugzeug", "schiff",
    "hund", "katze", "pferd", "kuh", "vogel", "fisch",
    "sonne", "sonnenuntergang", "sonnenaufgang", "mond", "sterne",
    "schnee", "regen", "wolken", "himmel", "nebel",
    "kuchen", "torte", "essen", "frühstück", "kaffee", "tee", "wein", "bier",
    "kind", "kinder", "baby", "frau", "mann",
    "bart", "brille", "kopfhörer", "hut",
    "weihnachten", "ostern", "geburtstag", "hochzeit",
    "schloss", "kirche", "brücke", "straße", "stadt", "dorf",
    "blume", "blumen", "baum", "bäume", "wiese",
    "spiegel", "fenster", "tür", "treppe",
}


def _looks_english(text: str) -> bool:
    """Best-effort English detector — skip the LLM call when the LLM
    already gave us English. Two signals:
      - any non-ASCII letter (ä ö ü ß é è à ñ …) → not English
      - any token in the German-hint set → not English
    False positives are cheap (one extra translation round-trip),
    false negatives are expensive (Immich CLIP returns garbage). Bias
    toward translating.
    """
    if not text or not text.strip():
        return True
    if any(ord(c) > 127 for c in text):
        return False
    words = [w.strip(".,!?;:'\"()[]").lower() for w in text.split()]
    return not any(w in _GERMAN_HINTS for w in words if w)


async def _translate_query_to_english(text: str) -> str:
    """One-shot LLM call: turn the query into an English CLIP-friendly
    phrase. Returns the original text on any failure so a degraded LLM
    can't break photo search entirely — Immich still gets *something*
    to embed, even if it's the German original."""
    prompt = (
        "Translate this photo search query into a SHORT English phrase "
        "suitable for CLIP semantic image search. Return ONLY the "
        "translated phrase — no quotes, no explanation, no list. "
        "Keep it under 8 words. Examples:\n"
        "  Anzug → suit\n"
        "  am Strand bei Sonnenuntergang → beach at sunset\n"
        "  Hund im Schnee → dog in snow\n"
        "  rotes Auto vor einem Haus → red car in front of a house\n"
        "\n"
        f"Query: {text}\n"
        "English:"
    )
    try:
        from backend.whatsapp import _call_llm
        out = await _call_llm(prompt)
        cleaned = (out or "").strip().strip('"').strip("'")
        # Some models echo "English: " — drop it.
        if cleaned.lower().startswith("english:"):
            cleaned = cleaned.split(":", 1)[1].strip()
        # First line only — guard against models that go chatty.
        cleaned = cleaned.splitlines()[0].strip() if cleaned else ""
        return cleaned or text
    except Exception:
        return text
