"""One explicit, supervised GWS watch worker per Hermes profile.

Run it in the foreground or under a user service; never import-spawn it from
plugin registration. It owns Google subscriptions, durable deltas, callbacks,
renewal, cleanup and forwarding to native Hermes routes.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import sys
import time
from collections import defaultdict
from contextlib import suppress
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - aiohttp is an existing Hermes dependency
    web = None

if __package__:
    from . import watch as W
    from . import watch_calendar, watch_drive, watch_gmail, watch_routes
    from .account_lifecycle import lifecycle_guard
    from .router import bounded_gws
    from . import watch_pubsub
else:  # Direct module use by the standalone test suite.
    from account_lifecycle import lifecycle_guard
    from router import bounded_gws  # type: ignore
    import watch_pubsub
    import watch as W  # type: ignore
    import watch_calendar, watch_drive, watch_gmail, watch_routes  # type: ignore

logger = logging.getLogger("plugins.gws_oauth.watch_worker")

TOOL = {"gmail": watch_gmail, "drive": watch_drive, "calendar": watch_calendar}
CHANNEL_KIND = {"drive": "drive_channel", "calendar": "calendar_channel"}
MAX_BODY = 1024
LEASE_SECONDS = {"gmail": 6 * 86400, "drive": 6 * 86400, "calendar": 6 * 86400}


class GwsClient:
    """Bounded native Workspace calls; native gws exclusively owns OAuth."""

    def __init__(self, home, *, gws_bin="gws", timeout=30):
        self.root = Path(home).expanduser().resolve() / "gws-oauth"
        self.gws_bin = gws_bin
        self.timeout = timeout


    def call(self, account, args, params=None, body=None, timeout=None):

        argv = list(args)
        if params is not None:
            argv += ["--params", json.dumps(params, separators=(",", ":"), sort_keys=True)]
        if body is not None:
            argv += ["--json", json.dumps(body, separators=(",", ":"), sort_keys=True)]
        return bounded_gws(self.root, account, argv, gws_bin=self.gws_bin,
                           timeout=timeout or self.timeout, max_output=2_000_000)

    def scopes(self, account):
        result = bounded_gws(self.root, account, ["auth", "status"], gws_bin=self.gws_bin,
                             timeout=10, max_output=200_000)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return None
        scopes = result["data"].get("scopes")
        return [str(scope) for scope in scopes] if isinstance(scopes, list) else None

    def project(self, account):
        result = bounded_gws(self.root, account, ["auth", "status"], gws_bin=self.gws_bin,
                             timeout=10, max_output=200_000)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return None
        project = result["data"].get("project_id") or result["data"].get("project")
        return str(project) if isinstance(project, str) and project else None


class SingleInstance:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        if fcntl is None:
            # ponytail: no portable lock for this platform; refuse to pretend a
            # second worker is excluded, since the shared outbox would corrupt.
            raise RuntimeError("Single worker locking is unsupported on this platform")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        self.handle = self.path.open("a+")
        try:
            if os.name != "nt":
                os.chmod(self.path, 0o600)
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another gws watch worker already owns this profile") from exc
        except Exception:
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            if fcntl is not None:
                with suppress(OSError):
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def process_callback(store, headers, body=b""):
    """Pure callback validation + durable hint write. Returns (status, JSON)."""
    if len(body) > MAX_BODY:
        return 413, {"ok": False, "error": "body too large"}
    channel_id = str(headers.get("X-Goog-Channel-ID") or "").strip()
    token = str(headers.get("X-Goog-Channel-Token") or "").strip()
    resource_id = str(headers.get("X-Goog-Resource-ID") or "").strip()
    state = str(headers.get("X-Goog-Resource-State") or "").strip()
    if not channel_id or not token:
        return 400, {"ok": False, "error": "missing channel identity"}
    remote = store.get_remote("channel:" + channel_id)
    if remote is None:
        return 404, {"ok": False, "error": "unknown channel"}
    identity = json.loads(remote["identity_json"])
    if not hmac.compare_digest(str(identity.get("token") or ""), token):
        return 403, {"ok": False, "error": "bad channel token"}
    if resource_id and identity.get("resource_id") and resource_id != identity["resource_id"]:
        return 400, {"ok": False, "error": "resource mismatch"}
    message_number = str(headers.get("X-Goog-Message-Number") or "").strip()
    hint_id = json.dumps([channel_id, message_number, resource_id, state], separators=(",", ":"))
    try:
        store.add_hint(hint_id, remote["source_id"],
                       {"state": state, "resource_id": resource_id, "channel_id": channel_id})
    except Exception:
        return 503, {"ok": False, "error": "durable hint write failed"}
    return 200, {"ok": True, "sync": state == "sync"}


class CallbackServer:
    def __init__(self, worker, host="127.0.0.1", port=8765):
        self.worker = worker
        self.host = host
        self.port = port
        self.runner = None

    async def start(self):
        if self.host not in ("127.0.0.1", "::1"):
            raise ValueError("CallbackServer must bind a loopback address")
        if web is None:
            raise RuntimeError("aiohttp is unavailable; Google callbacks cannot be received")
        app = web.Application(client_max_size=watch_pubsub.MAX_BODY)
        app.router.add_post("/google/gmail", self._gmail)
        app.router.add_post("/google/drive", self._handle)
        app.router.add_post("/google/calendar", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def _handle(self, request):
        raw = await request.read()
        status, payload = await asyncio.to_thread(
            process_callback, self.worker.store, request.headers, raw)
        return web.json_response(payload, status=status)

    async def _gmail(self, request):
        assert web is not None
        raw = await request.read()
        status, payload = await asyncio.to_thread(
            watch_pubsub.process_push, self.worker.store, self.worker.config, request.headers, raw)
        return web.Response(status=204) if status == 204 else web.json_response(payload, status=status)


class Worker:
    def __init__(self, home, client, routes, config, *, store=None, poster=None, clock=time.time):
        self.home = Path(home).expanduser().resolve()
        self.client = client
        self.routes = routes
        self.config = dict(config)
        self.store = store or W.WatchStore(self.home, clock=clock)
        self.poster = poster or routes.post
        self.clock = clock
        self.reconcile_seconds = int(config.get("watch_reconcile_seconds", 300))
        self.subscription_backoff_max = int(config.get("watch_subscription_backoff_max", 900))
        self.retention_seconds = int(config.get("watch_retention_days", 7)) * 86400
        self._last_prune = None
        self.stopping = False

    # --- subscriptions ---------------------------------------------------
    def _channel(self, source, kind):
        channel_id = "gws-" + source["source_id"][:12] + "-" + secrets.token_hex(4)
        token = secrets.token_urlsafe(32)
        remote_id = "channel:" + channel_id
        self.store.add_remote(remote_id, kind, source_id=source["source_id"], state="pending",
                              identity={"token": token, "channel_id": channel_id,
                                        "resource_id": "", "source_id": source["source_id"]})
        self.store.update_source(source["source_id"], channel_id=channel_id,
                                 channel_token=token)
        return remote_id, channel_id, token

    def _address(self, service):
        base = str(self.config.get("watch_callback_url") or "").rstrip("/")
        if not base:
            return None
        return base + "/google/" + service

    def _ensure_gmail(self, source):
        try:
            watch_pubsub.validate_config(self.config)
        except W.WatchError as exc:
            self.store.update_source(source["source_id"], last_error=exc.message)
            return False
        topic = self.config["watch_gmail_topic"]
        remote = self.store.remote_for_source(source["source_id"], "gmail_watch")
        now = self.clock()
        if remote and (remote.get("expires_at") or 0) > now + 86400:
            return True
        result = self.client.call(source["account"], ["gmail", "users", "watch"], {"userId": "me"},
                                  body={"topicName": topic})
        if not result.get("ok"):
            self.store.update_source(source["source_id"], last_error=result.get("error", "Gmail watch failed"))
            return False
        data = result.get("data") or {}
        expiration = _expiry(data.get("expiration"))
        remote_id = "gmail:" + source["source_id"]
        self.store.add_remote(remote_id, "gmail_watch", source_id=source["source_id"],
                              identity={"historyId": str(data.get("historyId") or "")},
                              state="active", expires_at=expiration)
        self.store.update_source(source["source_id"], expires_at=expiration, last_error=None,
                                 **({"cursor": str(data["historyId"])}
                                    if not source.get("cursor") and str(data.get("historyId") or "").isdigit()
                                    else {}))
        return True

    def _ensure_channel(self, source, service):
        remote = self.store.remote_for_source(source["source_id"], CHANNEL_KIND[service])
        now = self.clock()
        if remote and (remote.get("expires_at") or 0) > now + 86400:
            return True
        address = self._address(service)
        if not address:
            self.store.update_source(source["source_id"], last_error="watch_callback_url is not configured")
            return False
        remote_id, channel_id, token = self._channel(source, CHANNEL_KIND[service])
        expiration = int((now + LEASE_SECONDS[service]) * 1000)
        body = {"id": channel_id, "type": "web_hook", "address": address,
                "token": token, "expiration": str(expiration)}
        params = {}
        if service == "drive":
            params = {"pageToken": source.get("cursor"), "includeItemsFromAllDrives": True,
                      "supportsAllDrives": True}
            drive_id = json.loads(source["resource_json"]).get("drive_id")
            if drive_id:
                params["driveId"] = drive_id
            result = self.client.call(source["account"], ["drive", "changes", "watch"], params, body=body)
        else:
            calendar_id = json.loads(source["resource_json"])["calendar_id"]
            params = {"calendarId": calendar_id}
            result = self.client.call(source["account"], ["calendar", "events", "watch"], params, body=body)
        if not result.get("ok"):
            # Keep one diagnostic error row per source, not one per retry.
            for old in self.store.remotes(source_id=source["source_id"]):
                if old["kind"] == CHANNEL_KIND[service] and old["state"] == "error":
                    self.store.remove_remote(old["remote_id"])
            self.store.add_remote(remote_id, CHANNEL_KIND[service], source_id=source["source_id"],
                                  state="error", identity={"token": token, "channel_id": channel_id},
                                  detail=result.get("error", "channel registration failed"))
            self.store.update_source(source["source_id"], last_error=result.get("error", "channel registration failed"))
            return False
        data = result.get("data") or {}
        resource_id = str(data.get("resourceId") or "")
        expires = _expiry(data.get("expiration")) or expiration / 1000.0
        self.store.update_remote(remote_id, state="active", expires_at=expires,
                                 identity_json=json.dumps({
                                     "token": token, "channel_id": channel_id,
                                     "resource_id": resource_id, "source_id": source["source_id"],
                                 }, sort_keys=True))
        self.store.update_source(source["source_id"], resource_id=resource_id, expires_at=expires,
                                 last_error=None)
        return True

    def ensure_subscription(self, source):
        """Create/renew the Google subscription with durable bounded backoff.

        The backoff lives in ``meta`` so a restart or crash loop cannot hammer
        Google; a fresh watch start on the source clears it.
        """
        service = source["service"]
        key = "backoff:sub:" + source["source_id"]
        if not self._retry_due(key):
            return False
        ok = False
        try:
            if service == "gmail":
                ok = self._ensure_gmail(source)
            elif service in ("drive", "calendar"):
                ok = self._ensure_channel(source, service)
        except Exception:
            logger.warning("watch subscription failed for %s", service, exc_info=True)
        self._retry_outcome(key, ok)
        return ok

    def _retry_due(self, key):
        return float(_backoff_state(self.store, key).get("next", 0)) <= self.clock()

    def _retry_outcome(self, key, ok):
        """Durable, bounded exponential backoff: one meta row per key, never growth."""
        if ok:
            self.store.delete_meta(key)
            return
        attempts = int(_backoff_state(self.store, key).get("attempts", 0)) + 1
        delay = min(self.subscription_backoff_max, 5 * 2 ** min(attempts, 16))
        self.store.set_meta(key, json.dumps({"attempts": attempts, "next": self.clock() + delay}))

    # --- delta reading ---------------------------------------------------
    def drain_source(self, source):
        # Start holds this barrier from baseline capture through linking the new watch.
        # Never advance the shared cursor past a watch still being registered.
        try:
            with lifecycle_guard(self.home / "gws-oauth", source["account"], wait=False):
                current = self.store.get_source(source["source_id"])
                if current is None or current["state"] != "active":
                    return {"ok": True, "skipped": "inactive source"}
                return self._drain_source(current)
        except TimeoutError:
            return {"ok": True, "skipped": "account lifecycle busy"}
        except (OSError, ValueError):
            self.store.update_source(source["source_id"], last_error="source lifecycle guard failed")
            return {"ok": False, "error_code": "source_lifecycle_error", "retryable": True}

    def _drain_source(self, source):
        watches = self.store.consumers(source["source_id"])
        if not watches:
            return {"ok": True, "skipped": "no consumers"}
        service = source["service"]
        # Settle only hints visible before this read, never a racing PUSH.
        hints = self.store.pending_hints(source["source_id"])
        transport_error = (source.get("last_error") if _backoff_state(
            self.store, "backoff:sub:" + source["source_id"]).get("attempts") else None)
        if service == "gmail":
            try:
                watch_pubsub.validate_config(self.config)
            except W.WatchError as exc:
                transport_error = exc.message
        try:
            if service == "gmail":
                result = watch_gmail.drain(self.client, self.store, source, watches)
            elif service == "drive":
                result = watch_drive.drain(self.client, self.store, source, watches)
            elif service == "calendar":
                result = watch_calendar.drain(self.client, self.store, source, watches)
            else:
                return {"ok": False, "error_code": "unknown_service"}
        except Exception:
            logger.warning("watch drain failed for %s", service, exc_info=True)
            self.store.update_source(source["source_id"], last_error="delta reader internal failure")
            return {"ok": False, "error_code": "internal_error", "retryable": True}
        if not result.get("ok"):
            detail = result.get("error_code", "delta error")
            self.store.update_source(source["source_id"], last_error=detail)
            if result.get("resync_required"):
                for watch in watches:
                    self.store.update_watch(
                        watch["watch_id"],
                        gap="Drive change token was rejected; source requires an explicit resync/restart")
            return result
        if not result.get("staged"):
            if service == "gmail" and result.get("cursor"):
                hints = [hint for hint in hints if int(json.loads(hint["payload_json"])["history_id"])
                         <= int(result["cursor"])]
            with self.store.tx() as conn:
                conn.executemany("UPDATE hints SET state='done' WHERE hint_id=?",
                                 [(hint["hint_id"],) for hint in hints])
        self.store.update_source(source["source_id"], last_error=transport_error)
        return result

    # --- forwarding ------------------------------------------------------
    def _backoff(self, attempts):
        return min(300, max(1, 2 ** min(int(attempts), 8))) + secrets.randbelow(5)

    def forward(self):
        due = self.store.due_outbox(self.clock())
        groups = defaultdict(list)
        for row in due:
            groups[row["watch_id"]].append(row)
        for watch_id, rows in groups.items():
            watch = self.store.get_watch(watch_id)
            if watch is None:
                for row in rows:
                    self.store.set_event(row["event_id"], "cancelled", detail="watch no longer exists")
                continue
            if watch["state"] not in W.ACTIVE_STATES:
                for row in rows:
                    self.store.set_event(row["event_id"], "cancelled", detail="collection stopped")
                continue
            if watch["mode"] == "same_session":
                for row in rows:
                    self._forward_bridge(watch, row)
                continue
            self._forward_native(watch, rows)

    def _forward_native(self, watch, rows):
        # Rows that already carry a request id are an immutable accepted batch.
        # Never merge newly due events into them: Hermes dedupes by request id,
        # so a changed payload would be silently discarded as a duplicate.
        assigned = defaultdict(list)
        fresh = []
        for row in rows:
            if row.get("request_id"):
                assigned[row["request_id"]].append(row)
            else:
                fresh.append(row)
        for request_id, batch in assigned.items():
            self._send_native_batch(watch, batch, request_id)
        if not fresh:
            return
        oldest = min(row["created_at"] for row in fresh)
        if (watch["batch_seconds"] and not watch["stop_after_match"]
                and len(fresh) == 1 and self.clock() - oldest < watch["batch_seconds"]):
            return
        request_id = "gws-" + hashlib.sha256(
            ",".join(sorted(row["event_id"] for row in fresh)).encode()).hexdigest()[:40]
        self._send_native_batch(watch, fresh, request_id)

    def _send_native_batch(self, watch, rows, request_id):
        watch_now, claimed = self.store.claim_outbox(
            [row["event_id"] for row in rows], state="forwarding",
            expected_states=("pending", "retry"), request_id=request_id,
            generation=int(watch["generation"]))
        if watch_now is None or not claimed:
            return
        event_ids = [row["event_id"] for row in claimed]
        events = [json.loads(row["payload_json"]) for row in claimed]
        payload = {"event_type": "gws-watch", "watch_id": watch_now["watch_id"],
                   "service": watch_now["service"], "account": watch_now["account"],
                   "generation": watch_now["generation"], "events": events}
        try:
            result = self.poster(watch_now["route_name"], payload, request_id)
        except Exception:
            result = {"ok": False, "error_code": "native_route_internal", "retryable": True}
        if result.get("ok"):
            detail = "native admission accepted; completion and external delivery are unobservable"
            if self.store.finish_outbox(event_ids, "admitted",
                                        expected_states=("forwarding",), detail=detail):
                self.store.update_watch(watch_now["watch_id"], delivery_state="admitted",
                                        last_error=None)
            return
        if result.get("retryable"):
            changed = 0
            for row in claimed:
                changed += self.store.finish_outbox(
                    [row["event_id"]], "retry", expected_states=("forwarding",),
                    detail=result.get("error_code", "retry"),
                    next_attempt=self.clock() + self._backoff(row["attempts"]))
            if changed:
                self.store.update_watch(watch_now["watch_id"], delivery_state="pending",
                                        last_error=result.get("error_code", "native route retry"))
            return
        if self.store.finish_outbox(event_ids, "failed",
                                    expected_states=("forwarding",),
                                    detail=result.get("error_code", "rejected")):
            self.store.update_watch(watch_now["watch_id"], delivery_state="failed",
                                    last_error=result.get("error_code", "native route rejected"))

    def _forward_bridge(self, watch, row):
        if not watch.get("bridge_name"):
            if self.store.finish_outbox([row["event_id"]], "failed",
                                        expected_states=("pending", "retry"),
                                        detail="bridge route missing"):
                self.store.update_watch(watch["watch_id"], delivery_state="failed",
                                        last_error="bridge route missing")
            return
        request_id = row.get("request_id") or ("gws-" + hashlib.sha256(
            row["event_id"].encode()).hexdigest()[:40])
        watch_now, claimed = self.store.claim_outbox(
            [row["event_id"]], state="forwarding",
            expected_states=("pending", "retry"), request_id=request_id,
            generation=int(watch["generation"]))
        if watch_now is None or not claimed:
            return
        row = claimed[0]
        payload = {"event_type": "gws-watch-bridge", "watch_id": watch_now["watch_id"],
                   "event_id": row["event_id"], "generation": watch_now["generation"]}
        try:
            result = self.poster(watch_now["bridge_name"], payload, request_id)
        except Exception:
            result = {"ok": False, "error_code": "bridge_internal", "retryable": True}
        if result.get("ok"):
            # The hook may already have scheduled the event; only a still-
            # forwarding row gets this informational acknowledgement.
            self.store.bridge_accepted(
                row["event_id"],
                "bridge request accepted by Hermes; awaiting same-session admission")
            return
        if result.get("retryable"):
            if self.store.finish_outbox(
                    [row["event_id"]], "retry", expected_states=("forwarding",),
                    detail=result.get("error_code", "retry"),
                    next_attempt=self.clock() + self._backoff(row["attempts"])):
                self.store.update_watch(watch_now["watch_id"], delivery_state="pending",
                                        last_error=result.get("error_code", "bridge retry"))
            return
        if self.store.finish_outbox([row["event_id"]], "failed",
                                    expected_states=("forwarding",),
                                    detail=result.get("error_code", "rejected")):
            self.store.update_watch(watch_now["watch_id"], delivery_state="failed",
                                    last_error=result.get("error_code", "bridge rejected"))

    # --- cleanup ---------------------------------------------------------
    def _stop_source(self, source):
        key = "backoff:stop:" + source["source_id"]
        remotes = self.store.claim_unused_remotes(source["source_id"])
        if remotes is None:
            return True  # A collecting watch owns this lease.
        if not self._retry_due(key):
            return False
        failed = False
        for remote in remotes:
            if remote["kind"] == "gmail_watch":
                result = self.client.call(source["account"], ["gmail", "users", "stop"], {"userId": "me"})
            elif remote["kind"] in ("drive_channel", "calendar_channel"):
                identity = json.loads(remote["identity_json"])
                params = {"id": identity.get("channel_id"), "resourceId": identity.get("resource_id")}
                if not params["id"] or not params["resourceId"]:
                    continue
                result = self.client.call(source["account"], ["drive", "channels", "stop"]
                                          if remote["kind"] == "drive_channel"
                                          else ["calendar", "channels", "stop"], {}, body=params)
            else:
                continue
            if result.get("ok") or result.get("http_status") in (404, 410):
                self.store.remove_remote(remote["remote_id"])
            else:
                failed = True
                self.store.update_remote(remote["remote_id"], state="cleanup_pending",
                                         detail=result.get("error_code", "stop failed"))
        if not failed:
            # Conditional: a watch started during the network calls keeps the source.
            self.store.stop_source_if_unused(source["source_id"])
        self._retry_outcome(key, not failed)
        return not failed

    def cleanup(self):
        for watch in self.store.list_watches(service=None):
            if watch["state"] not in ("stopped", "expired", "matched"):
                continue
            if watch["cleanup_state"] == "done":
                continue
            sources_ok = True
            for source in self.store.sources_for(watch["watch_id"]):
                if source["state"] != "stopped":
                    sources_ok = self._stop_source(source) and sources_ok
            delivered = self.store.delivery_summary(watch["watch_id"])
            route_name = watch["route_name"] or watch["bridge_name"]
            # Keep reserved notifications routable until their dispatch settles.
            waiting = watch["state"] == "matched" and delivered["backlog"] > 0
            native_attempted = any(row["attempts"] or row["state"] in ("admitted", "uncertain")
                                   for row in self.store.outbox(watch["watch_id"]))
            retain = bool(watch["route_name"] and watch["state"] != "stopped"
                          and native_attempted)
            if waiting:
                state, reason = "pending", "one-shot notification still in flight"
            elif retain:
                state = "retained" if sources_ok else "pending"
                reason = "native completion is unobservable; route retained until explicit stop"
            else:
                removed = (self.routes.remove(route_name, watch["watch_id"]) if route_name
                           else {"removed": True, "state": "absent"})
                state = "done" if removed.get("removed") and sources_ok else "pending"
                reason = f"route={removed.get('state')}; sources={'ok' if sources_ok else 'pending'}"
            self.store.update_watch(watch["watch_id"], cleanup_state=state, cleanup_reason=reason)

    # --- loop ------------------------------------------------------------
    def run_once(self):
        self.store.expire_due()
        self.store.recover_stopped_sources()
        self.store.recover_stale_outbox(self.clock())
        for source in self.store.list_sources(active_only=True):
            watches = self.store.consumers(source["source_id"])
            if not watches:
                self._stop_source(source)
                continue
            # A failed push lease must not disable cursor reconciliation.
            self.ensure_subscription(source)
            source = self.store.get_source(source["source_id"])
            if source is None:
                continue
            pending = self.store.pending_hints(source["source_id"])
            reconciled = source["last_reconcile"]
            reconcile_due = reconciled is None or reconciled + self.reconcile_seconds <= self.clock()
            if source.get("cursor_staged") or pending or reconcile_due:
                self.drain_source(source)
        self.cleanup()
        self.forward()
        now = self.clock()
        if self._last_prune is None or now - self._last_prune >= 3600:
            self._last_prune = now
            self.store.prune(now, retention_seconds=self.retention_seconds)
        self.store.set_meta("worker_heartbeat", now)
        return {"ok": True}

    async def run_forever(self, *, interval=5, callback_host="127.0.0.1", callback_port=8765):
        server = None
        if web is not None and callback_port:
            server = CallbackServer(self, callback_host, callback_port)
            await server.start()
        try:
            while not self.stopping:
                await asyncio.to_thread(self.run_once)
                await asyncio.sleep(max(1, interval))
        finally:
            if server is not None:
                await server.stop()


def _expiry(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / 1000.0 if number > 10_000_000_000 else number


def _backoff_state(store, key):
    try:
        value = json.loads(store.get_meta(key) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _lock_path(home):
    return Path(home).expanduser().resolve() / "plugin-data" / "gws-oauth" / "worker.lock"


def _store(home):
    return W.WatchStore(home)


def _print(value):
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _lock_held(path):
    """True when a live worker holds the profile lock (probe, never blocks)."""
    if fcntl is None or not Path(path).exists():
        return False if fcntl is not None else None
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def health(store, lock_path, *, stale_seconds=60):
    """Secret-free worker/watch diagnostics for the operator CLI."""
    now = store.clock()
    beat = store.get_meta("worker_heartbeat")
    age = now - float(beat) if beat else None
    held = _lock_held(lock_path)
    watches = store.list_watches()
    counts = defaultdict(int)
    for row in watches:
        counts[row["state"]] += 1
    sources = []
    for source in store.list_sources(active_only=False):
        retries = {}
        for kind in ("sub", "stop"):
            state = _backoff_state(store, f"backoff:{kind}:" + source["source_id"])
            if state.get("next"):
                retries[kind] = {"attempts": state.get("attempts"), "next_at": W.now_iso(state["next"])}
        sources.append({"source_id": source["source_id"], "service": source["service"],
                        "account": source["account"], "state": source["state"],
                        "lease_expiry": W.now_iso(source["expires_at"]) if source.get("expires_at") else None,
                        "last_reconcile": W.now_iso(source["last_reconcile"]) if source.get("last_reconcile") else None,
                        "last_error": source.get("last_error"),
                        "pending_hints": len(store.pending_hints(source["source_id"])),
                        "retry": retries or None})
    uncollectable = store.uncollectable_watches()
    healthy = bool(held) and age is not None and age <= stale_seconds and not uncollectable
    return {"ok": healthy, "worker_running": held,
            "heartbeat": W.now_iso(float(beat)) if beat else None,
            "heartbeat_age_seconds": int(age) if age is not None else None,
            "stale_after_seconds": stale_seconds,
            "watches": dict(counts),
            "uncollectable_watches": uncollectable,
            "cleanup_pending": sum(1 for row in watches if row["cleanup_state"] == "pending"),
            "routes_retained": sum(1 for row in watches if row["cleanup_state"] == "retained"),
            "sources": sources}


def profile_settings(home):
    """Read only this profile's documented plugin settings; no ambient fallback."""
    import yaml
    try:
        path = Path(home).expanduser().absolute() / "config.yaml"
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        if config is None:
            return {}
        entry = config.get("plugins", {}).get("entries", {}).get("gws-workspace", {})
        legacy, settings = entry.get("config", {}), entry.get("settings", {})
        if not isinstance(legacy, dict) or not isinstance(settings, dict):
            raise ValueError
        return {**legacy, **settings}
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        raise ValueError("Invalid gws-workspace profile configuration") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="watch_worker", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the single profile worker in the foreground")
    run.add_argument("--home", required=True, help="Exact Hermes profile home")
    run.add_argument("--gws-bin")
    run.add_argument("--callback-url")
    run.add_argument("--gmail-topic")
    run.add_argument("--gmail-subscription")
    run.add_argument("--gmail-push-audience")
    run.add_argument("--gmail-push-service-account")
    run.add_argument("--callback-host", default="127.0.0.1")
    run.add_argument("--callback-port", type=int, default=8765)
    run.add_argument("--interval", type=int, default=5)
    run.add_argument("--reconcile-seconds", type=int)
    run.add_argument("--profile", default="default", help="Exact gateway profile name")
    run.add_argument("--subscription-backoff-max", type=int,
                     help="cap in seconds for failed subscription create/renew retries")
    run.add_argument("--retention-days", type=int,
                     help="days to keep fully cleaned terminal watch records")
    listing = sub.add_parser("list", help="list this profile's watches")
    listing.add_argument("--home", required=True)
    stop = sub.add_parser("stop", help="revoke one watch locally; the worker finishes remote cleanup")
    stop.add_argument("watch_id")
    stop.add_argument("--home", required=True)
    status = sub.add_parser("status", help="worker heartbeat and watch/source diagnostics; "
                                           "exit 1 for an absent/stale worker or an uncollectable watch")
    status.add_argument("--home", required=True)
    status.add_argument("--stale-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "status":
        report = health(_store(args.home), _lock_path(args.home), stale_seconds=args.stale_seconds)
        _print(report)
        return 0 if report["ok"] else 1
    if args.command == "list":
        store = _store(args.home)
        _print([W.watch_status(store, row) for row in store.list_watches()])
        return 0
    if args.command == "stop":
        store = _store(args.home)
        row = W.operator_stop(store, args.watch_id)
        _print({"ok": True, "watch": W.watch_status(store, row), "remote_cleanup": "pending"})
        return 0
    config = profile_settings(args.home)
    overrides = {"watch_callback_url": args.callback_url,
                 "watch_gmail_topic": args.gmail_topic,
                 "watch_gmail_subscription": args.gmail_subscription,
                 "watch_gmail_push_audience": args.gmail_push_audience,
                 "watch_gmail_push_service_account": args.gmail_push_service_account,
                 "watch_reconcile_seconds": args.reconcile_seconds,
                 "watch_subscription_backoff_max": args.subscription_backoff_max,
                 "watch_retention_days": args.retention_days}
    config.update({key: value for key, value in overrides.items() if value is not None})
    os.environ["HERMES_HOME"] = str(Path(args.home).expanduser().resolve())
    client = GwsClient(args.home, gws_bin=args.gws_bin or config.get("gws_bin", "gws"))
    # Re-read route attestations for every delivery; edits revoke existing bindings.
    routes = watch_routes.RouteManager(home=args.home, profile=args.profile,
        config=lambda: profile_settings(args.home).get("watch_routes", {"routes": []}))
    worker = Worker(args.home, client, routes, config)
    with SingleInstance(_lock_path(args.home)):
        try:
            asyncio.run(worker.run_forever(interval=args.interval,
                                           callback_host=args.callback_host,
                                           callback_port=args.callback_port))
        except KeyboardInterrupt:
            worker.stopping = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
