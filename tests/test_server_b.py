import copy
import hashlib
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from examples.legacy_battery.server_b.data_store import DataStore
from examples.legacy_battery.server_b.inference import DemoModel, ModelRegistry, TrustedTorchScript
from examples.legacy_battery.server_b.main import BoundedServer, Service
from examples.legacy_battery.server_b.run_manager import ProtocolError, RunManager


def start(run='run1', device='A01', count=6):
    return {'schema': 1, 'operation': 'start', 'request_id': 'request1', 'device_id': device, 'run_id': run,
            'task': 'soc', 'model_id': 'demo-vit', 'model_version': '1.0', 'source': 'demo',
            'dataset_name': 'deterministic_fixture.csv', 'sample_count': count, 'sample_period_s': 1.0,
            'mode': 'batch', 'features': ['voltage_v', 'current_a', 'temperature_c'],
            'current_convention': 'positive_discharge', 'file_sha256': 'a' * 64}


def sample(seq):
    return {'seq': seq, 'sample_time_s': float(seq), 'voltage_v': 4.1 - seq * 0.01,
            'current_a': 1.2, 'temperature_c': 25.0}


def batch(first=0, count=3, run='run1', device='A01'):
    return {'schema': 1, 'operation': 'samples', 'request_id': 'request2', 'device_id': device, 'run_id': run,
            'batch_id': f'batch{first}', 'seq_start': first, 'samples': [sample(seq) for seq in range(first, first+count)]}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'battery.db'
        self.store = DataStore(self.path)
        self.manager = RunManager(self.store, ModelRegistry())
        self.manager.handle('A01', start())

    def tearDown(self):
        self.manager.close(); self.store.close(); self.temp.cleanup()

    def call(self, operation, **kwargs):
        return self.manager.handle('A01', dict(schema=1, operation=operation, device_id='A01',
                                             request_id='r3', run_id='run1', **kwargs))

    def test_demonstration_windows_and_known_value(self):
        response = self.manager.handle('A01', batch())
        self.assertEqual(response['ack']['contiguous_through_seq'], 2)
        results = self.call('results')['results']
        self.assertEqual([r['valid'] for r in results], [False, False, True])
        self.assertIsNone(results[0]['value'])
        self.assertEqual(results[0]['state'], 'warming_up')
        self.assertAlmostEqual(results[2]['value'], 100 * (4.09 - 3) / 1.2 - 0.06, places=10)
        self.assertEqual(results[2]['mode'], 'demo')

    def test_duplicate_ack_has_no_duplicate_samples_or_predictions(self):
        first = self.manager.handle('A01', batch())
        second = self.manager.handle('A01', batch())
        self.assertEqual(first['ack'], second['ack'])
        self.assertEqual(second['run']['received_count'], 3)
        self.assertEqual(second['run']['result_count'], 3)

    def test_conflicting_duplicate_is_not_overwritten(self):
        self.manager.handle('A01', batch())
        changed = batch(); changed['samples'][0]['current_a'] = 99
        with self.assertRaisesRegex(ProtocolError, 'different content'): self.manager.handle('A01', changed)
        self.assertEqual(self.call('status')['run']['confirmed_through_seq'], 2)
        value = self.store.db.execute('SELECT current_a FROM samples WHERE seq=0').fetchone()[0]
        self.assertEqual(value, 1.2)

    def test_out_of_order_and_missing_batches_have_no_ack(self):
        with self.assertRaisesRegex(ProtocolError, 'Expected seq 0'): self.manager.handle('A01', batch(3))
        self.assertEqual(self.call('status')['run']['received_count'], 0)

    def test_labels_nan_and_wrong_units_rejected(self):
        bad = batch(); bad['samples'][0]['reference_soc'] = 100
        with self.assertRaisesRegex(ProtocolError, 'no reference'): self.manager.handle('A01', bad)
        for value in [float('nan'), float('inf'), None, True]:
            bad = batch(); bad['samples'][0]['voltage_v'] = value
            with self.assertRaises(ProtocolError): self.manager.handle('A01', bad)
        bad = batch(); bad['samples'][0]['voltage_v'] = 4100
        with self.assertRaisesRegex(ProtocolError, 'voltage in V'): self.manager.handle('A01', bad)

    def test_invalid_second_sample_rolls_back_whole_batch(self):
        bad = batch(); bad['samples'][1]['sample_time_s'] = 2
        with self.assertRaisesRegex(ProtocolError, 'time conflicts'): self.manager.handle('A01', bad)
        self.assertEqual(self.call('status')['run']['received_count'], 0)
        self.assertEqual(self.call('status')['run']['result_count'], 0)

    def test_explicit_pause_resume_finish_and_cancel(self):
        self.call('control', action='pause')
        with self.assertRaises(ProtocolError): self.manager.handle('A01', batch())
        self.call('control', action='resume')
        self.manager.handle('A01', batch())
        with self.assertRaisesRegex(ProtocolError, 'all samples'): self.call('control', action='finish')
        self.manager.handle('A01', batch(3))
        response = self.call('control', action='finish')
        self.assertEqual(response['run']['state'], 'completed')
        self.assertEqual(response['run']['valid_predictions'], 4)
        with self.assertRaises(ProtocolError): self.call('control', action='resume')

    def test_run_and_device_isolation(self):
        self.manager.handle('A01', batch())
        self.manager.handle('A02', start(device='A02'))
        self.manager.handle('A02', batch(count=1, device='A02'))
        result = self.manager.handle('A02', dict(schema=1, operation='results', request_id='r4', device_id='A02', run_id='run1'))
        self.assertIsNone(result['results'][0]['value'])
        self.assertEqual(result['run']['received_count'], 1)
        self.manager.handle('A01', start(run='run2'))
        self.manager.handle('A01', batch(count=1, run='run2'))
        self.assertEqual(self.manager.summary('A01', 'run2')['valid_predictions'], 0)

    def test_restart_rebuilds_window_from_durable_samples(self):
        self.manager.handle('A01', batch())
        self.manager.close(); self.store.close()
        self.store = DataStore(self.path); self.manager = RunManager(self.store, ModelRegistry())
        self.assertEqual(self.call('status')['run']['state'], 'paused')
        self.call('control', action='resume')
        self.manager.handle('A01', batch(3))
        self.assertEqual(self.call('status')['run']['valid_predictions'], 4)
        self.assertEqual(self.call('status')['run']['received_count'], 6)

    def test_changed_source_cannot_reuse_run_id(self):
        changed = start(); changed['file_sha256'] = 'b'*64
        with self.assertRaisesRegex(ProtocolError, 'another file'): self.manager.handle('A01', changed)

    def test_no_fallback_for_unavailable_research_model(self):
        message = start(run='research'); message['model_id'] = 'gru-mkmmd'
        with self.assertRaisesRegex(ProtocolError, 'no silent demo fallback'): self.manager.handle('A01', message)

    def test_model_metadata_is_required(self):
        model = TrustedTorchScript({'model_id': 'research'}, self.temp.name)
        with self.assertRaisesRegex(ValueError, 'mandatory model metadata'): model.validate()

    def test_backup_is_consistent(self):
        self.manager.handle('A01', batch())
        backup = self.store.backup(Path(self.temp.name) / 'backup.db')
        import sqlite3
        target = sqlite3.connect(backup)
        try:
            self.assertEqual(target.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 3)
        finally: target.close()


class HttpServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.password = 'test-admin-password'
        self.service = Service(self.temp.name, {'schema': 1, 'admin_hash': hashlib.sha256(self.password.encode()).hexdigest(),
            'device_tokens': {'A01': 'a'*64, 'A02': 'b'*64}, 'max_database_bytes': 1024*1024*32})
        self.server = BoundedServer(('127.0.0.1', 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.cookies = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.service.close(); self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        headers = dict(headers or {})
        if data is not None:
            headers.setdefault('Content-Type', 'application/json'); data = json.dumps(data).encode()
        return self.client.open(urllib.request.Request(self.url+path, data=data, headers=headers), timeout=10)

    def rpc(self, data, headers=None):
        with self.request('/api/edge/rpc', data, {'Authorization': 'Bearer '+'a'*64, **(headers or {})}) as response:
            return json.load(response)

    def login(self):
        with self.request('/api/session', {'password': self.password}) as response:
            return json.load(response)['csrf']

    def test_health_real_http(self):
        with self.request('/health') as response:
            self.assertEqual(json.load(response)['database'], 'ok')

    def test_admin_authentication_and_csrf(self):
        with self.assertRaises(urllib.error.HTTPError) as caught: self.request('/api/runs')
        self.assertEqual(caught.exception.code, 401)
        self.login()
        with self.request('/api/runs') as response: self.assertEqual(json.load(response)['runs'], [])
        with self.assertRaises(urllib.error.HTTPError) as caught: self.request('/api/backup', {})
        self.assertEqual(caught.exception.code, 403)

    def test_real_rpc_loop_and_export_without_reference_labels(self):
        self.rpc(start()); self.rpc(batch()); self.rpc(batch(3))
        control = dict(schema=1, operation='control', request_id='finish1', device_id='A01', run_id='run1', action='finish')
        self.assertEqual(self.rpc(control)['run']['state'], 'completed')
        self.login()
        with self.request('/api/runs/A01/run1/export') as response:
            text = response.read().decode('utf-8-sig')
        self.assertEqual(len(text.splitlines()), 7)
        self.assertNotIn('reference_soc', text)
        self.assertIn('demo', text)

    def test_browser_bypass_and_wrong_device_token_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught: self.rpc(start(), {'Origin': self.url})
        self.assertEqual(caught.exception.code, 403)
        message = start(device='A02')
        with self.assertRaises(urllib.error.HTTPError) as caught: self.rpc(message)
        self.assertEqual(caught.exception.code, 422)

    def test_browser_cross_origin_login_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/session', {'password': self.password}, {'Origin': 'https://attacker.example'})
        self.assertEqual(caught.exception.code, 403)

    def test_nonfinite_wire_json_is_rejected(self):
        self.rpc(start())
        message = batch(); message['samples'][0]['voltage_v'] = float('nan')
        with self.assertRaises(urllib.error.HTTPError) as caught: self.rpc(message)
        self.assertEqual(caught.exception.code, 400)


if __name__ == '__main__': unittest.main()
