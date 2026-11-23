"""Gmail targeted watches: exact thread/message/mailbox selectors and history reader."""
from __future__ import annotations


import hashlib
import json
import time
from email.utils import getaddresses

if __package__:
    from . import watch as W
    from .watch_pubsub import validate_config
else:  # Direct module use by the standalone test suite.
    import watch as W
    from watch_pubsub import validate_config

TARGET_KEYS = {"thread_ids", "message_ids", "mailbox"}
FILTER_KEYS = {"from_addresses", "from_domains", "label_ids", "subject_contains",
               "has_attachment", "include_sent", "change_types"}
CHANGE_TYPES = {"messageAdded": "messagesAdded", "messageDeleted": "messagesDeleted",
                "labelAdded": "labelsAdded", "labelRemoved": "labelsRemoved"}
TOOL_NAME = "gws_gmail_watch"
MAX_PAGE = 10
PAGE_SIZE = 500


def _call(client, account, args, params=None, body=None, timeout=30):
    return client.call(account, args, params=params, body=body, timeout=timeout)


def _message_meta(client, account, message_id):
    result = _call(client, account, ["gmail", "users", "messages", "get"], {
        "userId": "me", "id": message_id, "format": "metadata",
        "metadataHeaders": ["From", "Subject", "Date"],
        # Explicit projection: headers/IDs only, never snippet or body data.
        "fields": "id,threadId,internalDate,historyId,labelIds,payload/headers",
    })
    if not result.get("ok"):
        if result.get("http_status") == 404:
            raise W.WatchError("target_not_found", "A selected Gmail message no longer exists or is not accessible")
        raise W.WatchError("gmail_access", "Could not read the selected Gmail message with this account")
    data = result.get("data")
    if not isinstance(data, dict):
        raise W.WatchError("gmail_access", "Gmail returned an unreadable message response")
    return data


def _header(data, name):
    payload = data.get("payload") if isinstance(data, dict) else None
    for header in (payload or {}).get("headers", []) or []:
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def _from_address(data):
    values = getaddresses([_header(data, "From")])
    for _, address in values:
        if address:
            return address.strip().lower()
    return ""


def _has_attachment_tree(part, depth=0):
    if not isinstance(part, dict) or depth > 8:
        return False
    filename = part.get("filename")
    if isinstance(filename, str) and filename.strip():
        return True
    parts = part.get("parts") or []
    for child in parts[:200]:
        if _has_attachment_tree(child, depth + 1):
            return True
    return False


def _attachment_state(client, account, message_id, cache):
    if message_id in cache:
        return cache[message_id]
    result = _call(client, account, ["gmail", "users", "messages", "get"], {
        "userId": "me", "id": message_id, "format": "full",
        "metadataHeaders": ["From", "Subject"],
        "fields": "id,threadId,payload/filename,payload/parts/filename,"
                  "payload/parts/parts/filename,payload/parts/parts/parts/filename",
    })
    if not result.get("ok"):
        if result.get("http_status") == 404:
            cache[message_id] = False  # message vanished between history and fetch
            return False
        if result.get("http_status") in (401, 403):
            raise W.WatchError(
                "scope_insufficient",
                "has_attachment needs gmail.readonly, gmail.modify or mail.google.com without "
                "gmail.metadata; Gmail rejects format=full when the metadata scope is granted")
        raise W.WatchError("gmail_access", "Could not read Gmail MIME metadata for the attachment predicate")
    data = result.get("data") or {}
    cache[message_id] = _has_attachment_tree(data.get("payload") or {})
    return cache[message_id]


