import hashlib
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .inference import FEATURES


class ProtocolError(Exception):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,48}', value) is not None


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class RunManager:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='inference')
        with store.lock, store.db:
            store.db.execute("UPDATE runs SET state='paused',updated_at=? WHERE state IN ('created','accepted','running','draining')", (time.time(),))
            for meta in registry.metadata():
                if 'version' in meta:
                    store.db.execute('INSERT OR REPLACE INTO models VALUES(?,?,?)', (meta['model_id'], meta['version'], encoded(meta)))

    def event(self, device, run, kind, message):
        self.store.db.execute('INSERT INTO events(device_id,run_id,created_at,kind,message) VALUES(?,?,?,?,?)',
                              (device, run, time.time(), kind, message))

    def row(self, device, run):
        row = self.store.db.execute('SELECT * FROM runs WHERE device_id=? AND run_id=?', (device, run)).fetchone()
        if row is None: raise ProtocolError('run_not_found', 'Run does not belong to this device', 404)
        return row

    def summary(self, device, run):
        row = dict(self.row(device, run))
        row['metadata'] = json.loads(row['metadata'])
        row['result_count'] = self.store.db.execute('SELECT COUNT(*) FROM predictions WHERE device_id=? AND run_id=?', (device, run)).fetchone()[0]
        row['valid_predictions'] = self.store.db.execute("SELECT COUNT(*) FROM predictions WHERE device_id=? AND run_id=? AND json_extract(result,'$.valid')=1", (device, run)).fetchone()[0]
        row['received_count'] = row['confirmed_through_seq'] + 1
        return row

    def handle(self, device, message):
        if message.get('schema') != 1 or message.get('device_id') != device or not identifier(message.get('request_id')):
            raise ProtocolError('identity_mismatch', 'Invalid schema, device or request identity')
        operation = message.get('operation')
        with self.store.lock:
            with self.store.db:
                self.store.db.execute('INSERT INTO devices VALUES(?,?,?) ON CONFLICT(device_id) DO UPDATE SET last_seen=excluded.last_seen',
                                      (device, time.time(), encoded({'transport': 'https-rpc'})))
            response = {'schema': 1, 'device_id': device, 'request_id': message['request_id'], 'ok': True}
            if operation in ('models', 'heartbeat'):
                response.update(models=self.registry.metadata(), service={'online': True, 'transport': 'https-rpc'})
                return response
            run = message.get('run_id')
            if not identifier(run): raise ProtocolError('invalid_run_id', 'Invalid run ID')
            if operation == 'start': self.start(device, run, message)
            elif operation == 'samples': response['ack'] = self.samples(device, run, message)
            elif operation == 'control': self.control(device, run, message)
            elif operation == 'results':
                after, limit = message.get('after', -1), message.get('limit', 16)
                if type(after) is not int or after < -1 or type(limit) is not int or not 1 <= limit <= 16:
                    raise ProtocolError('invalid_cursor', 'Results pages are bounded to 1–16 rows')
                self.row(device, run)
                response['results'] = [json.loads(row[0]) for row in self.store.db.execute(
                    'SELECT result FROM predictions WHERE device_id=? AND run_id=? AND seq>? ORDER BY seq LIMIT ?', (device, run, after, limit))]
            elif operation != 'status': raise ProtocolError('unknown_operation', 'Unknown operation')
            response['run'] = self.summary(device, run)
            return response

    def start(self, device, run, message):
        try: model = self.registry.get(message['model_id'], message['model_version'])
        except (KeyError, ValueError) as exc: raise ProtocolError('model_unavailable', str(exc)) from exc
        meta = model.validate()
        count, period = message.get('sample_count'), message.get('sample_period_s')
        if type(count) is not int or not 1 <= count <= 10000000 or not finite(period) or abs(period-meta['sample_period_s']) > 1e-9:
            raise ProtocolError('input_contract', 'Count or sample period conflicts with model metadata')
        if message.get('features') != FEATURES or message.get('current_convention') != meta['current_convention'] or message.get('task') != meta['task']:
            raise ProtocolError('input_contract', 'Feature order, current sign or task conflicts with model')
        if message.get('mode') not in ('realtime', 'batch') or message.get('source') not in ('dataset_replay', 'demo', 'public_dataset', 'simulation'):
            raise ProtocolError('invalid_mode', 'Explicit source and execution mode required')
        file_hash = message.get('file_sha256')
        if not isinstance(file_hash, str) or re.fullmatch('[0-9a-f]{64}', file_hash) is None:
            raise ProtocolError('file_identity', 'Source file SHA-256 is required for safe resume')
        dataset_name = message.get('dataset_name', '')
        if not isinstance(dataset_name, str) or len(dataset_name) > 128:
            raise ProtocolError('dataset_name', 'Dataset name is too long')
        fields = ['task', 'model_id', 'model_version', 'source', 'dataset_name', 'sample_count', 'sample_period_s',
                  'mode', 'features', 'current_convention', 'file_sha256']
        descriptor = {key: message[key] for key in fields}
        raw = encoded(descriptor)
        existing = self.store.db.execute('SELECT metadata FROM runs WHERE device_id=? AND run_id=?', (device, run)).fetchone()
        if existing:
            if existing[0] != raw: raise ProtocolError('run_conflict', 'Run ID already belongs to another file or model', 409)
            return
        now = time.time()
        with self.store.db:
            self.store.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                                  (device, run, meta['model_id'], meta['version'], 'accepted', raw, now, now, -1))
            self.event(device, run, 'accepted', 'Input contract accepted; model mode=' + meta['mode'])

    def samples(self, device, run, message):
        row = self.row(device, run)
        if row['state'] not in ('accepted', 'running'):
            raise ProtocolError('run_state', 'Resume or start an accepted run before sending data', 409)
        values = message.get('samples')
        if not isinstance(values, list) or not 1 <= len(values) <= 8:
            raise ProtocolError('batch_limit', 'A batch must contain 1–8 samples')
        if self.store.bytes_used() >= self.store.max_bytes:
            raise ProtocolError('storage_quota', 'Database quota reached; archive before continuing', 507)
        descriptor = json.loads(row['metadata']); model = self.registry.get(row['model_id'], row['model_version']); meta = model.metadata()
        confirmed = row['confirmed_through_seq']
        batch_id = message.get('batch_id')
        if not identifier(batch_id): raise ProtocolError('batch_id', 'Valid batch ID required')
        if type(message.get('seq_start')) is not int or message['seq_start'] != values[0].get('seq'):
            raise ProtocolError('batch_sequence', 'Batch seq_start does not match first sample')
        for index, sample in enumerate(values):
            if not isinstance(sample, dict) or set(sample) != {'seq', 'sample_time_s', *FEATURES}:
                raise ProtocolError('sample_schema', 'Samples contain only seq, time and V/I/T; no reference labels')
            seq = sample.get('seq')
            if type(seq) is not int or seq < 0 or seq >= descriptor['sample_count'] or seq != message['seq_start']+index:
                raise ProtocolError('invalid_sequence', 'Invalid or noncontiguous sample sequence')
            if any(not finite(sample.get(key)) for key in ['sample_time_s', *FEATURES]):
                raise ProtocolError('non_finite', 'NaN, infinity, booleans and missing values are rejected')
            if not 0 <= sample['voltage_v'] <= 6 or not -273.15 <= sample['temperature_c'] <= 500:
                raise ProtocolError('unit_range', 'Confirm cell voltage in V and temperature in degC')
            if sample['sample_time_s'] < 0: raise ProtocolError('invalid_time', 'Time must be nonnegative seconds')
        with self.store.db:
            for sample in values:
                seq = sample['seq']; raw_hash = hashlib.sha256(encoded(sample).encode()).hexdigest()
                previous = self.store.db.execute('SELECT digest FROM samples WHERE device_id=? AND run_id=? AND seq=?', (device, run, seq)).fetchone()
                if previous:
                    if previous[0] != raw_hash: raise ProtocolError('sample_conflict', 'Duplicate seq has different content; no overwrite', 409)
                    continue
                if seq != confirmed+1: raise ProtocolError('missing_sequence', f'Expected seq {confirmed+1}; retry from durable ACK', 409)
                if confirmed >= 0:
                    prior_time = self.store.db.execute('SELECT sample_time_s FROM samples WHERE device_id=? AND run_id=? AND seq=?', (device, run, confirmed)).fetchone()[0]
                    if abs(sample['sample_time_s'] - prior_time - meta['sample_period_s']) > max(1e-6, meta['sample_period_s']*1e-6):
                        raise ProtocolError('time_contract', 'Nonmonotonic or irregular time conflicts with model sampling period')
                self.store.db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?)',
                    (device, run, seq, sample['sample_time_s'], sample['voltage_v'], sample['current_a'], sample['temperature_c'], raw_hash, time.time()))
                window = [dict(r) for r in self.store.db.execute('SELECT * FROM samples WHERE device_id=? AND run_id=? AND seq BETWEEN ? AND ? ORDER BY seq',
                                                               (device, run, seq-meta['window_length']+1, seq))]
                valid = len(window) == meta['window_length'] and (seq-meta['window_length']+1) % meta['stride'] == 0
                started = time.perf_counter(); value = None
                if valid:
                    def execute(): return model.postprocess(model.predict(model.preprocess(window)))
                    value = self.executor.submit(execute).result(timeout=30)
                inference_ms = (time.perf_counter()-started)*1000 if valid else 0
                result = {'schema': 1, 'type': 'result', 'device_id': device, 'run_id': run, 'seq': seq,
                          'sample_time_s': sample['sample_time_s'], 'window_start_seq': max(0, seq-meta['window_length']+1),
                          'window_end_seq': seq, 'model_id': meta['model_id'], 'model_version': meta['version'],
                          'task': meta['task'], 'value': value, 'unit': meta['output_unit'], 'valid': valid,
                          'state': 'valid' if valid else ('warming_up' if len(window) < meta['window_length'] else 'stride_waiting'),
                          'inference_ms': inference_ms, 'mode': meta['mode']}
                self.store.db.execute('INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?)',
                                      (device, run, seq, meta['model_id'], meta['version'], meta['task'], encoded(result), time.time()))
                confirmed = seq
            self.store.db.execute("UPDATE runs SET state='running',confirmed_through_seq=?,updated_at=? WHERE device_id=? AND run_id=?",
                                  (confirmed, time.time(), device, run))
        # ACK is constructed only after transaction commit. PUBACK/HTTP receipt is not substituted.
        return {'schema': 1, 'type': 'ack', 'device_id': device, 'run_id': run, 'batch_id': batch_id,
                'contiguous_through_seq': confirmed, 'missing_ranges': [], 'state': 'persisted'}

    def control(self, device, run, message):
        row = self.row(device, run); action = message.get('action'); state = row['state']
        transitions = {'pause': {'accepted': 'paused', 'running': 'paused', 'paused': 'paused'},
                       'resume': {'paused': 'running', 'running': 'running', 'accepted': 'accepted'},
                       'cancel': {key: 'cancelled' for key in ['accepted', 'running', 'paused', 'cancelled']}}
        if action == 'finish':
            if state == 'completed': return
            if state not in ('running', 'accepted') or row['confirmed_through_seq']+1 != json.loads(row['metadata'])['sample_count']:
                raise ProtocolError('incomplete_run', 'Finish requires all samples durably confirmed', 409)
            target = 'completed'
        else:
            target = transitions.get(action, {}).get(state)
            if target is None: raise ProtocolError('invalid_transition', 'Task control conflicts with terminal/current state', 409)
        with self.store.db:
            if action == 'finish': self.event(device, run, 'draining', 'All synchronous model results are already persisted')
            self.store.db.execute('UPDATE runs SET state=?,updated_at=? WHERE device_id=? AND run_id=?', (target, time.time(), device, run))
            self.event(device, run, target, 'Authorized task control: ' + action)

    def close(self): self.executor.shutdown(wait=True, cancel_futures=True)
