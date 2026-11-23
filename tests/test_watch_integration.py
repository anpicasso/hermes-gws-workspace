"""Real-host integration paths, isolated from the active profile.

These checks exercise the public host injection API and side-effect-free
registration. Operator route configuration is covered in test_watch_routes.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch_routes


class HostIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patch = mock.patch("hermes_constants.get_hermes_home", return_value=self.home)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_installed_host_public_injection_uses_existing_session_key_api(self):
        from hermes_cli.plugins_context import PluginContext
        calls = {}

        class FakeManager:
            home_path = self.home
            has_gateway_message_injector = True
            has_tui_message_injector = False
            _cli_ref = None
            _plugins = {}

            def inject_gateway_message(self, **kwargs):
                calls.update(kwargs)
                return True

        fake_ctx = SimpleNamespace(
            plugin_id="gws-workspace",
            _manager=FakeManager(),
            _gateway_injection_allowed=lambda: True,
        )
        self.assertTrue(PluginContext.inject_message(
            fake_ctx, "hello", session_key="agent:default:discord:chat:thread"))
        self.assertNotIn("expected_session_id", calls)
        self.assertEqual(calls["session_key"], "agent:default:discord:chat:thread")
        self.assertNotIn("origin", calls)

    def test_registration_does_not_spawn_worker(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gws_oauth_int", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        tools, hooks = {}, []
        ctx = SimpleNamespace(
            profile_name="default",
            register_command=lambda *args, **kwargs: None,
            register_skill=lambda *args, **kwargs: None,
            register_cli_command=lambda *args, **kwargs: None,
            get_config=lambda key, default=None: default,
            register_tool=lambda **kwargs: tools.setdefault(kwargs["name"], kwargs),
            register_hook=lambda name, callback: hooks.append(name),
            on_unload=lambda callback: None,
        )
        with mock.patch("hermes_constants.get_hermes_home", return_value=self.home):
            plugin.register(ctx)
        from gws_oauth_int.workspace_tools import SCHEMAS
        self.assertEqual(set(tools), {"gws_accounts", "gws_api", "gws_watch_prepare"} | set(SCHEMAS))
        self.assertIn("pre_gateway_dispatch", hooks)
        self.assertIn("pre_tool_call", hooks)
        self.assertFalse((self.home / "plugin-data" / "gws-oauth" / "worker.lock").exists())
        # Registration never starts the supervised worker; its lock only exists after `run`.
        self.assertTrue(all("watch-worker" not in thread.name for thread in __import__("threading").enumerate()))


if __name__ == "__main__":
    unittest.main()
