"""Local draft invariants; test_session_origin drives the real tool and command dispatchers."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch
import watch_tools
from fake_google import FakeGoogle
from test_watch_routes import config_for
from watch_helpers import Clock, make_origin, runtime
from watch_routes import RouteManager


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.origin = make_origin()
        self.config = config_for(self.origin)
        routes = RouteManager(store=self.store, config=lambda: self.config)
        self.rt = runtime(self.store, FakeGoogle(), routes)
        self.base = {"service": "gmail", "account": "person@example.com", "target": {"mailbox": True},
                     "delivery_mode": "new_session"}

    def stage(self, **args):
        return watch_tools.prepare({**self.base, **args}, self.store, "default", self.origin["session_id"])

    def confirm(self, code, **origin):
        return watch_tools.handle("confirm", code, self.rt, origin={**self.origin, **origin})

    def count(self):
        with self.store.read() as conn:
            return conn.execute("SELECT COUNT(*) FROM watch_drafts").fetchone()[0]

    def test_invalid_drafts_never_persist_or_touch_accounts(self):
        window = {"start": "2030-01-01T00:00:00Z", "end": "2030-02-01T00:00:00Z"}
        for args in ({key: "forged"} for key in ("session_id", "origin", "actor", "principal", "profile",
                                                   "destination", "route", "watch_id", "code", "action")):
            with self.subTest(args=args), self.assertRaises(watch.WatchError):
                self.stage(**args)
        for args in ({"service": "status"}, {"account": "a/b@example.com"}, {"minutes": True},
                     {"batch_seconds": -1}, {"stop_after_match": 1}, {"instructions": ""}, {"skills": None},
                     {"delivery_mode": "same_session", "skills": ["test"]}, {"shared_drive_id": "drive"},
                     {"filters": []}, {"target": {"mailbox": 1}}, {"target": {"thread_ids": [" x"]}},
                     {"target": {"thread_ids": ["t"]}, "filters": {"from_domains": ["not valid"]}},
                     {"target": {"mailbox": True, "destination": "bad"}}, {"filters": {"label_ids": []}},
                     {"service": "drive", "target": {"all_files": True}, "shared_drive_id": None},
                     {"service": "drive", "target": {"all_files": False}},
                     {"service": "drive", "target": {"file_ids": []}},
                     {"service": "drive", "target": {"folder_id": " padded"}},
                     {"service": "drive", "target": {"all_files": True}, "filters": {"mime_types": ["bad"]}},
                     {"service": "calendar", "target": {"calendar_ids": ["primary"], "event_ids": []}},
                     {"service": "calendar", "target": {"calendar_ids": ["primary"], "recurring_event_ids": ["i"]},
                      "filters": {"date_window": window}},
                     {"service": "calendar", "target": {"calendar_ids": ["primary"]},
                      "filters": {"changed_fields": ["attendee_responses"]}}):
            with self.subTest(args=args), self.assertRaises(watch.WatchError):
                self.stage(**args)
        with self.assertRaises(watch.WatchError) as err:
            self.stage(filters={"subject_contains": ["💬" * 200]})
        self.assertEqual(err.exception.code, "draft_too_large")
        self.assertEqual(self.count(), 0)
        self.assertFalse((self.home / "gws-oauth").exists())

    def test_frozen_exact_review_and_expiry_capacity(self):
        selectors = {"label_ids": ["INBOX"]}
        with patch.object(watch, "MAX_DRAFTS", 2):
            draft = self.stage(filters=selectors, minutes=12, stop_after_match=True)
            selectors["label_ids"].append("SENT")
            summary = draft["summary"]
            self.assertEqual(summary["filters"], {"label_ids": ["INBOX"], "change_types": ["messageAdded"], "include_sent": False})
            self.assertEqual((summary["minutes"], summary["stop_after_match"]), (12, True))
            self.assertLessEqual(len(json.dumps(draft)), 2000)
            # A later valid proposal must never replace the contents under an already-shown code.
            code = draft["command"].split()[-1]
            self.assertRegex(code, r"^[A-Za-z0-9_-]{22}$")
            self.assertNotEqual(self.stage()["command"], draft["command"])
            with self.assertRaises(watch.WatchError) as err:
                self.stage()
            self.assertEqual(err.exception.code, "draft_limit")
            self.rt.store = watch.WatchStore(self.home, clock=self.clock)  # reopen the on-disk proposal
            denied = self.confirm(code, profile="foreign")
            self.assertEqual(denied["error_code"], "draft_unavailable")
            self.assertEqual(self.confirm(code, session_id="other"), denied)
            self.assertEqual(self.confirm("A" * 22), denied)
            self.assertFalse(self.rt.client.calls)
            self.assertFalse(self.store.list_watches())
            activated = self.confirm(code)
            self.assertTrue(activated["ok"], activated)
            self.assertEqual(activated["watch"]["filters"], summary["filters"])
            self.assertEqual(self.confirm(code), denied)
            expiring = self.stage()["command"].split()[-1]
            self.clock.advance(watch.DRAFT_TTL_SECONDS)
            self.assertEqual(self.confirm(expiring), denied)
            self.stage()
            self.assertEqual(self.count(), 1)
            self.clock.advance(watch.DRAFT_TTL_SECONDS)
            self.store.prune()
            self.assertEqual(self.count(), 0)

    def test_denied_principal_cannot_burn_code_but_failed_google_start_does(self):
        code = self.stage()["command"].split()[-1]
        denied = self.confirm(code, principal="other", user_id="other")
        self.assertEqual(denied["error_code"], "draft_unavailable")
        route = self.config["routes"][0]["new_session"]
        route["enabled"] = False
        self.assertEqual(self.confirm(code), denied)
        self.assertEqual(self.count(), 1)
        self.assertFalse(self.rt.client.calls)
        route["enabled"] = True
        def failure(*args, **kwargs):
            self.assertEqual(self.count(), 0)  # committed, visible on another connection before Google
            return {"ok": False}
        with patch.object(self.rt.client, "call", side_effect=failure) as google:
            self.assertEqual(self.confirm(code)["error_code"], "gmail_access")
            self.assertEqual(self.confirm(code), denied)
            google.assert_called_once()
        self.assertFalse(self.store.list_watches())

    def test_all_files_confirmation_deduplicates_only_the_reviewed_drive(self):
        for drive, duplicate in (("first", False), ("second", False), ("first", True)):
            draft = self.stage(service="drive", target={"all_files": True}, shared_drive_id=drive)
            self.assertEqual(draft["summary"]["shared_drive_id"], drive)
            result = self.confirm(draft["command"].split()[-1])
            self.assertEqual(result["already_active"], duplicate)
            source, = self.store.sources_for(result["watch"]["watch_id"])
            self.assertEqual(json.loads(source["resource_json"])["drive_id"], drive)

    def test_exact_confirmation_grammar(self):
        code = self.stage()["command"].split()[-1]
        for verb in ("confirmar", "confirm"):
            self.assertEqual(watch_tools.parse_command(f"{verb} {code}"), ("confirm", code))
            for arg in ("", code[:-1], code + "=", code + ' {"target":{"mailbox":true}}'):
                with self.subTest(verb=verb, arg=arg), self.assertRaises(watch.WatchError):
                    watch_tools.parse_command(f"{verb} {arg}")
        self.assertEqual(self.count(), 1)
