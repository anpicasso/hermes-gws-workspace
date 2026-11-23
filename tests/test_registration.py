"""Public plugin loads independently; account lifecycle and origins fail closed."""
import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gws_public_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.active_home = self.home
        self.tools, self.commands, self.hooks, self.skills = {}, {}, {}, {}
        self.cli_commands = {}
        self.home_patch = patch("hermes_constants.get_hermes_home", side_effect=lambda: self.active_home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.ctx = SimpleNamespace(profile_name="default",
            get_config=lambda key, default=None: default,
            register_skill=lambda name, path: self.skills.setdefault(name, path),
            register_cli_command=lambda name, **kw: self.cli_commands.setdefault(name, kw),
            register_tool=lambda **kw: self.tools.setdefault(kw["name"], kw),
            register_hook=lambda name, cb: self.hooks.setdefault(name, cb),
            register_command=lambda name, cb, **kw: self.commands.setdefault(name, cb),
            on_unload=lambda cb: None)
        plugin.register(self.ctx)

    def call(self, name, args, **kwargs):
        return json.loads(self.tools[name]["handler"](args, **kwargs))

    def test_public_only_tools_and_no_credential_creation(self):
        from gws_public_test.workspace_tools import SCHEMAS
        self.assertEqual(set(self.tools), {"gws_accounts", "gws_api", "gws_watch_prepare"} | set(SCHEMAS))
        self.assertNotIn("gws_public_test.oauth", sys.modules)
        self.assertEqual(set(self.commands), {"gws-watch", "gws_watch"})
        self.assertIs(self.commands["gws_watch"], self.commands["gws-watch"])
        self.assertEqual(set(self.cli_commands), {"gws"})
        self.assertEqual(set(self.skills), {"workspace"})
        self.assertTrue(self.skills["workspace"].is_file())
        self.assertFalse((self.home / "gws-oauth").exists())
        self.assertEqual(self.call("gws_accounts", {}), {"ok": True, "accounts": []})

    def test_accounts_health_and_filter(self):
        for name in ("alice@example.com", "bob@example.com"):
            slot = self.home / "gws-oauth" / "accounts" / name
            slot.mkdir(parents=True)
            (slot / "credentials.enc").write_text("synthetic")
        with patch.object(plugin, "account_health", return_value={"state": "stored"}) as health:
            result = self.call("gws_accounts", {"account": "ALICE@example.com", "check_health": True})
        self.assertEqual(result["accounts"], ["alice@example.com"])
        health.assert_called_once()

    def test_lifecycle_contract_covers_pending_cleanup(self):
        from gws_public_test.watch import WatchStore
        store = WatchStore(self.home)
        query = {"account": "USER@example.com", "check_lifecycle": True}
        expected = {"ok": True, "contract": "gws-account-lifecycle-v1",
                    "account": "user@example.com", "credentials_in_use": False}
        self.assertEqual(self.call("gws_accounts", query), expected)
        store.add_watch({"watch_id": "lifecycle-test", "service": "gmail",
            "account": "user@example.com", "profile": "default", "principal": "u",
            "origin": {}, "mode": "new_session", "target": {}, "filters": {},
            "instructions": "", "skills": [], "minutes": 1, "batch_seconds": 0,
            "stop_after_match": False, "created_at": 1, "not_before": 1,
            "expires_at": 2, "fingerprint": "lifecycle-test"})
        for state, cleanup in (("active", "none"), ("matched", "done"), ("stopped", "pending")):
            store.update_watch("lifecycle-test", state=state, cleanup_state=cleanup)
            self.assertTrue(self.call("gws_accounts", query)["credentials_in_use"])
        store.update_watch("lifecycle-test", state="stopped", cleanup_state="done")
        self.assertEqual(self.call("gws_accounts", query), expected)
        for args in ({"check_lifecycle": True}, {"account": "../bad"}, {"check_health": "false"},
                     {**query, "check_health": True}):
            self.assertFalse(self.call("gws_accounts", args)["ok"])

    def test_every_tool_rejects_profile_mismatch(self):
        self.active_home = self.home / "other-profile"
        for name in self.tools:
            self.assertEqual(self.call(name, {})["error_code"], "profile_mismatch", name)

    def command(self, raw, **kwargs):
        return asyncio.run(self.commands["gws-watch"](raw, **kwargs))

    def test_watch_requires_explicit_command_context_not_environment(self):
        import os
        with patch.dict(os.environ, {"HERMES_SESSION_ID": "parent", "HERMES_SESSION_KEY": "route",
                                     "HERMES_SESSION_USER_ID": "user"}):
            result = json.loads(self.command('gmail {"action":"status"}', session_id="child"))
        self.assertEqual(result["error_code"], "no_trusted_origin")
        for name in ("gws_gmail_watch", "gws_drive_watch", "gws_calendar_watch"):
            self.assertNotIn(name, self.tools)

    def test_command_origin_is_host_context_and_profile_checked(self):
        source = SimpleNamespace(platform="discord", user_id="u", chat_id="c", thread_id="t")
        kwargs = dict(source=source, platform="discord", session_id="physical", session_key="logical")
        from gws_public_test import watch_tools
        with patch.object(watch_tools, "handle", return_value={"ok": True, "action": "status", "total": 0, "watches": []}) as handle:
            self.assertTrue(json.loads(self.command('gmail {"action":"status"}', **kwargs))["ok"])
            self.assertEqual(handle.call_args.kwargs["origin"]["session_id"], "physical")
            self.assertEqual(handle.call_args.kwargs["origin"]["session_key"], "logical")
            self.active_home = self.home / "other-profile"
            self.assertEqual(json.loads(self.command('gmail {"action":"status"}', **kwargs))["error_code"], "profile_mismatch")
            self.assertEqual(handle.call_count, 1)

    def test_help_and_malformed_commands_never_reach_google(self):
        with patch.object(plugin._PluginClient, "call", side_effect=AssertionError("No Google call")):
            self.assertIn("Usage:", self.command("--help"))
            for raw in ('gmail nope', 'drive []', 'other {}', 'gmail {"action":"status","action":"stop"}',
                        'gmail {"minutes":NaN}', 'x' * 16001):
                with self.subTest(raw=raw[:60]):
                    self.assertEqual(json.loads(self.command(raw))["error_code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
