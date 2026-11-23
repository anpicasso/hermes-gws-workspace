"""Drive targeted watches: exact files, direct folders and the explicit account feed."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime

if __package__:
    from . import watch as W
else:  # Direct module use by the standalone test suite.
    import watch as W

TARGET_KEYS = {"file_ids", "folder_id", "all_files"}
FILTER_KEYS = {"name_contains", "mime_types", "change_types"}
CHANGE_TYPES = {"appeared", "updated", "renamed", "moved", "trashed", "restored", "removed"}
TOOL_NAME = "gws_drive_watch"
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_MEMBERS = 5000
FILE_FIELDS = ("id,name,mimeType,parents,version,modifiedTime,trashed,webViewLink,driveId")


def _call(client, account, args, params=None, body=None, timeout=30):
    return client.call(account, args, params=params, body=body, timeout=timeout)


def _file_get(client, account, file_id):
    result = _call(client, account, ["drive", "files", "get"], {
        "fileId": file_id, "fields": FILE_FIELDS, "supportsAllDrives": True,
    })
    if not result.get("ok"):
        if result.get("http_status") == 404:
            raise W.WatchError("target_not_found", "A selected Drive file/folder does not exist or is not accessible")
        raise W.WatchError("drive_access", "Could not read the selected Drive resource with this account")
    data = result.get("data")
    if not isinstance(data, dict) or data.get("id") != file_id:
        raise W.WatchError("drive_access", "Drive returned an unreadable or mismatched file response")
    return _file_meta(data)


def _file_meta(data):
    parents = data.get("parents") if isinstance(data.get("parents"), list) else []
    return {
        "id": data.get("id"), "name": data.get("name") or "",
        "mimeType": data.get("mimeType") or "",
        "parents": [str(parent) for parent in parents[:20]],
        "version": str(data.get("version") or ""),
        "modifiedTime": data.get("modifiedTime") or "",
        "trashed": bool(data.get("trashed")),
        "webViewLink": data.get("webViewLink") or "",
        "driveId": data.get("driveId") or "",
    }


def _list_children(client, account, folder_id, drive_id):
    children, token = {}, None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "pageSize": 1000, "fields": f"nextPageToken,files({FILE_FIELDS})",
            "supportsAllDrives": True, "includeItemsFromAllDrives": True,
        }
        if drive_id:
            params.update({"driveId": drive_id, "corpora": "drive"})
        if token:
            params["pageToken"] = token
        result = _call(client, account, ["drive", "files", "list"], params)
        if not result.get("ok"):
            raise W.WatchError("drive_access", "Could not snapshot the selected Drive folder")
        data = result.get("data") or {}
        for file in data.get("files", []) or []:
            meta = _file_meta(file)
            if meta["id"] and len(children) < MAX_MEMBERS:
                children[meta["id"]] = meta
        token = data.get("nextPageToken")
        if not token:
            return children


def _start_token(client, account, drive_id):
    params = {"supportsAllDrives": True}
    if drive_id:
        params["driveId"] = drive_id
    result = _call(client, account, ["drive", "changes", "getStartPageToken"], params)
    if not result.get("ok") or not isinstance(result.get("data"), dict):
        raise W.WatchError("drive_access", "Could not obtain a Drive changes start token for this account")
    token = result["data"].get("startPageToken")
    if not isinstance(token, str) or not token:
        raise W.WatchError("drive_access", "Drive did not return a start page token")
    return token


def validate_selectors(args):
    target = args.get("target")
    filters = args.get("filters", {})
    if not isinstance(target, dict) or set(target) - TARGET_KEYS:
        raise W.WatchError("invalid_arguments", "drive target accepts file_ids, folder_id or all_files")
    forms = [key for key in TARGET_KEYS if key in target]
    if len(forms) != 1:
        raise W.WatchError("invalid_arguments", "drive target must be exactly one of file_ids, folder_id or all_files")
    if not isinstance(filters, dict) or set(filters) - FILTER_KEYS:
        raise W.WatchError("invalid_arguments", "unknown drive filter field")
    if args.get("shared_drive_id") is not None:
        W.clean_id(args["shared_drive_id"], "shared_drive_id")
    clean = {}
    if "name_contains" in filters:
        clean["name_contains"] = W.string_list(
            filters["name_contains"], "name_contains",
            cleaner=lambda value, label: W.clean_text(value, label, maximum=200))
    if "mime_types" in filters:
        mime_types = W.string_list(
            filters["mime_types"], "mime_types",
            cleaner=lambda value, label: W.clean_text(value, label, maximum=200))
        for mime in mime_types:
            if not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", mime):
                raise W.WatchError("invalid_arguments", "mime_types entries must be exact type/subtype strings")
        clean["mime_types"] = mime_types
    if "change_types" in filters:
        clean["change_types"] = W._clean_change_types(filters["change_types"], CHANGE_TYPES)

    form = forms[0]
    if form == "all_files" and target[form] is not True:
        raise W.WatchError("invalid_arguments", "all_files must be true when used")
    if form == "folder_id":
        W.clean_id(target[form], form)
    if form == "file_ids":
        target = {form: W.string_list(target[form], form)}
    return target, clean


def prepare_start(client, account, args, config):
    started_at = time.time()
    target, clean = validate_selectors(args)
    drive_id = args.get("shared_drive_id")
    if not config.get("watch_callback_url"):
        raise W.WatchError(
            "drive_callback_missing",
            "Set watch_callback_url to the public HTTPS base that reaches the worker before starting a Drive watch")

    form = next(iter(target))
    # The change token must predate the snapshot; changes racing the snapshot are
    # then replayed and reconciled instead of falling between baseline and token.
    cursor = _start_token(client, account, drive_id)
    baseline = {}
    if form == "all_files":
        target_resolved = {"all_files": True, "broad": not clean}
        baseline = {"seen": {}, "not_before": started_at}
    elif form == "folder_id":
        folder_id = target["folder_id"]
        folder = _file_get(client, account, folder_id)
        if folder["mimeType"] != FOLDER_MIME:
            raise W.WatchError("not_a_folder", "folder_id must name a Drive folder (direct children only)")
        if drive_id and folder["driveId"] != drive_id:
            raise W.WatchError("drive_mismatch", "folder_id is not in the selected shared drive")
        baseline = {"folder_id": folder_id, "not_before": started_at,
                    "members": _list_children(client, account, folder_id, drive_id)}
        target_resolved = {"folder_id": folder_id, "direct_children_only": True,
                           "broad": False, "shared_drive_id": drive_id}
    else:
        file_ids = target["file_ids"]
        files = {}
        for file_id in file_ids:
            meta = _file_get(client, account, file_id)
            if drive_id and meta["driveId"] != drive_id:
                raise W.WatchError("drive_mismatch", "a selected file is not in the selected shared drive")
            files[file_id] = meta
        baseline = {"files": files, "not_before": started_at}
        target_resolved = {"file_ids": file_ids, "broad": False, "shared_drive_id": drive_id}

    source = {"resource": {"drive_id": drive_id or ""},
              "query": {"drive_id": drive_id or "", "supports_all_drives": True},
              "cursor": cursor, "baseline": {"start_token": cursor}}
    return {"target": target_resolved, "filters": clean, "sources": [source],
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


def _name_or_type_matches(filters, *metas):
    names = filters.get("name_contains")
    types = filters.get("mime_types")
    if names and not any(W.literal_contains(meta.get("name") or "", names) for meta in metas if meta):
        return False
    if types and not any((meta.get("mimeType") or "") in types for meta in metas if meta):
        return False
    return True


def _categories(before, after):
    if before is None and after is None:
        return set()
    if before is None:
        return {"appeared"}
    if after is None:
        return {"removed"}
    categories = set()
    if before.get("name") != after.get("name"):
        categories.add("renamed")
    if before.get("parents") != after.get("parents"):
        categories.add("moved")
    if not before.get("trashed") and after.get("trashed"):
        categories.add("trashed")
    if before.get("trashed") and not after.get("trashed"):
        categories.add("restored")
    for field in ("version", "modifiedTime", "mimeType"):
        if before.get(field) != after.get(field):
            categories.add("updated")
    if not categories:
        categories.add("updated")
    return categories


def classify(watch, file_id, after, removed, *, apply_filters=True):
    target = json.loads(watch["target_json"])
    filters = json.loads(watch["filters_json"])
    baseline = json.loads(watch["baseline_json"])
    before = None
    in_scope = False
    if target.get("folder_id"):
        before = (baseline.get("members") or {}).get(file_id)
        in_folder = bool(after and not removed
                         and target["folder_id"] in (after.get("parents") or []))
        if removed or after is None:
            if before is None:
                return None  # never observed as a direct child
            categories = {"removed"}
        elif in_folder:
            categories, in_scope = _categories(before, after), True
        elif before is not None:
            # The file is still accessible elsewhere: an exit from the watched
            # folder, not a deletion or access loss.
            categories = _categories(before, after) | {"moved"}
        else:
            return None
    elif target.get("file_ids"):
        if file_id not in target["file_ids"]:
            return None
        before = (baseline.get("files") or {}).get(file_id)
        if removed or after is None:
            categories = {"removed"}
        else:
            categories, in_scope = _categories(before, after), True
    else:
        before = (baseline.get("seen") or {}).get(file_id)
        if removed or after is None:
            # The explicit broad feed reports removals even when the file was
            # never in the bounded seen cache; sparse metadata may still satisfy
            # name/type filters.
            categories = {"removed"}
        else:
            categories, in_scope = _categories(before, after), True
    if not categories:
        return None
    if apply_filters:
        if not _name_or_type_matches(filters, before, after):
            return None
        selected = filters.get("change_types")
        if selected and not categories.intersection(selected):
            return None
    return {"before": before, "after": after, "in_scope": in_scope, "categories": categories}


def match(watch, event, client=None):
    if not event_after_start(watch, event):
        return False
    result = classify(watch, event["file_id"], event.get("_after"), event.get("removed", False))
    if result is None:
        return False
    event["_categories"] = sorted(result["categories"])
    event["_after_matched"] = result["after"]
    event["_before_matched"] = result["before"]
    event["_in_scope"] = result["in_scope"]
    return True


def render(watch, event):
    after = event.get("_after_matched", event.get("_after"))
    before = event.get("_before_matched")
    removed = bool(event.get("removed")) or after is None
    shown = (before or after or {}) if removed else (after or before or {})
    categories = event.get("_categories") or []
    payload = {
        "file_id": event["file_id"],
        "categories": categories,
        "removed": removed,
        "name": (shown.get("name") or "")[:500],
        "mime_type": shown.get("mimeType") or "",
        "parents": [str(parent)[:256] for parent in (shown.get("parents") or [])[:20]],
        "trashed": bool(shown.get("trashed")),
        "modified_at": shown.get("modifiedTime") or "",
        "web_view_link": shown.get("webViewLink") or "",
    }
    if removed:
        payload["detail"] = "removed or no longer accessible; the API did not prove a narrower cause"
    return payload


def _update_baseline(store, watch, file_id, match_result):
    baseline = json.loads(watch["baseline_json"])
    target = json.loads(watch["target_json"])
    bucket_name = "members" if target.get("folder_id") else ("files" if target.get("file_ids") else "seen")
    bucket = baseline.setdefault(bucket_name, {})
    after = match_result.get("after") if match_result.get("in_scope") else None
    if after is None:
        bucket.pop(file_id, None)
    else:
        bucket[file_id] = after
    if len(bucket) > MAX_MEMBERS:
        # Bounded snapshot: keep the newest metadata; an evicted file is treated as newly
        # appeared on its next change rather than silently promising forensic history.
        ordered = sorted(bucket.items(), key=lambda item: item[1].get("modifiedTime") or "")
        baseline[bucket_name] = dict(ordered[-MAX_MEMBERS:])
    store.update_watch(watch["watch_id"], baseline_json=json.dumps(baseline, sort_keys=True))


def _flush_baselines(store, updates):
    for watch_id, items in updates.items():
        watch = store.get_watch(watch_id)
        if watch is None:
            continue
        for file_id, result in items:
            _update_baseline(store, watch, file_id, result)
            watch = store.get_watch(watch_id)


def drain(client, store, source, watches, *, max_pages=10):
    token = source.get("cursor")
    if not token:
        return {"ok": False, "error_code": "cursor_missing"}
    drive_id = (json.loads(source["resource_json"]).get("drive_id") or "")
    page_token = source.get("cursor_staged") or token
    baseline_updates = {}
    for _ in range(max_pages):
        params = {
            "pageToken": page_token, "pageSize": 1000, "includeRemoved": True,
            "includeItemsFromAllDrives": True, "supportsAllDrives": True,
            "fields": "nextPageToken,newStartPageToken,changes(changeType,fileId,removed,time,"
                      f"file({FILE_FIELDS}),driveId)",
        }
        if drive_id:
            params["driveId"] = drive_id
        result = _call(client, source["account"], ["drive", "changes", "list"], params)
        if not result.get("ok"):
            return {"ok": False, "error_code": result.get("error_code", "drive_error"),
                    "http_status": result.get("http_status"),
                    "retryable": result.get("retryable", True),
                    "resync_required": result.get("http_status") in (400, 410)}
        data = result.get("data") or {}
        watch_events = []
        for index, change in enumerate(data.get("changes", []) or []):
            file_id = change.get("fileId")
            if not isinstance(file_id, str) or not file_id:
                continue
            raw_file = change.get("file") if isinstance(change.get("file"), dict) else None
            removed = bool(change.get("removed")) or raw_file is None
            # Removed changes may still carry sparse file metadata (name/type),
            # which is used for filters and prior-state explanations.
            after = _file_meta(raw_file) if raw_file else None
            for watch in watches:
                if watch["service"] != "drive":
                    continue
                event = {"file_id": file_id, "_after": after, "removed": removed,
                         "occurred_at": _change_time(change.get("time"), after)}
                try:
                    scope = classify(watch, file_id, after, removed, apply_filters=False)
                    if scope is None:
                        continue
                    baseline_updates.setdefault(watch["watch_id"], []).append((file_id, scope))
                    matched = match(watch, event, client)
                except W.WatchError as exc:
                    store.update_watch(watch["watch_id"], gap=exc.message, last_error=exc.message)
                    continue
                if not matched:
                    continue
                payload = render(watch, event)
                identity = json.dumps([file_id, removed, (after or {}).get("version"),
                                       (after or {}).get("modifiedTime"), page_token, index],
                                      sort_keys=True)
                event_id = "drive:" + watch["watch_id"] + ":" + hashlib.sha256(
                    identity.encode()).hexdigest()[:24]
                watch_events.append((watch["watch_id"], event_id, "drive", payload))
        next_token = data.get("nextPageToken")
        new_start = data.get("newStartPageToken")
        if next_token:
            store.apply_events(source["source_id"], watch_events, cursor_staged=str(next_token))
            page_token = str(next_token)
            # Persist membership for this page before slicing; otherwise a
            # long pagination loses before-state for the pages already read.
            _flush_baselines(store, baseline_updates)
            baseline_updates.clear()
            continue
        final = str(new_start or token)
        store.apply_events(source["source_id"], watch_events, cursor=final, clear_staged=True)
        store.settle_hints(source["source_id"])
        _flush_baselines(store, baseline_updates)
        baseline_updates.clear()
        return {"ok": True,
                "events": len({event[1] for event in watch_events}),
                "cursor": final}
    return {"ok": True, "staged": True, "pages": max_pages}


def _change_time(value, after):
    for candidate in (value, (after or {}).get("modifiedTime")):
        if not candidate:
            continue
        try:
            return datetime.fromisoformat(str(candidate).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return time.time()


def tool_schema():
    return {
        "name": TOOL_NAME,
        "description": (
            "Follow exact Drive files, direct children of one folder, or an explicitly broad account "
            "feed. Filters are literal filename/MIME/change-type selectors evaluated locally before any "
            "model run. Folder watches are not recursive; removals mean deleted or no longer accessible. "
            "Actions start, status, stop."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "status", "stop"]},
                "account": {"type": "string"},
                "target": {
                    "type": "object",
                    "properties": {
                        "file_ids": {"type": "array", "items": {"type": "string"}},
                        "folder_id": {"type": "string"},
                        "all_files": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "filters": {
                    "type": "object",
                    "properties": {
                        "name_contains": {"type": "array", "items": {"type": "string"}},
                        "mime_types": {"type": "array", "items": {"type": "string"}},
                        "change_types": {"type": "array", "items": {
                            "type": "string", "enum": sorted(CHANGE_TYPES)}},
                    },
                    "additionalProperties": False,
                },
                "shared_drive_id": {"type": "string",
                                    "description": "Exact shared drive ID when the target lives there"},
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
