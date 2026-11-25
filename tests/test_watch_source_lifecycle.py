"""Shared collection is independent of each watch's delivery lifetime and route."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch
import watch_gmail
import watch_worker
from fake_google import FakeGoogle
from watch_helpers import Clock, FakeRoutes, make_origin, runtime, start_gmail


class SourceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.google, self.routes = FakeGoogle(), FakeRoutes(same_session=True)
        self.rt = runtime(self.store, self.google, self.routes,
                          ctx=SimpleNamespace(inject_message=lambda *a, **kw: True))
        self.worker = watch_worker.Worker(self.home, self.google, self.routes, self.rt.config,
                                          store=self.store, clock=self.clock)

    def start(self, **kwargs):
        result = start_gmail(self.rt, batch_seconds=0, **kwargs)
        self.assertTrue(result['ok'], result)
        return result['watch']['watch_id']

    def message(self, history):
        mid = str(history)
        self.google.add_message(mid, 'T', internal_date=str(int(self.clock() * 1000)))
        self.google.add_history(history, mid)

    def watch(self, wid):
        row = self.store.get_watch(wid)
        assert row is not None
        return row

    def source(self, wid):
        only, = self.store.sources_for(wid)
        return only

    def test_rearm_identical_one_shot_while_old_delivery_is_pending(self):
        old = self.start(stop_after_match=True)
        self.worker.run_once()
        source = self.source(old)['source_id']
        self.store.add_event(old, 'reserved', 'gmail', {'message_id': 'old'})
        self.worker.cleanup()
        self.assertEqual(self.source(old)['state'], 'stopped')
        self.assertEqual(self.watch(old)['cleanup_state'], 'pending')
        self.message(101)  # between watches: not a new watch event
        new = self.start(stop_after_match=True)
        self.assertNotEqual(old, new)
        self.assertEqual(self.start(stop_after_match=True), new)  # still idempotent while collecting
        self.assertEqual(self.source(new)['cursor'], '101')
        self.assertEqual(self.source(new)['state'], 'active')
        self.assertEqual([w['watch_id'] for w in self.store.consumers(source)], [new])
        self.message(102)
        self.store.add_hint('new', source, {'history_id': '102'})
        self.worker.run_once()
        self.assertCountEqual([(p['payload']['watch_id'], p['payload']['events'][0]['message_id'])
                               for p in self.routes.posts], [(old, 'old'), (new, '102')])

    def test_mixed_sessions_keep_the_shared_cursor_and_lease_until_last_stop(self):
        bridge = self.start(mode='same_session')
        self.worker.run_once()
        sid = self.source(bridge)['source_id']
        self.message(101)
        self.store.update_source(sid, cursor_staged='0')
        saved = self.source(bridge)
        sibling = self.start(mode='same_session', filters={'subject_contains': ['Hello']})
        self.rt.origin = lambda: dict(make_origin(session_id='other-session'), thread_id='other',
                                      session_key='another-thread')
        native = self.start()
        self.assertEqual(self.source(native), saved)
        self.message(102)
        self.worker.run_once()
        self.assertEqual({w: sorted(json.loads(row['payload_json'])['message_id'] for row in self.store.outbox(w))
                          for w in (bridge, sibling, native)}, {bridge: ['101', '102'], sibling: ['102'], native: ['102']})
        self.assertCountEqual([(p['payload']['watch_id'], p['payload']['event_type']) for p in self.routes.posts],
                              [(bridge, 'gws-watch-bridge')] * 2 + [(sibling, 'gws-watch-bridge'), (native, 'gws-watch')])
        for wid in (native, bridge):
            self.store.stop(wid)
            self.worker.cleanup()
            self.assertEqual(self.google.gmail['stop_calls'], 0)
        self.store.stop(sibling)
        self.worker.run_once()
        self.assertEqual(self.google.gmail['watch_calls'], 1)
        self.assertEqual(self.google.gmail['stop_calls'], 1)

    def test_health_and_worker_recover_lost_mail_without_new_push_or_start(self):
        wid = self.start()
        sid = self.source(wid)['source_id']
        self.store.update_source(sid, state='stopped')  # legacy broken database
        self.store.set_meta('worker_heartbeat', self.clock())
        self.message(101)
        lock = self.home / 'worker.lock'
        with watch_worker.SingleInstance(lock):
            report = watch_worker.health(self.store, lock)
            self.assertFalse(report['ok'], report)
            self.assertEqual(report['uncollectable_watches'], [dict(watch_id=wid, source_id=sid, source_state='stopped')])
            self.worker.run_once()
            self.assertTrue(watch_worker.health(self.store, lock)['ok'])
        self.assertEqual(self.routes.posts[0]['payload']['events'][0]['message_id'], '101')
        self.assertEqual(self.source(wid)['cursor'], '101')
        self.assertEqual(self.google.gmail['watch_calls'], 1)

    def test_missing_link_is_reported_but_not_guessed_from_other_watches(self):
        first, orphan = self.start(), self.start(minutes=30)
        with self.store.tx() as conn:
            conn.execute('DELETE FROM watch_sources WHERE watch_id=?', (orphan,))
        self.worker.run_once()
        lock = self.home / 'worker.lock'
        with watch_worker.SingleInstance(lock):
            report = watch_worker.health(self.store, lock)
        self.assertFalse(report['ok'])
        self.assertEqual(report['uncollectable_watches'], [dict(watch_id=orphan, source_id=None, source_state=None)])
        self.assertEqual(self.source(first)['state'], 'active')

    def test_joining_stranded_collectors_preserves_committed_and_staged_cursors(self):
        first = self.start()
        sid = self.source(first)['source_id']
        self.message(101)
        self.store.update_source(sid, state='stopped', cursor_staged='0')
        second = self.start(minutes=30)
        source = self.source(second)
        self.assertEqual((source['state'], source['cursor'], source['cursor_staged']), ('active', '100', '0'))
        self.worker.run_once()
        self.assertEqual([p['payload']['watch_id'] for p in self.routes.posts], [first])
        self.assertEqual(self.store.outbox(second), [])

    def test_changed_start_reuses_source_after_matched_bridge_cleanup(self):
        old = self.start(mode='same_session', stop_after_match=True)
        self.worker.run_once()
        self.store.add_event(old, 'old-event', 'gmail', {})
        self.worker.forward()
        self.store.finish_bridge_event('old-event', 'scheduled', expected_states=('forwarding',))
        self.worker.cleanup()
        self.assertEqual(self.watch(old)['cleanup_state'], 'done')
        self.message(101)
        new = self.start(mode='same_session')
        source = self.source(new)
        self.assertEqual((source['state'], source['cursor']), ('active', '101'))

    def test_delta_does_not_cross_a_start_already_capturing_its_baseline(self):
        first = self.start()
        self.clock.advance(301)
        prepare = watch_gmail.prepare_start
        def racing_start(*args):
            spec = prepare(*args)
            self.message(101)  # after baseline capture, before the watch is linked
            self.worker.run_once()
            return spec
        with patch.object(watch_gmail, 'prepare_start', racing_start):
            second = self.start(minutes=30)
        self.worker.run_once()
        self.assertCountEqual([p['payload']['watch_id'] for p in self.routes.posts], [first, second])

    def test_expired_watch_does_not_resurrect_source(self):
        wid = self.start(minutes=1)
        sid = self.source(wid)['source_id']
        self.store.update_source(sid, state='stopped')
        self.clock.advance(61)
        self.worker.run_once()
        self.assertEqual(self.watch(wid)['state'], 'expired')
        self.assertEqual(self.source(wid)['state'], 'stopped')
        self.assertEqual(self.google.gmail['watch_calls'], 0)

    def test_stale_cleanup_never_stops_an_active_shared_lease(self):
        first = self.start()
        self.worker.run_once()
        snapshot = self.source(first)
        self.store.stop(first)
        self.start()
        self.worker._stop_source(snapshot)  # worker saw no collectors before the new start
        self.assertEqual(self.google.gmail['stop_calls'], 0)
        self.assertIsNotNone(self.store.remote_for_source(snapshot['source_id'], 'gmail_watch'))

    def test_rearm_during_remote_stop_survives_worker_crash(self):
        first = self.start()
        self.worker.run_once()
        source = self.source(first)
        self.store.stop(first)
        original = self.google.call
        def interrupted(account, args, *a, **kw):
            result = original(account, args, *a, **kw)
            if args[-1] == 'stop':
                self.start()  # start only registers local interest; worker is the sole lease writer
                raise SystemExit('crash after Gmail stopped, before the local acknowledgement')
            return result
        with patch.object(self.google, 'call', interrupted), self.assertRaises(SystemExit):
            self.worker._stop_source(source)
        self.message(101)
        self.clock.advance(301)
        restarted = watch_worker.Worker(self.home, self.google, self.routes, self.rt.config, clock=self.clock)
        restarted.run_once()
        self.assertEqual(self.google.gmail['watch_calls'], 2)
        self.assertEqual(self.routes.posts[0]['payload']['events'][0]['message_id'], '101')
