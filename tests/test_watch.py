import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch
import watch_tools
from fake_google import FakeGoogle
from watch_helpers import Clock, FakeRoutes, make_origin, runtime, start_gmail


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.client = FakeGoogle()
        for thread in ("THREAD_1", "THREAD_2", "T1", "T2", "T"):
            self.client.add_message("root-" + thread, thread, internal_date="1600000000000")
        self.routes = FakeRoutes()
        self.rt = runtime(self.store, self.client, self.routes)

    def tearDown(self):
        self.tmp.cleanup()

    def test_trusted_origin_rejects_missing_chat_and_session(self):
        with self.assertRaises(watch.WatchError) as raised:
            watch.origin_from_session(env=lambda key: "", session_key="k", session_id="s",
                                      profile="default")
        self.assertEqual(raised.exception.code, "no_trusted_origin")
        with self.assertRaises(watch.WatchError) as raised:
            watch.origin_from_session(env=lambda key: {"HERMES_SESSION_PLATFORM": "discord",
                                                       "HERMES_SESSION_CHAT_ID": "c",
                                                       "HERMES_SESSION_ACTOR_ID": "u"}.get(key),
                                      session_key="", session_id="s", profile="default")
        self.assertEqual(raised.exception.code, "no_trusted_origin")

    def test_ids_addresses_and_domains_are_not_repaired(self):
        self.assertEqual(watch.clean_id("THREAD_1"), "THREAD_1")
        for bad in (" THREAD_1", "THREAD_1 ", "a b", "a\u0000b", ""):
            with self.assertRaises(watch.WatchError):
                watch.clean_id(bad)
        self.assertEqual(watch.clean_address("Alice@Example.COM"), "alice@example.com")
        for bad in ("Alice <alice@example.com>", "alice@example.com ", "a@b"):
            with self.assertRaises(watch.WatchError):
                watch.clean_address(bad)
        self.assertEqual(watch.clean_domain("example.com"), "example.com")
        for bad in ("Example.com", ".example.com", "example.com.", "sub.example.com "):
            with self.assertRaises(watch.WatchError):
                watch.clean_domain(bad)

    def test_strict_action_fields_and_service_mismatch(self):
        result = watch_tools.handle("gmail", {"action": "status", "account": "x"}, self.rt, origin=self.rt.origin())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_arguments")
        result = watch_tools.handle("drive", {"action": "status", "watch_id": "gmail-id"}, self.rt, origin=self.rt.origin())
        self.assertFalse(result["ok"])

    def test_start_status_stop_and_principal_isolation(self):
        started = start_gmail(self.rt, target={"thread_ids": ["THREAD_1"]})
        self.assertTrue(started["ok"], started)
        watch_id = started["watch"]["watch_id"]
        status = watch_tools.handle("gmail", {"action": "status", "watch_id": watch_id}, self.rt, origin=self.rt.origin())
        self.assertTrue(status["ok"])
        self.assertEqual(status["watch"]["target"]["thread_ids"], ["THREAD_1"])
        other = runtime(self.store, self.client, self.routes, origin=make_origin("user-2"))
        denied = watch_tools.handle("gmail", {"action": "status", "watch_id": watch_id}, other, origin=other.origin())
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error_code"], "not_found")
        stopped = watch_tools.handle("gmail", {"action": "stop", "watch_id": watch_id}, self.rt, origin=self.rt.origin())
        self.assertTrue(stopped["ok"])
        self.assertEqual(stopped["watch"]["state"], "stopped")
        self.assertEqual(stopped["remote_cleanup"], "pending")
        again = watch_tools.handle("gmail", {"action": "stop", "watch_id": watch_id}, self.rt, origin=self.rt.origin())
        self.assertTrue(again["ok"])

    def test_identical_start_is_idempotent_and_changed_selector_is_new(self):
        first = start_gmail(self.rt, target={"thread_ids": ["THREAD_1"]})
        second = start_gmail(self.rt, target={"thread_ids": ["THREAD_1"]})
        self.assertTrue(second["already_active"])
        self.assertEqual(first["watch"]["watch_id"], second["watch"]["watch_id"])
        changed = start_gmail(self.rt, target={"thread_ids": ["THREAD_2"]})
        self.assertFalse(changed["already_active"])
        self.assertNotEqual(first["watch"]["watch_id"], changed["watch"]["watch_id"])

    def test_one_shot_reservation_survives_expiry_until_explicit_stop(self):
        started = start_gmail(self.rt, target={"thread_ids": ["T"]}, stop_after_match=True)
        watch_id = started["watch"]["watch_id"]
        self.assertTrue(self.store.add_event(watch_id, "e1", "gmail", {"x": 1}))
        self.assertFalse(self.store.add_event(watch_id, "e2", "gmail", {"x": 2}))
        row = self.store.get_watch(watch_id)
        self.assertEqual(row["state"], "matched")
        self.assertEqual(row["collection"], "reserved")
        self.clock.advance(3601)
        self.store.expire_due()
        row = self.store.get_watch(watch_id)
        self.assertEqual(row["state"], "matched")
        self.assertEqual(self.store.outbox(watch_id)[0]["state"], "pending")
        self.store.stop(watch_id)
        self.assertEqual(self.store.outbox(watch_id)[0]["state"], "cancelled")

    def test_gmail_source_is_shared_and_consumers_are_tracked(self):
        first = start_gmail(self.rt, target={"thread_ids": ["T1"]})
        second = start_gmail(self.rt, target={"thread_ids": ["T2"]})
        sources = self.store.sources_for(first["watch"]["watch_id"])
        self.assertEqual(len(sources), 1)
        self.assertEqual(len(self.store.sources_for(second["watch"]["watch_id"])), 1)
        self.assertEqual(len(self.store.consumers(sources[0]["source_id"])), 2)
        self.store.stop(first["watch"]["watch_id"])
        self.assertEqual(len(self.store.consumers(sources[0]["source_id"])), 1)

    def test_corrupt_or_wrong_profile_database_path_is_private(self):
        self.assertTrue(self.store.path.is_file())
        self.assertEqual(self.store.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        other = watch.WatchStore(self.home / "other", clock=self.clock)
        self.assertNotEqual(self.store.path, other.path)

    def test_unknown_fields_and_empty_lists_are_rejected_before_google(self):
        base = {"action": "start", "account": "person@example.com",
                "target": {"mailbox": True}, "delivery_mode": "new_session"}
        for extra in ({"destination": "elsewhere"}, {"filters": {"regex": "x"}},
                      {"target": {"mailbox": True, "thread_ids": ["T"]}},
                      {"filters": {"from_addresses": []}}, {"minutes": 0},
                      {"batch_seconds": 61}):
            args = {**base, **extra}
            result = watch_tools.handle("gmail", args, self.rt, origin=self.rt.origin())
            self.assertFalse(result["ok"], (extra, result))
        skills = {**base, "delivery_mode": "same_session", "skills": ["x"]}
        self.assertEqual(watch_tools.handle("gmail", skills, self.rt, origin=self.rt.origin())["error_code"], "invalid_arguments")

    def test_non_object_filters_never_touch_accounts_or_google(self):
        for service, target in (("gmail", {"mailbox": True}), ("drive", {"all_files": True}),
                                ("calendar", {"calendar_ids": ["primary"]})):
            for invalid in (None, False, "", [], 0, ["x"]):
                args = {"action": "start", "account": "person@example.com", "delivery_mode": "new_session",
                        "target": target, "filters": invalid}
                with self.subTest(service=service, invalid=invalid):
                    self.assertEqual(watch_tools.handle(service, args, self.rt, origin=self.rt.origin())["error_code"], "invalid_arguments")
        self.assertFalse((self.home / "gws-oauth").exists())
        self.assertEqual(self.client.calls, [])

    def test_operator_route_checked_before_google_and_again_before_binding(self):
        from test_watch_routes import config_for
        from watch_routes import RouteManager
        config = config_for(self.rt.origin())
        self.rt.routes = RouteManager(store=self.store, config=config)
        other = runtime(self.store, self.client, self.rt.routes, origin=make_origin("other"))
        self.assertEqual(start_gmail(other)["error_code"], "route_not_configured")
        self.assertEqual(start_gmail(self.rt, skills=["absent"])["error_code"], "route_skills_mismatch")
        self.assertFalse((self.home / "gws-oauth").exists())
        self.assertEqual(self.client.calls, [])
        with patch.object(self.rt.routes, "config", side_effect=[config, {"routes": []}]):
            self.assertEqual(start_gmail(self.rt)["error_code"], "route_not_configured")
        self.assertTrue(self.client.calls)
        self.assertEqual(self.store.list_watches(), [])

    def test_status_pages_reach_all_owned_ids_and_bound_selectors(self):
        ids = {start_gmail(self.rt, minutes=i)["watch"]["watch_id"] for i in range(1, 53)}
        def status(**fields):
            result = watch_tools.handle("gmail", {"action": "status", **fields}, self.rt, origin=self.rt.origin())
            reply = watch_tools.command_reply(result)
            self.assertLessEqual(len(reply), 2000)
            return json.loads(reply)
        seen = []
        offset = 0
        while offset is not None:
            page = status(offset=offset)
            self.assertEqual(page["total"], len(ids))
            seen.extend(row["watch_id"] for row in page["watches"])
            self.assertLessEqual(len(page["watches"]), 10)
            offset = page["next_offset"]
            self.assertEqual(page["has_more"], offset is not None)
            if offset is not None:
                self.assertEqual(offset, len(seen))
        self.assertCountEqual(seen, ids)
        exact = status(watch_id=seen[-1])["watch"]
        self.assertIs(exact["target"]["mailbox"], True)
        self.assertEqual(exact["filters"]["change_types"], ["messageAdded"])
        for fields in ({"offset": True}, {"offset": -1}, {"offset": 0, "watch_id": seen[0]}):
            self.assertEqual(status(**fields)["error_code"], "invalid_arguments")
        long = start_gmail(self.rt, filters={"subject_contains": ['"\\' * 100, '💬' * 200]})
        wid = long["watch"]["watch_id"]
        self.store.update_watch(wid, gap='"' * 2000, last_error='\\' * 2000)
        detail = status(watch_id=wid)["watch"]
        self.assertNotIn("filters", detail)
        self.assertTrue(detail["filters_preview"].endswith("..."))
        self.assertEqual(detail["watch_id"], wid)

    def test_new_watch_restarts_a_stopped_source_from_its_fresh_baseline(self):
        first = start_gmail(self.rt, target={"thread_ids": ["T1"]})
        watch_id = first["watch"]["watch_id"]
        source = self.store.sources_for(watch_id)[0]
        self.store.stop(watch_id)
        self.client.gmail["history_id"] = "777"
        second = start_gmail(self.rt, target={"thread_ids": ["T2"]})
        self.assertTrue(second["ok"], second)
        source = self.store.get_source(source["source_id"])
        self.assertEqual(source["state"], "active")
        self.assertEqual(source["cursor"], "777")
        self.assertEqual(len(self.store.consumers(source["source_id"])), 1)

    def test_backpressure_rolls_back_page_and_cursor(self):
        started = start_gmail(self.rt)
        wid = started["watch"]["watch_id"]
        source = self.store.sources_for(wid)[0]
        with patch.object(watch, "MAX_BACKLOG", 1):
            with self.assertRaises(watch.WatchError) as raised:
                self.store.apply_events(source["source_id"],
                    [(wid, "e1", "gmail", {}), (wid, "e2", "gmail", {})], cursor="999")
            self.assertEqual(raised.exception.code, "backlog_full")
        self.assertEqual(self.store.outbox(wid), [])
        self.assertEqual(self.store.get_source(source["source_id"])["cursor"], source["cursor"])

    def test_expired_watch_cannot_collect_before_expiry_sweep(self):
        started = start_gmail(self.rt, minutes=1)
        wid = started["watch"]["watch_id"]
        self.clock.advance(61)
        self.assertFalse(self.store.add_event(wid, "late", "gmail", {}))
        self.store.expire_due()
        self.assertEqual(self.store.get_watch(wid)["state"], "expired")

    def test_watch_management_is_bound_to_conversation(self):
        started = start_gmail(self.rt)
        origin = {**make_origin(), "thread_id": "other", "session_key": "other"}
        other = runtime(self.store, self.client, self.routes, origin=origin)
        wid = started["watch"]["watch_id"]
        for action in ("status", "stop"):
            self.assertEqual(watch_tools.handle("gmail", {"action": action, "watch_id": wid},
                                               other, origin=other.origin())["error_code"], "not_found")
        self.assertEqual(watch_tools.handle("gmail", {"action": "status"}, other, origin=other.origin())["watches"], [])

    def test_fingerprint_distinguishes_timing_and_physical_same_session(self):
        common = watch.validate_common({"account": "person@example.com", "delivery_mode": "same_session"})
        spec = {**common, "target": {}, "filters": {}}
        first = watch.fingerprint("gmail", common["account"], make_origin(), spec)
        for changed in ({"minutes": 10}, {"batch_seconds": 1}):
            self.assertNotEqual(first, watch.fingerprint("gmail", common["account"],
                                                        make_origin(), {**spec, **changed}))
        self.assertNotEqual(first, watch.fingerprint("gmail", common["account"],
                                                     make_origin(session_id="new"), spec))

    def test_source_setup_failure_rolls_back_watch(self):
        spec = {**watch.validate_common({"account": "person@example.com", "delivery_mode": "new_session"}),
                "target": {}, "filters": {}, "fingerprint": "test"}
        with self.assertRaises(KeyError):
            watch.create_watch(self.store, "gmail", spec, make_origin(), watch_id="rollback",
                               sources=[{"resource": "me", "cursor": "100"}, {}])
        self.assertIsNone(self.store.get_watch("rollback"))
        self.assertEqual(self.store.list_sources(), [])

    def test_status_never_contains_credentials_or_route_secret(self):
        started = start_gmail(self.rt, target={"mailbox": True}, instructions="Notify me")
        rendered = str(started)
        self.assertNotIn("fixture-secret", rendered)
        self.assertNotIn("access_token", rendered)
        self.assertNotIn("refresh_token", rendered)


if __name__ == "__main__":
    unittest.main()
