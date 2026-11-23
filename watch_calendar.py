"""Calendar targeted watches: exact calendars/events/series with local deterministic filters."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, time as dtime, timedelta

if __package__:
    from . import watch as W
else:  # Direct module use by the standalone test suite.
    import watch as W

TARGET_KEYS = {"calendar_ids", "event_ids", "recurring_event_ids"}
FILTER_KEYS = {"organizer_addresses", "attendee_addresses", "summary_contains",
               "change_types", "changed_fields", "date_window"}
CHANGE_TYPES = {"created", "updated", "cancelled"}
CHANGED_FIELDS = {"start", "end", "location", "summary", "status", "attendee_responses"}
TOOL_NAME = "gws_calendar_watch"
MAX_EVENTS = 2000
EVENT_FIELDS = ("id,status,summary,location,start,end,organizer,attendees,recurringEventId,"
                "originalStartTime,updated,eventType,recurrence")


def _call(client, account, args, params=None, body=None, timeout=30):
    return client.call(account, args, params=params, body=body, timeout=timeout)


def _calendar_get(client, account, calendar_id):
    result = _call(client, account, ["calendar", "calendars", "get"], {"calendarId": calendar_id})
    if not result.get("ok") or not isinstance(result.get("data"), dict):
        raise W.WatchError("calendar_access", "Could not read the selected calendar with this account")
    concrete = result["data"].get("id")
    if not isinstance(concrete, str) or not concrete:
        raise W.WatchError("calendar_access", "Calendar did not return a concrete ID")
    timezone = result["data"].get("timeZone")
    return concrete, timezone if isinstance(timezone, str) and timezone else None


def _event_get(client, account, calendar_id, event_id):
    result = _call(client, account, ["calendar", "events", "get"], {
        "calendarId": calendar_id, "eventId": event_id, "fields": EVENT_FIELDS,
        "maxAttendees": 100,
    })
    if not result.get("ok"):
        if result.get("http_status") in (404, 410):
            return None
        raise W.WatchError("calendar_access",
                           "Could not read the selected Calendar event with this account")
    return result.get("data") if isinstance(result.get("data"), dict) else None


def _event_meta(data):
    start = data.get("start") or {}
    end = data.get("end") or {}
    organizer = data.get("organizer") or {}
    attendees = []
    for attendee in (data.get("attendees") or [])[:100]:
        email = str(attendee.get("email") or "").strip().lower()
        if email:
            attendees.append({"email": email, "responseStatus": attendee.get("responseStatus") or ""})
    original = data.get("originalStartTime") or {}
    return {
        "id": data.get("id") or "", "status": data.get("status") or "confirmed",
        "summary": (data.get("summary") or "")[:500],
        "location": (data.get("location") or "")[:500],
        "start": {key: value for key, value in start.items() if key in ("dateTime", "date", "timeZone")},
        "end": {key: value for key, value in end.items() if key in ("dateTime", "date", "timeZone")},
        "organizer": str(organizer.get("email") or "").strip().lower(),
        "attendees": attendees,
        "recurringEventId": data.get("recurringEventId") or "",
        "recurrence": bool(data.get("recurrence")),
        "originalStartTime": {key: value for key, value in original.items()
                              if key in ("dateTime", "date", "timeZone")},
        "updated": data.get("updated") or "",
    }


def _list_initial(client, account, calendar_id, *, page_limit=20):
    events, token, sync = {}, None, None
    for _ in range(page_limit):
        params = {"calendarId": calendar_id, "maxResults": 250, "showDeleted": False,
                  "singleEvents": False, "maxAttendees": 100,
                  "fields": f"nextPageToken,nextSyncToken,items({EVENT_FIELDS})"}
        if token:
            params["pageToken"] = token
        result = _call(client, account, ["calendar", "events", "list"], params)
        if not result.get("ok"):
            raise W.WatchError("calendar_access", "Could not establish the calendar baseline")
        data = result.get("data") or {}
        for event in data.get("items", []) or []:
            meta = _event_meta(event)
            if meta["id"] and len(events) < MAX_EVENTS:
                events[meta["id"]] = meta
        token = data.get("nextPageToken")
        sync = data.get("nextSyncToken") or sync
        if not token:
            if not sync:
                raise W.WatchError("calendar_access", "Calendar did not return a sync token")
            return events, sync
    raise W.WatchError("calendar_access", "Calendar baseline exceeded the bounded page limit")


def validate_selectors(args):
    target = args.get("target")
    filters = args.get("filters", {})
    if not isinstance(target, dict) or set(target) - TARGET_KEYS:
        raise W.WatchError("invalid_arguments",
                           "calendar target accepts calendar_ids, optional event_ids or recurring_event_ids")
    if "calendar_ids" not in target:
        raise W.WatchError("invalid_arguments", "calendar_ids is required")
    target = {k: W.string_list(v, k, maximum=20 if k == "calendar_ids" else W.MAX_IDS)
              for k, v in target.items()}
    if "event_ids" in target and "recurring_event_ids" in target:
        raise W.WatchError("invalid_arguments", "event_ids and recurring_event_ids are alternatives")
    if not isinstance(filters, dict) or set(filters) - FILTER_KEYS:
        raise W.WatchError("invalid_arguments", "unknown calendar filter field")

    clean = {}
    if "organizer_addresses" in filters:
        clean["organizer_addresses"] = W.string_list(
            filters["organizer_addresses"], "organizer_addresses", cleaner=W.clean_address)
    if "attendee_addresses" in filters:
        clean["attendee_addresses"] = W.string_list(
            filters["attendee_addresses"], "attendee_addresses", cleaner=W.clean_address)
    if "summary_contains" in filters:
        clean["summary_contains"] = W.string_list(
            filters["summary_contains"], "summary_contains",
            cleaner=lambda value, label: W.clean_text(value, label, maximum=200))
    if "change_types" in filters:
        clean["change_types"] = W._clean_change_types(filters["change_types"], CHANGE_TYPES)
    if "changed_fields" in filters:
        changed = string_fields = W.string_list(
            filters["changed_fields"], "changed_fields",
            cleaner=lambda value, label: W.clean_text(value, label, maximum=32))
        unknown = [field for field in changed if field not in CHANGED_FIELDS]
        if unknown:
            raise W.WatchError("invalid_arguments",
                               f"changed_fields accepts only: {', '.join(sorted(CHANGED_FIELDS))}")
        clean["changed_fields"] = changed
    if "date_window" in filters:
        window = filters["date_window"]
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise W.WatchError("invalid_arguments", "date_window requires exactly start and end")
        start = W.parse_rfc3339(window["start"], "date_window.start")
        end = W.parse_rfc3339(window["end"], "date_window.end")
        if start >= end:
            raise W.WatchError("invalid_arguments", "date_window.start must be before end")
        clean["date_window"] = {"start": start.isoformat(), "end": end.isoformat()}
    if "attendee_responses" in clean.get("changed_fields", []) and not clean.get("attendee_addresses"):
        raise W.WatchError("invalid_arguments",
                           "changed_fields=attendee_responses requires attendee_addresses")

    if "recurring_event_ids" in target and clean.get("date_window"):
        raise W.WatchError(
            "unsupported_combination",
            "recurring_event_ids with date_window is not supported in this version; use a bounded "
            "event_ids watch or omit date_window")
    return target, clean


def prepare_start(client, account, args, config):
    started_at = time.time()
    target, clean = validate_selectors(args)
    if not config.get("watch_callback_url"):
        raise W.WatchError(
            "calendar_callback_missing",
            "Set watch_callback_url to the public HTTPS base that reaches the worker before starting a Calendar watch")

    concrete, timezones = [], {}
    for calendar_id in target["calendar_ids"]:
        resolved, timezone = _calendar_get(client, account, calendar_id)
        if resolved not in concrete:
            concrete.append(resolved)
            if timezone:
                timezones[resolved] = timezone
    pairs = []
    series = []
    master_selected = False
    if "event_ids" in target:
        for event_id in target["event_ids"]:
            found = [(calendar_id, _event_get(client, account, calendar_id, event_id))
                     for calendar_id in concrete]
            existing = [(calendar_id, data) for calendar_id, data in found if data is not None]
            if not existing:
                raise W.WatchError("target_not_found", "A selected event was not found in the target calendars")
            if len(existing) > 1:
                raise W.WatchError(
                    "ambiguous_event",
                    "The same event ID exists in more than one target calendar; watch one calendar at a time")
            calendar_id, data = existing[0]
            if data.get("recurrence"):
                master_selected = True
            pairs.append({"calendar_id": calendar_id, "event_id": event_id,
                          "recurring_event_id": data.get("recurringEventId") or ""})
    if clean.get("date_window") and master_selected:
        raise W.WatchError(
            "unsupported_combination",
            "event_ids selecting a recurring series master with date_window is not supported in this "
            "version; use recurring_event_ids without date_window or select a single occurrence")
    if "recurring_event_ids" in target:
        for series_id in target["recurring_event_ids"]:
            found = [(calendar_id, _event_get(client, account, calendar_id, series_id))
                     for calendar_id in concrete]
            existing = [(calendar_id, data) for calendar_id, data in found
                        if data is not None and data.get("recurrence")]
            if not existing:
                raise W.WatchError("target_not_found", "A selected recurring series was not found")
            if len(existing) > 1:
                raise W.WatchError("ambiguous_event",
                                   "The same series ID exists in more than one target calendar; watch one calendar")
            calendar_id, _ = existing[0]
            series.append({"calendar_id": calendar_id, "series_id": series_id})

    target_resolved = {"calendar_ids": concrete, "broad": not pairs and not series,
                       "created_cancelled_bypass_changed_fields": True}
    if pairs:
        target_resolved["event_pairs"] = pairs
    if series:
        target_resolved["series_pairs"] = series

    sources, snapshots = [], {}
    for calendar_id in concrete:
        events, sync = _list_initial(client, account, calendar_id)
        snapshots[calendar_id] = events
        sources.append({"resource": {"calendar_id": calendar_id},
                        "query": {"singleEvents": False}, "cursor": sync,
                        "baseline": {"sync_token": sync}})
    baseline = {"calendars": snapshots, "timezones": timezones, "not_before": started_at}
    return {"target": target_resolved, "filters": clean, "sources": sources,
            "baseline": baseline, "resolved": target_resolved}


def _event_floor(watch):
    floor = float(watch["not_before"])
    try:
        captured = json.loads(watch.get("baseline_json") or "{}").get("not_before")
    except (TypeError, ValueError):
        captured = None
    if isinstance(captured, (int, float)) and not isinstance(captured, bool):
        floor = min(floor, float(captured))
    return floor


def event_after_start(watch, event):
    occurred = event.get("occurred_at")
    return occurred is None or float(occurred) >= _event_floor(watch)


def _target_match(watch, calendar_id, event_id, recurring_id):
    target = json.loads(watch["target_json"])
    if calendar_id not in target.get("calendar_ids", []):
        return False
    if target.get("event_pairs"):
        return any(pair["calendar_id"] == calendar_id and pair["event_id"] == event_id
                   for pair in target["event_pairs"])
    if target.get("series_pairs"):
        return any(pair["calendar_id"] == calendar_id
                   and (recurring_id == pair["series_id"] or event_id == pair["series_id"])
                   for pair in target["series_pairs"])
    return True


def _value(meta, field, selected_people=None):
    if not meta:
        return None
    if field == "attendee_responses":
        people = selected_people or set()
        return tuple(sorted((a["email"], a["responseStatus"]) for a in meta.get("attendees", [])
                            if not people or a["email"] in people))
    return meta.get(field)


def _fields_changed(before, after, fields, selected_people=None):
    for field in fields:
        if _value(before, field, selected_people) != _value(after, field, selected_people):
            return True
    return False


def _calendar_timezone(watch, calendar_id):
    """Calendar timezone captured at start; all-day events lack their own."""
    try:
        return (json.loads(watch.get("baseline_json") or "{}").get("timezones") or {}).get(calendar_id)
    except (TypeError, ValueError):
        return None


def _window_overlaps(meta, window, calendar_tz=None):
    if not meta:
        return False
    start, end = window["start"], window["end"]
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    values = meta.get("start") or {}
    ends = meta.get("end") or {}
    if "date" in values:
        from zoneinfo import ZoneInfo
        zone_name = values.get("timeZone") or calendar_tz
        try:
            zone = ZoneInfo(zone_name) if zone_name else start_dt.tzinfo
            event_start = datetime.combine(date.fromisoformat(values["date"]), dtime.min, tzinfo=zone)
            event_end = datetime.combine(date.fromisoformat(ends.get("date") or values["date"]),
                                          dtime.min, tzinfo=zone)
        except (ValueError, KeyError):
            return False
        # All-day end dates are exclusive.
        return event_start < end_dt and event_end > start_dt
    try:
        event_start = datetime.fromisoformat(str(values.get("dateTime", "")).replace("Z", "+00:00"))
        event_end = datetime.fromisoformat(str(ends.get("dateTime", "")).replace("Z", "+00:00"))
    except ValueError:
        return False
    if event_start.tzinfo is None or event_end.tzinfo is None:
        return False  # cannot compare a naive event time safely
    return event_start < end_dt and event_end > start_dt


def _attendee_match(filters, *metas):
    selected = filters.get("attendee_addresses")
    if not selected:
        return True
    for meta in metas:
        if meta and any(attendee["email"] in selected for attendee in meta.get("attendees", [])):
            return True
    return False


def _organizer_match(filters, *metas):
    selected = filters.get("organizer_addresses")
    if not selected:
        return True
    return any(meta and meta.get("organizer") in selected for meta in metas if meta)


def classify(watch, calendar_id, before, after, *, apply_filters=True):
    target = json.loads(watch["target_json"])
    filters = json.loads(watch["filters_json"])
    event_id = (after or before or {}).get("id")
    recurring = (after or before or {}).get("recurringEventId") or ""
    if not _target_match(watch, calendar_id, event_id, recurring):
        return None
    if before is None and after is not None and after.get("status") != "cancelled":
        change = "created"
    elif after is not None and after.get("status") == "cancelled":
        change = "cancelled"
    elif before is not None and after is not None:
        change = "updated"
    else:
        return None
    if not apply_filters:
        return change
    selected = filters.get("change_types")
    if selected and change not in selected:
        return None
    if not _organizer_match(filters, before, after):
        return None
    if not _attendee_match(filters, before, after):
        return None
    if filters.get("summary_contains") and not any(
            W.literal_contains(meta.get("summary") or "", filters["summary_contains"])
            for meta in (before, after) if meta):
        return None
    if filters.get("date_window"):
        if any(meta.get("recurrence") for meta in (before, after) if meta):
            # A recurring master's start is only its first occurrence. Without a
            # bounded instances expansion, window membership cannot be proven.
            return None
        timezone = _calendar_timezone(watch, calendar_id)
        if not any(_window_overlaps(meta, filters["date_window"], timezone)
                   for meta in (before, after) if meta):
            return None
    if change == "updated" and filters.get("changed_fields"):
        selected_people = set(filters.get("attendee_addresses") or [])
        if not _fields_changed(before, after, filters["changed_fields"], selected_people):
            return None
    if change == "updated" and "attendee_responses" in filters.get("changed_fields", []):
        selected_people = set(filters.get("attendee_addresses") or [])
        def responses(meta):
            return {a["email"]: a["responseStatus"] for a in (meta or {}).get("attendees", [])
                    if a["email"] in selected_people}
        prior, current = responses(before), responses(after)
        if not prior or not current:
            return None  # omitted attendee data is unknown, never "declined"/"removed"
        if prior == current:
            return None
    return change


def match(watch, event, client=None):
    if not event_after_start(watch, event):
        return False
    change = classify(watch, event["calendar_id"], event.get("before"), event.get("after"))
    if change is None:
        return False
    event["_change"] = change
    return True


def render(watch, event):
    after = event.get("after") or {}
    before = event.get("before") or {}
    filters = json.loads(watch["filters_json"])
    selected = set(filters.get("attendee_addresses") or [])
    attendees = [{"email": attendee["email"], "responseStatus": attendee["responseStatus"]}
                 for attendee in (after.get("attendees") or before.get("attendees") or [])
                 if attendee["email"] in selected]
    def value(field, fallback=""):
        return after.get(field) or before.get(field) or fallback
    payload = {
        "calendar_id": event["calendar_id"],
        "event_id": value("id"),
        "change": event.get("_change"),
        "status": value("status", "unknown"),
        "summary": (value("summary") or "")[:500],
        "start": after.get("start") or before.get("start") or {},
        "end": after.get("end") or before.get("end") or {},
        "location": (value("location") or "")[:500],
        "organizer": value("organizer"),
        "attendees": attendees[:20],
        "recurring_event_id": value("recurringEventId"),
        "original_start_time": after.get("originalStartTime") or before.get("originalStartTime") or {},
        "updated": value("updated"),
    }
    return payload


def _updated_ts(meta):
    try:
        return datetime.fromisoformat((meta or {}).get("updated", "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _update_baseline(store, watch, calendar_id, event_id, after):
    baseline = json.loads(watch["baseline_json"])
    calendar = baseline.setdefault("calendars", {}).setdefault(calendar_id, {})
    if after is None or after.get("status") == "cancelled":
        calendar.pop(event_id, None)
    else:
        calendar[event_id] = after
    if len(calendar) > MAX_EVENTS:
        ordered = sorted(calendar.items(), key=lambda item: item[1].get("updated") or "")
        baseline["calendars"][calendar_id] = dict(ordered[-MAX_EVENTS:])
    store.update_watch(watch["watch_id"], baseline_json=json.dumps(baseline, sort_keys=True))


def _resync(client, store, source, watches):
    calendar_id = json.loads(source["resource_json"])["calendar_id"]
    try:
        events, sync = _list_initial(client, source["account"], calendar_id)
    except W.WatchError as exc:
        return {"ok": False, "error_code": exc.code, "retryable": False}
    store.apply_events(source["source_id"], [], cursor=sync, clear_staged=True)
    for watch in watches:
        baseline = json.loads(watch["baseline_json"])
        baseline.setdefault("calendars", {})[calendar_id] = events
        store.update_watch(watch["watch_id"], baseline_json=json.dumps(baseline, sort_keys=True),
                           gap="Calendar sync token expired (HTTP 410); the snapshot was rebuilt and "
                               "changes during the gap cannot be recovered")
    return {"ok": True, "resynced": True, "cursor": sync}


def _flush_baselines(store, updates):
    for watch_id, items in updates.items():
        watch = store.get_watch(watch_id)
        if watch is None:
            continue
        for calendar_id, event_id, meta in items:
            _update_baseline(store, watch, calendar_id, event_id, meta)
            watch = store.get_watch(watch_id)


def drain(client, store, source, watches, *, max_pages=10):
    calendar_id = json.loads(source["resource_json"])["calendar_id"]
    cursor = source.get("cursor")
    if not cursor:
        return {"ok": False, "error_code": "cursor_missing"}
    page_token = source.get("cursor_staged")
    baseline_updates = {}
    for _ in range(max_pages):
        params = {"calendarId": calendar_id, "syncToken": cursor, "maxResults": 250,
                  "showDeleted": True, "singleEvents": False, "maxAttendees": 100,
                  "fields": f"nextPageToken,nextSyncToken,items({EVENT_FIELDS})"}
        if page_token:
            params["pageToken"] = page_token
        result = _call(client, source["account"], ["calendar", "events", "list"], params)
        if not result.get("ok"):
            if result.get("http_status") == 410:
                return _resync(client, store, source, watches)
            return {"ok": False, "error_code": result.get("error_code", "calendar_error"),
                    "http_status": result.get("http_status"),
                    "retryable": result.get("retryable", True)}
        data = result.get("data") or {}
        watch_events = []
        for event in data.get("items", []) or []:
            meta = _event_meta(event)
            if not meta["id"]:
                continue
            for watch in watches:
                if watch["service"] != "calendar":
                    continue
                baseline = json.loads(watch["baseline_json"]).get("calendars", {}).get(calendar_id, {})
                before = baseline.get(meta["id"])
                try:
                    scope_change = classify(watch, calendar_id, before, meta, apply_filters=False)
                    if scope_change is None:
                        continue
                    baseline_updates.setdefault(watch["watch_id"], []).append(
                        (calendar_id, meta["id"], meta))
                    change = classify(watch, calendar_id, before, meta)
                except W.WatchError as exc:
                    store.update_watch(watch["watch_id"], gap=exc.message, last_error=exc.message)
                    continue
                if change is None:
                    continue
                wrapper = {"calendar_id": calendar_id, "before": before, "after": meta,
                           "occurred_at": _updated_ts(meta)}
                if not match(watch, wrapper, client):
                    continue  # e.g. a change stamped before the watch started
                payload = render(watch, wrapper)
                identity = json.dumps([calendar_id, meta["id"], meta.get("updated"), change],
                                      sort_keys=True)
                event_id = "calendar:" + watch["watch_id"] + ":" + hashlib.sha256(
                    identity.encode()).hexdigest()[:24]
                watch_events.append((watch["watch_id"], event_id, "calendar", payload))
        next_token = data.get("nextPageToken")
        if next_token:
            store.apply_events(source["source_id"], watch_events, cursor_staged=str(next_token))
            page_token = str(next_token)
            # Keep before-state for the pages already read across a slice.
            _flush_baselines(store, baseline_updates)
            baseline_updates.clear()
            continue
        final = str(data.get("nextSyncToken") or cursor)
        store.apply_events(source["source_id"], watch_events, cursor=final, clear_staged=True)
        store.settle_hints(source["source_id"])
        _flush_baselines(store, baseline_updates)
        baseline_updates.clear()
        return {"ok": True, "events": len({event[1] for event in watch_events}), "cursor": final}
    return {"ok": True, "staged": True, "pages": max_pages}


def tool_schema():
    return {
        "name": TOOL_NAME,
        "description": (
            "Follow exact calendars and optionally exact events or recurring series, with deterministic "
            "organizer/attendee/title/field/date-window filters. Matching runs locally before any model "
            "run; recurrence is never expanded unboundedly. Actions start, status, stop."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "status", "stop"]},
                "account": {"type": "string"},
                "target": {
                    "type": "object",
                    "properties": {
                        "calendar_ids": {"type": "array", "items": {"type": "string"}},
                        "event_ids": {"type": "array", "items": {"type": "string"}},
                        "recurring_event_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["calendar_ids"],
                    "additionalProperties": False,
                },
                "filters": {
                    "type": "object",
                    "properties": {
                        "organizer_addresses": {"type": "array", "items": {"type": "string"}},
                        "attendee_addresses": {"type": "array", "items": {"type": "string"}},
                        "summary_contains": {"type": "array", "items": {"type": "string"}},
                        "change_types": {"type": "array", "items": {
                            "type": "string", "enum": sorted(CHANGE_TYPES)}},
                        "changed_fields": {"type": "array", "items": {
                            "type": "string", "enum": sorted(CHANGED_FIELDS)}},
                        "date_window": {
                            "type": "object",
                            "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
                            "required": ["start", "end"],
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                },
                "delivery_mode": {"type": "string", "enum": ["same_session", "new_session"]},
                "minutes": {"type": "integer", "minimum": 1, "maximum": W.MAX_MINUTES, "default": 60},
                "stop_after_match": {"type": "boolean", "default": False},
                "batch_seconds": {"type": "integer", "minimum": 0, "maximum": W.MAX_BATCH_SECONDS,
                                  "default": 10},
                "instructions": {"type": "string", "maxLength": W.MAX_INSTRUCTIONS},
                "skills": {"type": "array", "items": {"type": "string"}},
                "watch_id": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }
