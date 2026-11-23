"""Strict shared lifecycle and slash-command adapter for Workspace watches."""
from __future__ import annotations

import json
import secrets

if __package__:
    from . import watch as W
    from . import watch_calendar, watch_drive, watch_gmail, watch_routes
    from .account_lifecycle import lifecycle_guard
else:  # Direct module use by the standalone test suite.
    import watch as W  # type: ignore
    import watch_calendar, watch_drive, watch_gmail, watch_routes  # type: ignore
    from account_lifecycle import lifecycle_guard

SERVICES = {
    "gmail": watch_gmail,
    "drive": watch_drive,
    "calendar": watch_calendar,
}

_START_FIELDS = {
    "gmail": {"action", "account", "target", "filters", "delivery_mode", "minutes",
              "stop_after_match", "batch_seconds", "instructions", "skills"},
    "drive": {"action", "account", "target", "filters", "delivery_mode", "minutes",
              "stop_after_match", "batch_seconds", "instructions", "skills", "shared_drive_id"},
    "calendar": {"action", "account", "target", "filters", "delivery_mode", "minutes",
                 "stop_after_match", "batch_seconds", "instructions", "skills"},
}


COMMAND_HELP = '''Usage: /gws-watch <gmail|drive|calendar> <JSON>
Start: /gws-watch gmail {"action":"start","account":"person@example.com","target":{"mailbox":true},"delivery_mode":"same_session","minutes":30}
Status: /gws-watch gmail {"action":"status"} (use returned next_offset as offset for the next page, or watch_id for selectors)
Stop: /gws-watch gmail {"action":"stop","watch_id":"EXACT_ID"}
/gws_watch is also accepted by the messaging gateway. Send a normal message first in a new chat.
Use exact service selectors; this command does not interpret natural language. Ask the agent to draft the JSON, then send it yourself. Worker, account and operator delivery-route setup are required.'''


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def parse_command(raw):
    if not isinstance(raw, str) or len(raw) > 16000:
        raise W.WatchError("invalid_arguments", "Command must be text of at most 16000 characters")
    if raw.strip() in ("", "help", "-h", "--help"):
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0] not in SERVICES:
        raise W.WatchError("invalid_arguments", "Use /gws-watch <gmail|drive|calendar> <JSON>; /gws-watch help shows examples")
    try:
        args = json.loads(parts[1], object_pairs_hook=_unique_fields)
        if not isinstance(args, dict):
            raise ValueError("Expected object")
        json.dumps(args, allow_nan=False)
    except (ValueError, RecursionError):
        raise W.WatchError("invalid_arguments", "Supply one valid JSON object without duplicate fields or non-finite numbers") from None
    return parts[0], args


