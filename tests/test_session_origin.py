"""Real unchanged-host command dispatch; Google and final delivery are simulated."""
import asyncio
import contextvars
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HOST = Path(os.environ.get("HERMES_AGENT_SRC", Path.home() / ".hermes/hermes-agent"))
sys.path.insert(0, str(HOST))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_origin import command_origin
from watch import WatchError


class OriginTests(unittest.TestCase):
    def test_only_explicit_command_source_can_supply_origin(self):
        source = SimpleNamespace(platform="discord", user_id="actor", chat_id="chat", thread_id="thread")
        def origin(**overrides):
            args = dict(source=source, session_id="physical", session_key="route", platform="discord", profile="default")
            return command_origin(**{**args, **overrides})
        self.assertEqual(origin()["principal"], "actor")
        snapshot = origin()
        source.user_id = ""
        with self.assertRaises(WatchError):
            origin()
        self.assertEqual(snapshot["principal"], "actor")
        source.user_id = "actor"
        with mock.patch.dict(os.environ, {"HERMES_SESSION_ID": "physical", "HERMES_SESSION_ACTOR_ID": "actor", "HERMES_SESSION_CHAT_ID": "chat"}):
            for changes in ({"source": None}, {"session_id": ""}, {"session_key": ""},
                            {"session_id": " physical"}, {"profile": ""}):
                with self.subTest(changes=changes), self.assertRaises(WatchError):
                    origin(**changes)
        for name, value in (("chat_id", ""), ("profile", "foreign"), ("is_bot", True)):
            original = getattr(source, name, None)
            setattr(source, name, value)
            with self.assertRaises(WatchError):
                origin()
            setattr(source, name, original)
        for platform in ("local", "cli", "cron", "webhook", "api_server", "subagent"):
            source.platform = platform
            with self.subTest(platform=platform), self.assertRaises(WatchError):
                origin(platform=platform)

    def test_whatsapp_bridge_requires_an_explicit_nonshared_dm(self):
        from gateway.config import Platform
        from gateway.session import SessionSource
        source = SessionSource(platform=Platform.WHATSAPP, user_id="u", chat_id="c", chat_type="dm")
        def capture():
            return command_origin(source, "s", "k", "whatsapp", "default")
        self.assertEqual(capture()["principal"], "u")
        for source.chat_type, source.shared_session in (("group", False), ("", False), ("dm", True)):
            with self.subTest(chat=source.chat_type, shared=source.shared_session), self.assertRaises(WatchError):
                capture()