def validate_selectors(args):
    target = args.get("target")
    filters = args.get("filters", {})
    if not isinstance(target, dict) or set(target) - TARGET_KEYS:
        raise W.WatchError("invalid_arguments", "gmail target accepts thread_ids, message_ids or mailbox")
    forms = [key for key in TARGET_KEYS if key in target]
    if len(forms) != 1:
        raise W.WatchError("invalid_arguments", "gmail target must be exactly one of thread_ids, message_ids or mailbox")
    if not isinstance(filters, dict) or set(filters) - FILTER_KEYS:
        raise W.WatchError("invalid_arguments", "unknown gmail filter field")
    form = forms[0]
    if form == "mailbox" and target["mailbox"] is not True:
        raise W.WatchError("invalid_arguments", "mailbox must be true when used")
    if form != "mailbox":
        target = {form: W.string_list(target[form], form)}

    clean = {}
    clean["change_types"] = W._clean_change_types(
        filters.get("change_types", ["messageAdded"]), CHANGE_TYPES)
    if "from_addresses" in filters:
        clean["from_addresses"] = W.string_list(
            filters["from_addresses"], "from_addresses", cleaner=W.clean_address)
    if "from_domains" in filters:
        clean["from_domains"] = W.string_list(
            filters["from_domains"], "from_domains", cleaner=W.clean_domain)
    if "label_ids" in filters:
        clean["label_ids"] = W.string_list(filters["label_ids"], "label_ids")
    if "subject_contains" in filters:
        clean["subject_contains"] = W.string_list(
            filters["subject_contains"], "subject_contains",
            cleaner=lambda value, label: W.clean_text(value, label, maximum=200))
    if "has_attachment" in filters:
        if type(filters["has_attachment"]) is not bool:
            raise W.WatchError("invalid_arguments", "has_attachment must be a boolean")
        clean["has_attachment"] = filters["has_attachment"]
    if "include_sent" in filters:
        if type(filters["include_sent"]) is not bool:
            raise W.WatchError("invalid_arguments", "include_sent must be a boolean")
        clean["include_sent"] = filters["include_sent"]
    clean.setdefault("include_sent", False)

    return target, clean


def prepare_start(client, account, args, config):
    started_at = time.time()
    target, clean = validate_selectors(args)
    form = next(iter(target))
    if form == "thread_ids":
        threads = target["thread_ids"]
        for thread_id in threads:
            checked = _call(client, account, ["gmail", "users", "threads", "get"],
                            {"userId": "me", "id": thread_id, "format": "metadata",
                             "fields": "id"})
            if not checked.get("ok"):
                raise W.WatchError("target_not_found",
                                   "A selected Gmail thread does not exist or is not accessible")
        target_resolved = {"thread_ids": threads, "broad": False}
    elif form == "message_ids":
        message_ids = target["message_ids"]
        threads = []
        for message_id in message_ids:
            meta = _message_meta(client, account, message_id)
            thread_id = meta.get("threadId")
            if not isinstance(thread_id, str) or not thread_id:
                raise W.WatchError("gmail_access", "Gmail did not return a thread for a selected message")
            if thread_id not in threads:
                threads.append(thread_id)
        target_resolved = {"thread_ids": threads, "message_ids": message_ids, "broad": False}
    else:
        threads = []
        target_resolved = {"mailbox": True, "broad": not args.get("filters")}

    if "has_attachment" in clean:
        scopes = getattr(client, "scopes", None)
        if callable(scopes):
            granted = scopes(account)
            metadata_scope = "https://www.googleapis.com/auth/gmail.metadata"
            needed = {"https://www.googleapis.com/auth/gmail.readonly",
                      "https://www.googleapis.com/auth/gmail.modify",
                      "https://mail.google.com/"}
            if granted is None:
                raise W.WatchError(
                    "scope_unverified",
                    "has_attachment requires a verifiable Gmail scope; reauthorize with gmail.readonly, "
                    "gmail.modify or mail.google.com so gws auth status can report it")
            # Gmail rejects format=full whenever gmail.metadata is granted, even
            # alongside a broader scope (issuetracker.google.com/issues/115621456),
            # and the metadata format never includes MIME parts. Fail closed.
            if metadata_scope in set(granted) or not needed.intersection(granted):
                raise W.WatchError(
                    "scope_insufficient",
                    "has_attachment needs gmail.readonly, gmail.modify or mail.google.com and a token "
                    "without gmail.metadata; Gmail rejects format=full when the metadata scope is granted")
    validate_config(config)
    topic = config["watch_gmail_topic"]
    topic_parts = topic.split("/")
    project_reader = getattr(client, "project", None)
    if callable(project_reader):
        project_id = project_reader(account)
        if project_id and topic_parts[1] != project_id:
            raise W.WatchError(
                "gmail_topic_project_mismatch",
                "watch_gmail_topic must belong to the OAuth client project reported by gws auth status")


    profile = _call(client, account, ["gmail", "users", "getProfile"], {"userId": "me"})
    if not profile.get("ok") or not isinstance(profile.get("data"), dict):
        raise W.WatchError("gmail_access", "Could not read the Gmail profile/history cursor for this account")
    if profile["data"].get("emailAddress") != account:
        raise W.WatchError("gmail_account_mismatch", "Gmail profile must match the exact registered account")
    history_id = profile["data"].get("historyId")
    if not isinstance(history_id, str) or not history_id.isdigit():
        raise W.WatchError("gmail_access", "Gmail did not return a numeric historyId baseline")
    sources = [{"resource": {"mailbox": "me"}, "query": {}, "cursor": history_id,
                "baseline": {"history_id": history_id}}]
    return {"target": target_resolved, "filters": clean, "sources": sources,
            "baseline": {"history_id": history_id, "thread_ids": threads,
                         "not_before": started_at},
            "resolved": target_resolved}


