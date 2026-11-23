"""Shared test fixtures; no real Google account or network is used."""
from __future__ import annotations

import json
from types import SimpleNamespace

import watch_routes


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeRoutes:
    def __init__(self, *, same_session=False):
        self.routes = {}
        self.posts = []
        self.removes = []
        self.same_session = same_session
        self.post_result = {"ok": True, "status": 202, "duplicate": False, "admission": "accepted"}

    def preflight(self, origin, mode, skills):
        if mode == "same_session" and not self.same_session:
            raise watch_routes.RouteError(
                "same_session_host_unsupported",
                "same-session requires a callable plugin session injection")

    def create(self, watch, spec, origin):
        self.preflight(origin, spec["mode"], spec["skills"])
        if spec["mode"] == "same_session":
            name = watch_routes.bridge_name(watch["watch_id"])
        else:
            name = watch_routes.route_name(watch["watch_id"])
        self.routes[name] = {"mode": spec["mode"], "watch_id": watch["watch_id"],
                             "origin": origin, "instructions": spec["instructions"]}
        return {"name": name, "mode": spec["mode"], "url": "http://127.0.0.1/webhooks/" + name}

    def post(self, name, payload, request_id, timeout=10):
        self.posts.append({"name": name, "payload": payload, "request_id": request_id})
        return dict(self.post_result)

    def remove(self, name, watch_id):
        self.removes.append((name, watch_id))
        self.routes.pop(name, None)
        return {"removed": True, "state": "removed"}

    def secret(self, name):
        return "fixture-secret"


def make_origin(principal="user-1", *, session_id="session-1", profile="default"):
    return {
        "profile": profile, "platform": "discord", "chat_id": "chat-1", "chat_type": "thread",
        "thread_id": "thread-1", "scope_id": None, "parent_chat_id": None,
        "user_id": principal, "principal": principal,
        "session_key": "agent:default:discord:chat-1:thread-1", "session_id": session_id,
    }


def runtime(store, client, routes, *, config=None, origin=None, ctx=None):
    origin = origin or make_origin()
    return SimpleNamespace(
        store=store, client=client, routes=routes, ctx=ctx or SimpleNamespace(),
        config={"watch_gmail_topic": "projects/p/topics/t",
                "watch_gmail_subscription": "projects/p/subscriptions/s",
                "watch_callback_url": "https://watch.example.invalid",
                "watch_gmail_push_audience": "https://watch.example.invalid/google/gmail",
                "watch_gmail_push_service_account": "push@p.iam.gserviceaccount.com",
                **(config or {})},
        origin=lambda: origin,
    )


def start_gmail(runtime_, *, target=None, filters=None, mode="new_session", **extra):
    args = {"action": "start", "account": "person@example.com",
            "target": target or {"mailbox": True}, "filters": filters or {},
            "delivery_mode": mode, "minutes": 60}
    args.update(extra)
    from watch_tools import handle
    return handle("gmail", args, runtime_, origin=runtime_.origin())


def start_drive(runtime_, *, target=None, filters=None, mode="new_session", **extra):
    args = {"action": "start", "account": "person@example.com",
            "target": target or {"all_files": True}, "filters": filters or {},
            "delivery_mode": mode, "minutes": 60}
    args.update(extra)
    from watch_tools import handle
    return handle("drive", args, runtime_, origin=runtime_.origin())


def start_calendar(runtime_, *, target=None, filters=None, mode="new_session", **extra):
    args = {"action": "start", "account": "person@example.com",
            "target": target or {"calendar_ids": ["primary"]}, "filters": filters or {},
            "delivery_mode": mode, "minutes": 60}
    args.update(extra)
    from watch_tools import handle
    return handle("calendar", args, runtime_, origin=runtime_.origin())


def as_json(value):
    return json.loads(json.dumps(value))