class GatewayDispatchTests(unittest.TestCase):
    def test_slash_lifecycle_and_saved_same_session_injection(self):
        from gateway.config import GatewayConfig, Platform
        from gateway.platforms.event import MessageEvent
        from gateway.session import SessionEntry, SessionSource
        from gateway.run import GatewayRunner
        from model_tools import handle_function_call
        from hermes_cli.plugins import get_plugin_manager, get_plugin_command_handler
        from hermes_cli.plugins_context import PluginContext
        from hermes_cli.plugins_manifest import parse_manifest_file
        from hermes_cli.profiles import get_active_profile_name
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from fake_google import FakeGoogle
        from test_watch_routes import config_for
        import watch

        repo = Path(__file__).resolve().parents[1]
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        home = Path(tmp) / ".hermes"
        home.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": tmp, "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1"}))
        token = set_hermes_home_override(home)
        self.addCleanup(reset_hermes_home_override, token)
        runner = GatewayRunner.__new__(GatewayRunner)  # No network listeners or credentials.
        runner.config = GatewayConfig(multiplex_profiles=True)
        profile = runner._primary_profile_name = get_active_profile_name()
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="actor", thread_id="thread")
        runner._canonicalize(source)
        key = runner._session_key_for_source(source)
        now = datetime.now(timezone.utc)
        entry = SessionEntry(key, "physical", now, now)
        origin = command_origin(source, entry.session_id, key, "telegram", profile)
        settings = {"watch_routes": config_for(origin), "watch_gmail_topic": "projects/p/topics/t",
                    "watch_gmail_subscription": "projects/p/subscriptions/s",
                    "watch_gmail_push_audience": "https://watch.example.invalid/google/gmail",
                    "watch_gmail_push_service_account": "push@p.iam.gserviceaccount.com",
                    "watch_callback_url": "https://watch.example.invalid"}
        (home / "config.yaml").write_text(json.dumps({"plugins": {"entries": {"gws-workspace": {"settings": settings}}}}))
        manager = get_plugin_manager()
        manifest = parse_manifest_file(repo / "plugin.yaml", repo, "user", "")
        self.assertIsNotNone(manifest)
        manager._load_plugin(manifest)
        self.addCleanup(manager.unload, manifest)
        handler = get_plugin_command_handler("gws-watch")
        assert handler is not None  # Loaded through the real plugin manager.
        self.assertIs(get_plugin_command_handler("gws_watch"), handler)
        client = FakeGoogle()
        client.add_calendar("person@example.com", primary=True)
        transport = mock.Mock(return_value=client)
        self.enterContext(mock.patch.dict(handler.__globals__, {"_PluginClient": transport}))
        injected = []
        self.enterContext(mock.patch.object(PluginContext, "inject_message", side_effect=lambda content, *, session_key: injected.append((content, session_key)) or True))
        store = watch.WatchStore(home)

        async def dispatch(service, args, selected_source=source, selected_entry=entry):
            text = '/gws_watch ' + service + ' ' + (args if isinstance(args, str) else json.dumps(args))
            event = MessageEvent(text=text, source=selected_source, user_id=selected_source.user_id)
            raw = await runner._hm_run_plugin_handler(handler, event, selected_source, selected_entry)
            return json.loads(raw)

        async def exercise():
            from importlib import import_module
            # Exercise the real access resolver before invoking the lifecycle handler below.
            with mock.patch.dict(runner.config.platforms, {source.platform: SimpleNamespace(extra={"allow_admin_from": ["admin"]})}):
                for name in ("gws-watch", "gws_watch"):
                    event = MessageEvent(text="/" + name + " help", source=source)
                    expected = runner._check_slash_access(source, name)
                    self.assertTrue(expected)
                    self.assertEqual(await runner._hm_resolve_command(event, source, key), (True, expected, name, name))
            routes_module = import_module(handler.__module__ + '.watch_routes')
            fake_ctx = SimpleNamespace(inject_message=lambda content, *, session_key: injected.append((content, session_key)) or True)
            routes = routes_module.RouteManager(fake_ctx, store=store, config=settings["watch_routes"], profile=profile)
            bridge = routes_module.bridge_hook(store, fake_ctx, routes, profile=profile)
            for service, target in (("gmail", {"mailbox": True}), ("drive", {"all_files": True}),
                                    ("calendar", {"calendar_ids": ["primary"]})):
                for mode in ("same_session", "new_session"):
                    args = {"action": "start", "account": "person@example.com", "target": target,
                            "delivery_mode": mode, "minutes": 5}
                    fields = {"service": service, **{k: v for k, v in args.items() if k != "action"}}
                    with mock.patch.dict(os.environ, {"HERMES_SESSION_ID": "physical"}):
                        self.assertEqual(json.loads(handle_function_call("gws_watch_prepare", fields))["error_code"],
                                         "no_trusted_session")
                    before = transport.call_count
                    draft = json.loads(handle_function_call("gws_watch_prepare", fields, session_id=entry.session_id))
                    self.assertTrue(draft.get("ok"), draft)
                    self.assertEqual(draft["summary"]["target"], target)
                    cmd, verb, code = draft["command"].split()
                    self.assertEqual((cmd, verb), ("/gws-watch", "confirmar"))
                    reset = SessionEntry(key, "reset", now, now)
                    foreign = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="other", thread_id="thread")
                    denied = await dispatch(verb, code, selected_entry=reset)
                    self.assertEqual(denied["error_code"], "draft_unavailable")
                    self.assertEqual(await dispatch(verb, code, foreign), denied)
                    if not before:
                        self.assertFalse(store.list_watches())
                        self.assertFalse((home / "gws-oauth").exists())
                        stale = json.loads(handle_function_call("gws_watch_prepare", fields, session_id=entry.session_id))
                        stale_code = stale["command"].split()[-1]
                        with store.tx() as conn:
                            conn.execute("UPDATE watch_drafts SET expires_at=0 WHERE code=?", (stale_code,))
                        self.assertEqual(await dispatch(verb, stale_code), denied)
                    self.assertEqual(transport.call_count, before)  # No Google/auth/route setup while drafting or denied.
                    results = await asyncio.gather(dispatch(verb, code), dispatch("confirm", code))
                    self.assertCountEqual([r["ok"] for r in results], [True, False], results)
                    started = next(r for r in results if r["ok"])
                    self.assertIn(denied, results)
                    self.assertEqual(await dispatch(verb, code), denied)
                    self.assertTrue(started.get("ok"), started)
                    wid = started["watch"]["watch_id"]
                    self.assertEqual(json.loads(store.get_watch(wid)["origin_json"]), origin)
                    self.assertTrue((await dispatch(service, args))["already_active"])
                    status = await dispatch(service, {"action": "status", "watch_id": wid})
                    self.assertEqual(status["watch"]["state"], "active")
                    if mode == "same_session":
                        eid = "event-" + wid
                        store.add_event(wid, eid, service, {"summary": "synthetic change"})
                        event = SimpleNamespace(raw_message={"event_type": "gws-watch-bridge", "watch_id": wid, "event_id": eid, "generation": 1},
                            source=SimpleNamespace(chat_name="webhook/gws-watch-bridge-main", platform="webhook", user_id="webhook:gws-watch-bridge-main", profile=profile))
                        receipt = await bridge(event=event, session_store=SimpleNamespace(lookup_by_session_key=lambda _: entry))
                        self.assertEqual(receipt["reason"], "gws watch bridge: consumed")
                        self.assertEqual(injected[-1][1], key)
                        self.assertEqual(store.outbox(wid)[0]["state"], "scheduled")
                    foreign = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="other", thread_id="thread")
                    denied = await dispatch(service, {"action": "stop", "watch_id": wid}, foreign)
                    self.assertEqual(denied["error_code"], "not_found")
                    self.assertEqual((await dispatch(service, {"action": "stop", "watch_id": wid}))["watch"]["state"], "stopped")
                rejected = await dispatch(service, {**args, "session_id": "forged"})
                self.assertEqual(rejected["error_code"], "invalid_arguments")
            self.assertEqual(len(injected), 3)
            no_session = SessionEntry(key, "", now, now)
            result = await dispatch("gmail", {"action": "status"}, selected_entry=no_session)
            self.assertEqual(result["error_code"], "no_trusted_origin")
            self.assertIn("normal message", result["error"])

        contextvars.copy_context().run(asyncio.run, exercise())


if __name__ == "__main__":
    unittest.main()