def _event_floor(watch):
    """Barrier floor: the earlier of record creation and baseline capture.

    Messages that arrived while ``prepare_start`` was reading the profile are in
    the history feed from the captured cursor; using only the later watch row
    timestamp would silently drop them.
    """
    floor = float(watch["not_before"])
    try:
        captured = json.loads(watch.get("baseline_json") or "{}").get("not_before")
    except (TypeError, ValueError):
        captured = None
    if isinstance(captured, (int, float)) and not isinstance(captured, bool):
        floor = min(floor, float(captured))
    return floor


def event_after_start(watch, event):
    history = event.get("history_id")
    baseline = json.loads(watch.get("baseline_json") or "{}").get("history_id")
    if history and baseline and str(history).isdigit() and str(baseline).isdigit():
        if int(history) <= int(baseline):
            return False
    # A label/deletion change on old mail is new even though internalDate is old.
    if event.get("change_type", "messageAdded") != "messageAdded":
        return True
    occurred = event.get("occurred_at")
    if occurred is None:
        return True
    return float(occurred) >= _event_floor(watch)


def match(watch, event, client=None):
    if not event_after_start(watch, event):
        return False
    target = json.loads(watch["target_json"])
    filters = json.loads(watch["filters_json"])
    if event.get("change_type", "messageAdded") not in filters.get("change_types", ["messageAdded"]):
        return False
    if not target.get("mailbox"):
        if event.get("thread_id") not in (target.get("thread_ids") or []):
            return False
    labels = event.get("label_ids") or []
    # Deletion tombstones have no sender/subject/attachment metadata. Explicit
    # metadata predicates fail closed; absent labels also cannot prove not-SENT.
    if event.get("metadata_missing"):
        if any(key in filters for key in ("from_addresses", "from_domains", "subject_contains", "has_attachment")):
            return False
        if not filters.get("include_sent") and not event.get("labels_known"):
            return False
    if not filters.get("include_sent") and any(label in labels for label in ("SENT", "DRAFT")):
        return False
    if filters.get("label_ids") and not any(label in labels for label in filters["label_ids"]):
        return False
    sender = str(event.get("from_address") or "").lower()
    if filters.get("from_addresses") and sender not in filters["from_addresses"]:
        return False
    if filters.get("from_domains"):
        domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
        if domain not in filters["from_domains"]:
            return False
    if filters.get("subject_contains") and not W.literal_contains(
            event.get("subject") or "", filters["subject_contains"]):
        return False
    if "has_attachment" in filters:
        if client is None:
            return False
        if _attachment_state(client, watch["account"], event["message_id"], {}) != filters["has_attachment"]:
            return False
    return True