def command_reply(result):
    """Render small selectors losslessly and mark larger values as previews."""
    def compact(value):
        return json.dumps(value, separators=(",", ":"))

    if not result.get("ok"):
        error = str(result.get("error", "Watch operation failed"))
        if len(encoded := compact(error)) > 800:
            error = encoded[1:401] + "... [truncated]"
        return compact({"ok": False, "error_code": result.get("error_code"), "error": error})
    reply = {key: result[key] for key in ("ok", "action", "already_active", "local_revoked", "remote_cleanup", "note",
                                          "total", "has_more", "next_offset", "watches") if key in result}
    if "watch" in result:
        row = result["watch"]
        detail = {k: row[k] for k in ("watch_id", "service", "state", "delivery_mode", "expires_at", "remote_cleanup")}
        for key, budget in (("account", 260), ("delivery", 220), ("gap", 120), ("last_error", 160),
                            ("target", 340), ("filters", 340)):
            text = compact(row[key])
            if len(text) <= budget:
                detail[key] = row[key]
            else:
                # A preview is quoted again in the reply; escaped ASCII can double in size.
                detail[key + "_preview"] = text[:budget // 2] + "..."
        reply["watch"] = detail
    return compact(reply)


def _error(exc):
    if isinstance(exc, W.WatchError):
        return exc.as_dict()
    if isinstance(exc, watch_routes.RouteError):
        return {"ok": False, "error_code": exc.code, "error": exc.message}
    return {"ok": False, "error_code": "internal_error",
            "error": "The watch operation failed; inspect the worker status and local logs."}


def _same_route(watch, origin):
    bound = json.loads(watch["origin_json"])
    return all(bound.get(key) == origin.get(key) for key in
               ("profile", "principal", "session_key", "platform", "chat_id", "thread_id"))


def _owned(store, watch_id, service, origin):
    watch = store.get_watch(watch_id)
    if (watch is None or watch["service"] != service or watch["profile"] != origin["profile"]
            or watch["principal"] != origin["principal"] or not _same_route(watch, origin)):
        raise W.WatchError("not_found", "No accessible watch with that ID for this service")
    return watch


def handle(service, args, runtime, *, origin):
    """Fence watch creation against companion logout/reauthorization.

    Lock order: lifecycle -> native execution locks -> short SQLite transaction.
    Status and stop do not acquire this barrier, so cleanup can still progress.
    """
    try:
        if isinstance(args, dict) and args.get("action") == "start":
            common = W.validate_common(args)
            runtime.routes.preflight(origin, common["mode"], common["skills"])
            with lifecycle_guard(runtime.store.home / "gws-oauth", common["account"]):
                return _handle(service, args, runtime, origin=origin)
        return _handle(service, args, runtime, origin=origin)
    except Exception as exc:
        return _error(exc)


def _handle(service, args, runtime, *, origin):
    """Dispatch a command using its explicitly captured origin."""
    module = SERVICES[service]
    if not isinstance(args, dict):
        return {"ok": False, "error_code": "invalid_arguments", "error": "arguments must be an object"}
    action = args.get("action")
    if action not in ("start", "status", "stop"):
        return {"ok": False, "error_code": "invalid_arguments", "error": "action must be start, status or stop"}
    allowed = _START_FIELDS[service] if action == "start" else {"action", "watch_id"}
    if action == "status" and "watch_id" not in args:
        allowed = {"action", "offset"}
    unknown = set(args) - allowed
    if unknown:
        return {"ok": False, "error_code": "invalid_arguments",
                "error": f"unknown or action-inappropriate fields: {', '.join(sorted(unknown))}"}
    try:
        store = runtime.store
        store.expire_due()
        if action == "start":
            if "account" not in args or "target" not in args or "delivery_mode" not in args:
                raise W.WatchError("invalid_arguments",
                                   "start requires account, target and delivery_mode")
            common = W.validate_common(args)
            if common["mode"] == "same_session":
                capability = watch_routes.same_session_capability(runtime.ctx)
                if not capability["supported"]:
                    raise W.WatchError(capability["error_code"], capability["message"])
            prepared = module.prepare_start(runtime.client, common["account"], args,
                                            runtime.config)
            spec = {
                **common,
                "target": prepared["target"], "filters": prepared["filters"],
                "fingerprint": W.fingerprint(service, common["account"], origin, {
                    **common, "target": prepared["target"], "filters": prepared["filters"],
                }),
            }
            existing = store.find_active(spec["fingerprint"])
            if existing is not None:
                return {"ok": True, "action": "start", "already_active": True,
                        "watch": W.watch_status(store, existing)}
            watch_id = secrets.token_hex(8)
            stub = {"watch_id": watch_id, "service": service, "account": spec["account"],
                    "generation": 1}
            route = runtime.routes.create(stub, spec, origin)
            route_name = route["name"] if spec["mode"] == "new_session" else None
            bridge = route["name"] if spec["mode"] == "same_session" else None
            watch = None
            try:
                watch = W.create_watch(store, service, spec, origin, watch_id=watch_id,
                                       route_name=route_name, bridge_name=bridge,
                                       sources=prepared["sources"],
                                       baseline=prepared["baseline"])
                store.add_remote("route:" + watch_id, "native_route", watch_id=watch_id,
                                 identity={"name": route["name"], "mode": spec["mode"]},
                                 state="active")
            except Exception as exc:
                existing = store.get_watch(watch_id)
                if existing is not None:
                    store.stop(watch_id)
                    store.update_watch(watch_id, cleanup_state="pending",
                                       last_error="partial start compensation: " + type(exc).__name__)
                try:
                    runtime.routes.remove(route["name"], watch_id)
                except Exception:
                    pass
                raise
            return {"ok": True, "action": "start", "already_active": False,
                    "watch": W.watch_status(store, watch),
                    "resolved": prepared["resolved"]}
        if action == "stop":
            watch_id = args.get("watch_id")
            if not isinstance(watch_id, str) or not watch_id:
                raise W.WatchError("invalid_arguments", "stop requires an exact watch_id")
            watch = _owned(store, watch_id, service, origin)
            stopped = store.stop(watch_id)
            return {"ok": True, "action": "stop", "watch": W.watch_status(store, stopped),
                    "local_revoked": True,
                    "remote_cleanup": "pending",
                    "note": "Stop removes the local watch binding; operator routes are preserved. Already admitted or sent output cannot be undone."}
        watch_id = args.get("watch_id")
        if "watch_id" in args:
            if not isinstance(watch_id, str) or not watch_id:
                raise W.WatchError("invalid_arguments", "watch_id must be an exact non-empty ID")
            watch = _owned(store, watch_id, service, origin)
            return {"ok": True, "action": "status", "watch": W.watch_status(store, watch)}
        offset = args.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise W.WatchError("invalid_arguments", "offset must be a non-negative integer")
        rows = store.list_watches(service=service, profile=origin["profile"],
                                  principal=origin["principal"])
        rows = [row for row in rows if _same_route(row, origin)]
        end = offset + 10
        return {"ok": True, "action": "status", "total": len(rows), "has_more": end < len(rows),
                "next_offset": end if end < len(rows) else None,
                "watches": [{"watch_id": row["watch_id"], "state": row["state"], "delivery_mode": row["mode"],
                             "expires_at": W.now_iso(row["expires_at"])} for row in rows[offset:end]]}
    except Exception as exc:
        return _error(exc)


def bridge_callback(store, ctx, routes=None, *, profile="default"):
    return watch_routes.bridge_hook(store, ctx, routes, profile=profile)
