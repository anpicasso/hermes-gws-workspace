"""Shared-source fan-out through the real store, worker, signer and bridge; no network."""
import asyncio
import json
import sys
import tempfile
import unittest
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_google import FakeGoogle
from test_watch_routes import DummyResponse, config_for
from watch import WatchStore
from watch_helpers import Clock, make_origin, runtime, start_calendar, start_drive, start_gmail
from watch_routes import RouteManager, bridge_hook, route_name
from watch_worker import Worker


class MultiSessionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock, self.client = Clock(), FakeGoogle()
        self.store = WatchStore(tmp.name, clock=self.clock)
        self.origins, entries = [], []
        for i, who in enumerate(('user-A', 'user-A', 'user-B')):
            origin = dict(make_origin(who, session_id=f'session-{i}'), chat_id=f'chat-{i}',
                          thread_id=f'thread-{i}', session_key=f'agent:default:discord:chat-{i}:thread-{i}')
            self.origins.append(origin)
            entry = config_for(origin)['routes'][0]
            # Separate native destinations, one shared opaque bridge.
            for k in ('name', 'url'):
                entry['new_session'][k] += f'-{i}'
            entries.append(entry)
        self.ctx = SimpleNamespace(inject_message=Mock(return_value=True))
        routes = RouteManager(self.ctx, store=self.store, config={'routes': entries})
        self.rt = [runtime(self.store, self.client, routes, origin=o, ctx=self.ctx) for o in self.origins]
        self.worker = Worker(tmp.name, self.client, routes, self.rt[0].config, store=self.store, clock=self.clock)
        self.hook = bridge_hook(self.store, self.ctx, routes)
        self.sessions = {o['session_key']: SimpleNamespace(session_id=o['session_id']) for o in self.origins}
        self.lookup = SimpleNamespace(lookup_by_session_key=self.sessions.get)

    def dispatch(self, name, body):
        source = SimpleNamespace(chat_name=f'webhook/{name}', platform='webhook',
                                 user_id=f'webhook:{name}', profile='default')
        return asyncio.run(self.hook(SimpleNamespace(raw_message=body, source=source),
                                     session_store=self.lookup))

    def matrix(self, service, start, selector, id_field):
        watches = {}
        for rt, mode, (word, instruction) in product(self.rt, ('same_session', 'new_session'),
                (('red', 'first red'), ('red', 'another red'), ('blue', 'only blue'))):
            args = dict(mode=mode, filters={selector: [word]}, instructions=instruction, batch_seconds=0)
            started = start(rt, **args)
            self.assertTrue(started['ok'], started)
            wid = started['watch']['watch_id']
            self.assertNotIn(wid, watches)
            watches[wid] = rt.origin(), mode, word, instruction
            repeat = start(rt, **args)
            self.assertTrue(repeat['already_active'])
            self.assertEqual(repeat['watch']['watch_id'], wid)

        for i, word in enumerate(('red', 'blue', 'unrelated'), 101):
            if service == 'gmail':
                self.client.add_message(word, 'T', subject=word)
                self.client.add_history(i, word)
            elif service == 'drive':
                self.client.add_file(word, word)
                self.client.add_change(word, file=self.client.drive['files'][word])
            else:
                self.client.calendar['deltas'].append(self.client.add_event('cal', word, summary=word))
        source, = self.store.list_sources(service=service)
        self.assertEqual({r['watch_id'] for r in self.store.consumers(source['source_id'])}, set(watches))
        self.assertTrue(self.worker.drain_source(source)['ok'])
        with patch('watch_routes.urllib.request.build_opener') as opener:
            transmit = opener.return_value.open
            transmit.return_value = DummyResponse({'status': 'accepted'})
            self.worker.forward()
            self.worker.forward()
        self.assertEqual(transmit.call_count, len(watches))
        receipts = set()
        for call in transmit.call_args_list:
            req = call.args[0]
            body = json.loads(req.data)
            wid = body['watch_id']
            origin, mode, word, instruction = watches.pop(wid)
            row, = self.store.outbox(wid)
            self.assertEqual(json.loads(row['payload_json'])[id_field], word)
            self.assertEqual(req.get_header('X-request-id'), row['request_id'])
            self.assertNotIn(row['request_id'], receipts)
            receipts.add(row['request_id'])
            name = req.full_url.rsplit('/', 1)[-1]
            self.ctx.inject_message.reset_mock()
            decision = self.dispatch(name, body)
            if mode == 'new_session':
                self.assertIsNone(decision)
                self.assertEqual(name, 'gws-native-' + str(self.origins.index(origin)))
                self.assertEqual(body['instructions'], instruction)
                self.assertEqual([e[id_field] for e in body['events']], [word])
                self.assertEqual(row['state'], 'admitted')  # simulated HTTP receipt, not a reply
                self.ctx.inject_message.assert_not_called()
            else:
                self.assertEqual(set(body), {'event_type', 'watch_id', 'generation', 'event_id'})
                self.assertEqual(decision['action'], 'skip')
                self.dispatch(name, body)  # duplicate receipt
                self.ctx.inject_message.assert_called_once()
                args, kwargs = self.ctx.inject_message.call_args
                self.assertEqual(kwargs, {'session_key': origin['session_key']})
                self.assertIn('Owner instructions: ' + instruction + '\n', args[0])
                self.assertEqual(json.loads(args[0].split('Metadata (JSON):\n')[1])[id_field], word)
                self.assertEqual(self.store.outbox(wid)[0]['state'], 'scheduled')
        self.assertFalse(watches)

    def test_reset_and_native_retry_do_not_block_siblings(self):
        # The old bridge binding is unusable after /new; a sibling still schedules normally.
        ids = [start_gmail(self.rt[i], mode=mode, batch_seconds=0)['watch']['watch_id']
               for i, mode in ((0, 'same_session'), (1, 'same_session'),
                               (1, 'new_session'), (2, 'new_session'))]
        self.sessions[self.origins[0]['session_key']] = SimpleNamespace(session_id='replacement')
        self.client.add_message('red', 'T')
        self.client.add_history(101, 'red')
        source, = self.store.list_sources(service='gmail')
        self.assertTrue(self.worker.drain_source(source)['ok'])
        attempts = []

        def receive(req, timeout):
            body = json.loads(req.data)
            wid = body['watch_id']
            attempts.append((wid, req.get_header('X-request-id'), req.data))
            if body['event_type'] == 'gws-watch-bridge':
                self.dispatch(req.full_url.rsplit('/', 1)[-1], body)
            if wid == ids[2] and sum(a[0] == wid for a in attempts) == 1:
                return DummyResponse({}, 503)
            return DummyResponse({'status': 'accepted'})

        with patch('watch_routes.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = receive
            self.worker.forward()
            self.assertEqual([self.store.outbox(w)[0]['state'] for w in ids],
                             ['failed', 'scheduled', 'retry', 'admitted'])
            self.clock.advance(10)
            self.worker.forward()
        self.ctx.inject_message.assert_called_once()
        self.assertEqual(self.ctx.inject_message.call_args.kwargs['session_key'], self.origins[1]['session_key'])
        self.assertCountEqual([a[0] for a in attempts], [*ids, ids[2]])
        first, retry = (a for a in attempts if a[0] == ids[2])
        self.assertEqual(retry, first)
        self.assertEqual(self.store.outbox(ids[2])[0]['state'], 'admitted')

    def test_conflicting_native_destinations_fail_before_start_and_send(self):
        routes = self.worker.routes
        first, second, _ = [e['new_session'] for e in routes.config['routes']]
        first['url'] = 'https://receiver.example.invalid/webhooks/' + first['name']
        wid = start_gmail(self.rt[0])['watch']['watch_id']
        binding = route_name(wid)
        body = {'watch_id': wid, 'generation': 1, 'event_type': 'gws-watch', 'events': []}
        second['name'] = first['name']
        self.client.calls.clear()
        for url in (first['url'], 'https://RECEIVER.example.invalid:443/p/default/webhooks/' + first['name']):
            with self.subTest(url=url):
                second['url'] = url  # destination still belongs to the other conversation
                for rt in self.rt[:2]:
                    self.assertEqual(start_gmail(rt).get('error_code'), 'route_destination_conflict')
                with patch('watch_routes.urllib.request.build_opener') as opener:
                    self.assertEqual(routes.post(binding, body, 'retry')['error_code'], 'route_destination_conflict')
                    opener.assert_not_called()
        self.assertFalse(self.client.calls)
        for origin in self.origins:  # the shared opaque bridge is not a fixed native destination
            self.assertTrue(routes.preflight(origin, 'same_session', []))
        self.assertTrue(routes.preflight(self.origins[2], 'new_session', []))
        second['url'] = first['url'].replace('.invalid/', '.invalid:9443/')
        for origin in self.origins[:2]:  # same name on distinct endpoints is unambiguous
            self.assertTrue(routes.preflight(origin, 'new_session', []))

    def test_new_session_refuses_reserved_bridge_namespace(self):
        native = self.worker.routes.config['routes'][0]['new_session']
        native.update(name='gws-watch-bridge-unused', url='https://receiver.example.invalid/webhooks/gws-watch-bridge-unused')
        self.assertEqual(start_gmail(self.rt[0]).get('error_code'), 'route_config_invalid')
        self.assertFalse(self.client.calls)

    def test_gmail(self):
        self.matrix('gmail', start_gmail, 'subject_contains', 'message_id')

    def test_drive(self):
        self.matrix('drive', start_drive, 'name_contains', 'file_id')

    def test_calendar(self):
        self.client.add_calendar('cal', primary=True)
        self.matrix('calendar', start_calendar, 'summary_contains', 'event_id')