def render(watch, event):
    payload = {
        "change_type": event.get("change_type", "messageAdded"),
        "history_id": event.get("history_id"),
        "metadata_missing": bool(event.get("metadata_missing")),
        "changed_label_ids": event.get("changed_label_ids", []),
        "message_id": event.get("message_id"),
        "thread_id": event.get("thread_id"),
        "from": event.get("from_address") or "",
        "subject": (event.get("subject") or "")[:500],
        "received_at": W.now_iso(event["occurred_at"]) if event.get("occurred_at") is not None else None,
        "labels": [str(label)[:128] for label in (event.get("label_ids") or [])[:20]],
    }
    if "has_attachment" in event:
        payload["has_attachment"] = bool(event.get("has_attachment"))
    return payload


def _message_event(client, account, message_id, hint_history=None):
    data = _message_meta(client, account, message_id)
    if str(data.get("id") or "") != message_id:
        raise W.WatchError("gmail_access", "Gmail returned a different message identity")
    occurred = int(data["internalDate"]) / 1000 if str(data.get("internalDate") or "").isdigit() else time.time()
    return {
        "message_id": message_id,
        "thread_id": data.get("threadId"),
        "from_address": _from_address(data),
        "subject": _header(data, "Subject"),
        "label_ids": data.get("labelIds") or [],
        "occurred_at": occurred,
        "history_id": str(data.get("historyId") or hint_history or ""),
    }


def drain(client, store, source, watches, *, max_pages=MAX_PAGE):
    """Drain Gmail history from the committed cursor; never reset it on renewal."""
    cursor = source.get("cursor")
    if not cursor:
        return {"ok": False, "error_code": "cursor_missing"}
    page_token = source.get("cursor_staged")
    for _ in range(max_pages):
        # Always read all types. A shared source's filter cannot change between
        # pages when another consumer starts; per-watch filtering happens below.
        params = {"userId": "me", "startHistoryId": cursor, "maxResults": PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        result = _call(client, source["account"], ["gmail", "users", "history", "list"], params)
        if not result.get("ok"):
            if result.get("http_status") == 404:
                profile = _call(client, source["account"], ["gmail", "users", "getProfile"], {"userId": "me"})
                if profile.get("ok") and isinstance(profile.get("data"), dict):
                    new_cursor = profile["data"].get("historyId")
                    if isinstance(new_cursor, str) and new_cursor.isdigit():
                        store.set_cursor(source["source_id"], new_cursor)
                        for watch in watches:
                            store.update_watch(
                                watch["watch_id"], gap="Gmail history expired (HTTP 404); the cursor was "
                                "resynchronized and messages during the unavailable interval cannot be recovered")
                        return {"ok": True, "resynced": True}
            return {"ok": False, "error_code": result.get("error_code", "gmail_error"),
                    "http_status": result.get("http_status"), "retryable": result.get("retryable", True)}
        data = result.get("data") or {}
        changes = []
        for record in data.get("history", []) or []:
            history = str(record.get("id") or "")
            if not history.isdigit():
                return {"ok": False, "error_code": "gmail_history_invalid", "retryable": True}
            for change_type, field in CHANGE_TYPES.items():
                for change in record.get(field, []) or []:
                    changes.append((history, change_type, change))
        watch_events = []
        for history, change_type, change in changes:
            detail = change.get("message") or {}
            message_id = detail.get("id")
            if not isinstance(message_id, str) or not message_id:
                return {"ok": False, "error_code": "gmail_history_invalid", "retryable": True}
            try:
                if change_type == "messageDeleted":
                    event = {"message_id": message_id, "thread_id": detail.get("threadId"),
                             "label_ids": detail.get("labelIds", []), "metadata_missing": True,
                             "labels_known": "labelIds" in detail}
                else:
                    event = _message_event(client, source["account"], message_id)
            except W.WatchError as exc:
                if exc.code == "target_not_found":
                    if change_type == "messageAdded":
                        continue  # legacy arrival behavior for vanished mail
                    event = {"message_id": message_id, "thread_id": detail.get("threadId"),
                             "label_ids": detail.get("labelIds", []), "metadata_missing": True,
                             "labels_known": "labelIds" in detail}
                else:
                    # Transient metadata failures must replay the entire page.
                    store.update_source(source["source_id"], last_error=exc.message)
                    return {"ok": False, "error_code": exc.code, "retryable": True}
            event.update(history_id=history, change_type=change_type,
                         changed_label_ids=change.get("labelIds", []))
            for watch in watches:
                if watch["service"] != "gmail":
                    continue
                try:
                    matched = match(watch, event, client)
                except W.WatchError as exc:
                    if exc.code == "scope_insufficient":
                        store.update_watch(watch["watch_id"], gap=exc.message, last_error=exc.message)
                        continue
                    store.update_source(source["source_id"], last_error=exc.message)
                    return {"ok": False, "error_code": exc.code, "retryable": True}
                if matched:
                    payload = render(watch, event)
                    # Keep legacy arrival IDs stable; label cycles/deletions
                    # require record/type identity, not the message's latest ID.
                    key = source["account"] + ":" + message_id
                    if change_type != "messageAdded":
                        key += ":" + history + ":" + change_type + ":" + json.dumps(sorted(event["changed_label_ids"]))
                    event_id = "gmail:" + watch["watch_id"] + ":" + hashlib.sha256(key.encode()).hexdigest()[:24]
                    watch_events.append((watch["watch_id"], event_id, "gmail", payload))
        history_id = str(data.get("historyId") or "")
        if not history_id.isdigit() or int(history_id) < int(cursor):
            return {"ok": False, "error_code": "gmail_history_invalid", "retryable": True}
        next_token = data.get("nextPageToken")
        if next_token:
            store.apply_events(source["source_id"], watch_events, cursor_staged=str(next_token))
            page_token = str(next_token)
            continue
        final = history_id
        store.apply_events(source["source_id"], watch_events, cursor=final, clear_staged=True)

        return {"ok": True, "events": len(watch_events), "cursor": final}
    return {"ok": True, "staged": True, "pages": max_pages}



def tool_schema():
    return {
        "name": TOOL_NAME,
        "description": (
            "Follow exact Gmail conversations, selected messages, or explicitly broad new-arrival "
            "mail, with literal sender/domain/label/subject/attachment filters. Actions start, status, "
            "stop. Filters run before any model invocation; notifications contain metadata only. "
            "Start requires a trusted gateway conversation and an exact registered account."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "status", "stop"]},
                "account": {"type": "string", "description": "Exact registered Google account"},
                "target": {
                    "type": "object",
                    "description": "Exactly one of thread_ids, message_ids, mailbox",
                    "properties": {
                        "thread_ids": {"type": "array", "items": {"type": "string"}},
                        "message_ids": {"type": "array", "items": {"type": "string"}},
                        "mailbox": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "filters": {
                    "type": "object",
                    "properties": {
                        "from_addresses": {"type": "array", "items": {"type": "string"}},
                        "from_domains": {"type": "array", "items": {"type": "string"}},
                        "label_ids": {"type": "array", "items": {"type": "string"}},
                        "subject_contains": {"type": "array", "items": {"type": "string"}},
                        "has_attachment": {"type": "boolean"},
                        "include_sent": {"type": "boolean", "default": False},
                        "change_types": {"type": "array", "minItems": 1,
                                         "items": {"type": "string", "enum": list(CHANGE_TYPES)},
                                         "default": ["messageAdded"],
                                         "description": "Deletion tombstones lack sender/subject/attachment metadata; those predicates cannot match. Without labels, include_sent=true is required."},
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
