import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

from ab_platform.contracts import Fault, canonical, digest, identity, identifier, integer, new_id, public_deployment, validate

PAIR_SCOPES = {'catalog:read', 'task:submit', 'task:read:own', 'task:cancel:own', 'file:read:own'}
TERMINAL = {'completed', 'failed', 'cancelled', 'rejected'}


@dataclass(frozen=True)
class Principal:
    node_id: str
    role: str
    tenant_id: str = 'default'
    scopes: frozenset = frozenset({'node'})
    token_id: str = ''


class Center:
    def __init__(self, root, *, clock=time.time, max_file=64*1024*1024, quota=256*1024*1024):
        self.root = Path(root).resolve(); self.root.mkdir(parents=True, exist_ok=True)
        self.files = self.root / 'private_files'; self.files.mkdir(exist_ok=True)
        self.clock, self.max_file, self.quota = clock, max_file, quota
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'center.db', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,tenant TEXT,role TEXT,alias TEXT,credential TEXT UNIQUE,
          enabled INTEGER DEFAULT 1,instance TEXT DEFAULT '',epoch INTEGER DEFAULT 0,online_until REAL DEFAULT 0,
          accepting INTEGER DEFAULT 1,load INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS enrollments(hash TEXT PRIMARY KEY,tenant TEXT,role TEXT,expires REAL,used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pair_codes(hash TEXT PRIMARY KEY,b_id TEXT,expires REAL,used INTEGER DEFAULT 0,policy TEXT);
        CREATE TABLE IF NOT EXISTS pairings(id TEXT PRIMARY KEY,tenant TEXT,a_id TEXT,b_id TEXT,policy TEXT,version INTEGER,revoked REAL,
          FOREIGN KEY(a_id) REFERENCES devices(id),FOREIGN KEY(b_id) REFERENCES devices(id));
        CREATE UNIQUE INDEX IF NOT EXISTS active_pair ON pairings(a_id,b_id) WHERE revoked IS NULL;
        CREATE TABLE IF NOT EXISTS catalogs(b_id TEXT PRIMARY KEY,revision TEXT,content TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,tenant TEXT,owner TEXT,task_id TEXT,name TEXT,size INTEGER,sha TEXT,
          offset INTEGER,state TEXT,created REAL,expires REAL);
        CREATE TABLE IF NOT EXISTS parts(upload_id TEXT,offset INTEGER,size INTEGER,sha TEXT,PRIMARY KEY(upload_id,offset));
        CREATE TABLE IF NOT EXISTS upload_attempts(upload_id TEXT PRIMARY KEY,attempt_id TEXT,epoch INTEGER,instance TEXT,session_epoch INTEGER);
        CREATE TABLE IF NOT EXISTS groups_table(id TEXT PRIMARY KEY,tenant TEXT,a_id TEXT,idem TEXT,payload_hash TEXT,created REAL,
          UNIQUE(a_id,idem));
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,group_id TEXT,tenant TEXT,a_id TEXT,b_id TEXT,pairing_id TEXT,
          state TEXT,descriptor TEXT,attempt_id TEXT,lease_epoch INTEGER DEFAULT 0,instance TEXT,session_epoch INTEGER,
          lease_until REAL DEFAULT 0,result TEXT,error TEXT,created REAL,updated REAL);
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,task_id TEXT,epoch INTEGER,instance TEXT,session_epoch INTEGER,
          state TEXT,result TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,target TEXT,tenant TEXT,
          task_id TEXT,kind TEXT,body TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY,target TEXT,body TEXT,sent INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT,action TEXT,resource TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS api_keys(id TEXT PRIMARY KEY,owner TEXT,tenant TEXT,hash TEXT UNIQUE,scopes TEXT,revoked REAL);
        CREATE TABLE IF NOT EXISTS throttles(bucket TEXT PRIMARY KEY,window REAL,count INTEGER);
        PRAGMA user_version=1;''')
        self.db.commit()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.db.execute('BEGIN IMMEDIATE'); yield
                self.db.commit()
            except BaseException:
                self.db.rollback(); raise

    def audit(self, actor, action, resource):
        self.db.execute('INSERT INTO audit(actor,action,resource,created) VALUES(?,?,?,?)', (actor, action, resource, self.clock()))

    def throttle(self, bucket, maximum=20, window=60):
        # Independent commit: failed authentication cannot roll back its own rate limit.
        with self.transaction():
            row = self.db.execute('SELECT * FROM throttles WHERE bucket=?', (bucket,)).fetchone(); now = self.clock()
            count = row['count']+1 if row and now-row['window'] < window else 1
            started = row['window'] if row and now-row['window'] < window else now
            self.db.execute('INSERT OR REPLACE INTO throttles VALUES(?,?,?)', (bucket, started, count))
        if count > maximum: raise Fault('rate_limited', 'Retry after the authentication window', 429)

    def issue_enrollment(self, role, tenant='default', lifetime=300):
        if role not in {'A', 'B'}: raise Fault('role', 'Enrollment role must be A or B')
        code = secrets.token_urlsafe(24)
        with self.transaction():
            self.db.execute('INSERT INTO enrollments VALUES(?,?,?,?,0)', (hashlib.sha256(code.encode()).hexdigest(), tenant, role, self.clock()+lifetime))
        return code

    def enroll(self, code, role, node_id, alias):
        if not identity(node_id) or role not in {'A', 'B'} or not isinstance(alias, str) or not 1 <= len(alias) <= 64:
            raise Fault('enrollment', 'Valid UUID, role and alias required')
        self.throttle('enroll:'+node_id, 5)
        hashed = hashlib.sha256(str(code).encode()).hexdigest(); secret = secrets.token_urlsafe(32)
        with self.transaction():
            row = self.db.execute('SELECT * FROM enrollments WHERE hash=?', (hashed,)).fetchone()
            if not row or row['used'] or row['expires'] <= self.clock() or row['role'] != role:
                raise Fault('enrollment_denied', 'Enrollment code is invalid, expired, consumed or has another role', 403)
            if self.db.execute('SELECT 1 FROM devices WHERE id=?', (node_id,)).fetchone(): raise Fault('identity_exists', 'Identity already registered', 409)
            self.db.execute('INSERT INTO devices(id,tenant,role,alias,credential) VALUES(?,?,?,?,?)',
                            (node_id, row['tenant'], role, alias, hashlib.sha256(secret.encode()).hexdigest()))
            self.db.execute('UPDATE enrollments SET used=1 WHERE hash=?', (hashed,)); self.audit(node_id, 'enroll', node_id)
        return {'node_id': node_id, 'role': role, 'credential': secret, 'tenant_id': row['tenant']}

    def authenticate(self, secret):
        if not isinstance(secret, str) or not 16 <= len(secret) <= 256: raise Fault('authentication_required', 'Valid node or scoped API credential required', 401)
        value = hashlib.sha256(secret.encode()).hexdigest()
        with self.lock:
            row = self.db.execute('SELECT * FROM devices WHERE credential=? AND enabled=1', (value,)).fetchone()
            if row: return Principal(row['id'], row['role'], row['tenant'])
            key = self.db.execute('SELECT k.*,d.role,d.enabled FROM api_keys k JOIN devices d ON d.id=k.owner WHERE k.hash=? AND k.revoked IS NULL', (value,)).fetchone()
            if key and key['enabled']: return Principal(key['owner'], key['role'], key['tenant'], frozenset(json.loads(key['scopes'])), key['id'])
        raise Fault('authentication_required', 'Invalid or revoked credential', 401)

    def scope(self, principal, permission):
        if 'node' not in principal.scopes and permission not in principal.scopes:
            raise Fault('scope_denied', 'Credential does not grant this operation', 403)

    def heartbeat(self, p, instance, epoch=None, accepting=True, load=0):
        if p.token_id or not identity(instance) or type(accepting) is not bool or not integer(load, 0, 1024):
            raise Fault('heartbeat', 'Node credentials and valid instance/health data required')
        with self.transaction():
            row = self.db.execute('SELECT * FROM devices WHERE id=?', (p.node_id,)).fetchone()
            if row['instance'] and row['instance'] != instance and row['online_until'] > self.clock():
                raise Fault('duplicate_instance', 'Another live instance owns this identity; stop or migrate it first', 409)
            actual = row['epoch'] if row['instance'] == instance else row['epoch']+1
            if epoch is not None and epoch != actual: raise Fault('stale_session', 'Session was fenced by another instance', 409)
            self.db.execute('UPDATE devices SET instance=?,epoch=?,online_until=?,accepting=?,load=? WHERE id=?',
                            (instance, actual, self.clock()+60, accepting, load, p.node_id))
        return {'node_id': p.node_id, 'session_epoch': actual, 'lease_seconds': 60}

    def session(self, p, instance, epoch):
        row = self.db.execute('SELECT * FROM devices WHERE id=?', (p.node_id,)).fetchone()
        if not row or row['instance'] != instance or row['epoch'] != epoch or row['online_until'] <= self.clock():
            raise Fault('stale_session', 'Renew the live node session; old instances are fenced', 409)

    def pair_code(self, p, deployments=None, scopes=None, lifetime=300, max_active=2):
        if p.role != 'B' or p.token_id: raise Fault('role_denied', 'Only B node credentials create its pairing codes', 403)
        policy = {'scopes': sorted(PAIR_SCOPES if scopes is None else set(scopes)), 'deployments': deployments,
                  'max_active': max_active}
        if set(policy['scopes']) - PAIR_SCOPES or not integer(max_active, 1, 32) or not integer(lifetime, 30, 300):
            raise Fault('pair_policy', 'Invalid pairing policy')
        if deployments is not None and (not isinstance(deployments, list) or any(not identifier(x) for x in deployments)):
            raise Fault('pair_policy', 'Deployment allowlist must contain IDs')
        code = secrets.token_urlsafe(24)
        with self.transaction():
            self.db.execute('INSERT INTO pair_codes VALUES(?,?,?,0,?)', (hashlib.sha256(code.encode()).hexdigest(), p.node_id, self.clock()+lifetime, canonical(policy).decode()))
        return {'b_id': p.node_id, 'code': code, 'expires_in': lifetime}

    def pair(self, p, b_id, code):
        if p.role != 'A' or p.token_id: raise Fault('role_denied', 'Only A node credentials establish pairings', 403)
        self.throttle('pair:'+p.node_id+':'+str(b_id), 5)
        with self.transaction():
            row = self.db.execute('SELECT c.*,d.tenant FROM pair_codes c JOIN devices d ON d.id=c.b_id WHERE c.hash=? AND c.b_id=?',
                                  (hashlib.sha256(str(code).encode()).hexdigest(), b_id)).fetchone()
            if not row or row['tenant'] != p.tenant_id or row['used'] or row['expires'] <= self.clock():
                raise Fault('pairing_denied', 'Pairing code invalid for this B, expired or consumed', 403)
            if self.db.execute('SELECT 1 FROM pairings WHERE a_id=? AND b_id=? AND revoked IS NULL', (p.node_id, b_id)).fetchone():
                raise Fault('already_paired', 'A current pairing already exists', 409)
            pair_id = new_id()
            self.db.execute('INSERT INTO pairings VALUES(?,?,?,?,?,1,NULL)', (pair_id, p.tenant_id, p.node_id, b_id, row['policy']))
            self.db.execute('UPDATE pair_codes SET used=1 WHERE hash=?', (row['hash'],)); self.audit(p.node_id, 'pair', pair_id)
        return {'pairing_id': pair_id, 'a_id': p.node_id, 'b_id': b_id, 'policy': json.loads(row['policy']), 'version': 1}

    def pairing(self, a_id, b_id, permission, deployment=None):
        row = self.db.execute('SELECT * FROM pairings WHERE a_id=? AND b_id=? AND revoked IS NULL', (a_id, b_id)).fetchone()
        if not row: raise Fault('pairing_revoked', 'No current authorization between these nodes', 403)
        policy = json.loads(row['policy'])
        if permission not in policy['scopes'] or (deployment and policy['deployments'] is not None and deployment not in policy['deployments']):
            raise Fault('pair_scope_denied', 'Pairing does not grant this deployment or operation', 403)
        return row, policy

    def revoke(self, p, pair_id):
        if p.token_id: raise Fault('node_credential_required', 'Scoped clients cannot revoke node pairings', 403)
        with self.transaction():
            row = self.db.execute('SELECT * FROM pairings WHERE id=? AND tenant=?', (pair_id, p.tenant_id)).fetchone()
            if not row or p.node_id not in (row['a_id'], row['b_id']): raise Fault('not_found', 'Pairing not found', 404)
            self.db.execute('UPDATE pairings SET revoked=?,version=version+1 WHERE id=? AND revoked IS NULL', (self.clock(), pair_id))
            tasks = self.db.execute('SELECT * FROM tasks WHERE pairing_id=? AND state NOT IN (?,?,?,?)', (pair_id, *sorted(TERMINAL))).fetchall()
            for task in tasks:
                state = 'cancelled' if task['state'] == 'queued' else 'cancel_requested'
                self.db.execute('UPDATE tasks SET state=?,error=?,updated=? WHERE id=?', (state, 'pairing_revoked', self.clock(), task['id']))
                self.event(task, 'pairing.revoked', {'state': state})
            self.audit(p.node_id, 'revoke_pairing', pair_id)
        return {'revoked': True}

    def list_pairings(self, p):
        self.scope(p, 'nodes:read')
        with self.lock:
            rows = self.db.execute('SELECT * FROM pairings WHERE tenant=? AND (a_id=? OR b_id=?) ORDER BY id', (p.tenant_id, p.node_id, p.node_id)).fetchall()
            return [{k: (json.loads(r[k]) if k == 'policy' else r[k]) for k in r.keys()} for r in rows]

    def nodes(self, p, role=None):
        self.scope(p, 'nodes:read')
        with self.lock:
            rows = self.db.execute('SELECT id,role,alias,online_until,accepting,load FROM devices WHERE tenant=? AND enabled=1', (p.tenant_id,)).fetchall()
            result = []
            for row in rows:
                if row['id'] == p.node_id or (role and row['role'] != role): continue
                relation = self.db.execute('SELECT id FROM pairings WHERE revoked IS NULL AND ((a_id=? AND b_id=?) OR (a_id=? AND b_id=?))',
                                          (p.node_id, row['id'], row['id'], p.node_id)).fetchone()
                if p.role == 'B' and not relation: continue
                result.append({'node_id': row['id'], 'role': row['role'], 'alias': row['alias'], 'online': row['online_until'] > self.clock(),
                               'accepting': bool(row['accepting']), 'load': row['load'], 'paired': bool(relation),
                               'pairing_id': relation[0] if relation else None})
            return result

    def publish_catalog(self, p, deployments):
        if p.role != 'B' or p.token_id: raise Fault('role_denied', 'Only B node credentials publish its catalog', 403)
        if not isinstance(deployments, list) or len(deployments) > 100: raise Fault('catalog_size', 'Catalog has at most 100 deployments')
        checked = [public_deployment(item) for item in deployments]
        keys = [(d['deployment_id'], d['deployment_version']) for d in checked]
        if len(set(keys)) != len(keys): raise Fault('catalog_duplicate', 'Duplicate deployment version')
        content = canonical(checked).decode(); revision = hashlib.sha256(content.encode()).hexdigest()
        with self.transaction():
            self.db.execute('INSERT OR REPLACE INTO catalogs VALUES(?,?,?,?)', (p.node_id, revision, content, self.clock()))
        return {'revision': revision}

    def catalog(self, p, b_id):
        self.scope(p, 'catalog:read')
        with self.lock:
            policy = None
            if p.node_id != b_id:
                if p.role != 'A': raise Fault('role_denied', 'B cannot read another B catalog', 403)
                _, policy = self.pairing(p.node_id, b_id, 'catalog:read')
            row = self.db.execute('SELECT * FROM catalogs WHERE b_id=?', (b_id,)).fetchone()
            if not row: raise Fault('catalog_unavailable', 'B has not published a validated catalog', 404)
            items = json.loads(row['content'])
            if policy and policy['deployments'] is not None: items = [d for d in items if d['deployment_id'] in policy['deployments']]
            return {'revision': row['revision'], 'updated_at': row['updated'], 'deployments': items}

    def create_upload(self, p, name, size, sha256, task_id=None, attempt_id=None, lease_epoch=None, instance=None, session_epoch=None):
        self.scope(p, 'file:write:own')
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or '/' in name or '\\' in name or '\x00' in name:
            raise Fault('file_name', 'Display filename must not contain a path')
        if not integer(size, 0, self.max_file) or not isinstance(sha256, str) or len(sha256) != 64 or any(c not in '0123456789abcdef' for c in sha256):
            raise Fault('file_metadata', 'Declared size and SHA-256 required')
        with self.transaction():
            if p.role == 'B':
                if not task_id: raise Fault('task_required', 'Remote B uploads are scoped to a claimed task')
                task = self.task_access(p, task_id, 'file:read:own')
                self.fence(p, task, instance, session_epoch, attempt_id, lease_epoch)
            elif task_id: raise Fault('role_denied', 'A cannot stage a B result', 403)
            used = self.db.execute('SELECT COALESCE(SUM(size),0) FROM uploads WHERE owner=? AND state<>?', (p.node_id, 'expired')).fetchone()[0]
            if used+size > self.quota: raise Fault('quota_exceeded', 'Per-node private storage quota exceeded', 507)
            file_id = new_id()
            self.db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,?)', (file_id, p.tenant_id, p.node_id, task_id, name, size, sha256, 0, 'uploading', self.clock(), self.clock()+7*86400))
            if task_id: self.db.execute('INSERT INTO upload_attempts VALUES(?,?,?,?,?)', (file_id,attempt_id,lease_epoch,instance,session_epoch))
            (self.files / file_id).touch(exist_ok=False)
        return {'upload_id': file_id, 'file_id': file_id, 'offset': 0, 'part_size': 16384, 'state': 'uploading'}

    def upload_access(self, p, upload_id):
        row = self.db.execute('SELECT * FROM uploads WHERE id=? AND tenant=?', (upload_id, p.tenant_id)).fetchone()
        if not row or row['owner'] != p.node_id: raise Fault('not_found', 'Private upload not found', 404)
        if row['expires'] <= self.clock(): raise Fault('file_expired', 'Private temporary upload expired', 410)
        if row['task_id']:
            task = self.task_access(p, row['task_id'], 'file:read:own')
            binding = self.db.execute('SELECT * FROM upload_attempts WHERE upload_id=?', (upload_id,)).fetchone()
            if not binding: raise Fault('stale_attempt', 'Result upload has no attempt binding', 409)
            self.fence(p, task, binding['instance'],binding['session_epoch'],binding['attempt_id'],binding['epoch'])
        return row

    def upload_status(self, p, upload_id):
        self.scope(p, 'file:write:own')
        with self.lock:
            row = self.upload_access(p, upload_id)
            return {'upload_id': row['id'], 'offset': row['offset'], 'size': row['size'], 'sha256': row['sha'], 'state': row['state']}

    def put_part(self, p, upload_id, offset, data, sha256):
        self.scope(p, 'file:write:own')
        if not integer(offset) or not isinstance(data, bytes) or not 1 <= len(data) <= 16384 or hashlib.sha256(data).hexdigest() != sha256:
            raise Fault('invalid_part', 'Part offset, 16 KiB bound, or SHA-256 is invalid')
        with self.transaction():
            row = self.upload_access(p, upload_id)
            old = self.db.execute('SELECT * FROM parts WHERE upload_id=? AND offset=?', (upload_id, offset)).fetchone()
            if old:
                if old['size'] != len(data) or old['sha'] != sha256: raise Fault('part_conflict', 'Retry bytes differ from durable part', 409)
                return {'offset': row['offset'], 'state': 'persisted', 'duplicate': True}
            if row['state'] != 'uploading' or row['offset'] != offset or offset+len(data) > row['size']:
                raise Fault('offset_conflict', 'Resume from the durable upload offset', 409)
            with (self.files / upload_id).open('r+b') as file:
                file.seek(offset); file.write(data); file.flush(); os.fsync(file.fileno())
            self.db.execute('INSERT INTO parts VALUES(?,?,?,?)', (upload_id, offset, len(data), sha256))
            self.db.execute('UPDATE uploads SET offset=? WHERE id=?', (offset+len(data), upload_id))
        return {'offset': offset+len(data), 'state': 'persisted', 'duplicate': False}

    def finalize(self, p, upload_id):
        self.scope(p, 'file:write:own')
        with self.transaction():
            row = self.upload_access(p, upload_id)
            if row['state'] == 'ready': return {'file_id': upload_id, 'state': 'ready', 'sha256': row['sha'], 'size': row['size']}
            if row['offset'] != row['size']: raise Fault('incomplete_file', 'Not all bytes have a durable ACK', 409)
            sha = hashlib.sha256()
            with (self.files / upload_id).open('rb') as file:
                while True:
                    data = file.read(65536)
                    if not data: break
                    sha.update(data)
            if sha.hexdigest() != row['sha']: raise Fault('file_hash', 'Whole-file integrity check failed', 409)
            self.db.execute("UPDATE uploads SET state='ready' WHERE id=?", (upload_id,))
        return {'file_id': upload_id, 'state': 'ready', 'sha256': row['sha'], 'size': row['size']}

    def file_access(self, p, file_id):
        self.scope(p, 'file:read:own')
        row = self.db.execute('SELECT * FROM uploads WHERE id=? AND tenant=?', (file_id, p.tenant_id)).fetchone()
        if not row or row['state'] != 'ready': raise Fault('not_found', 'Private completed file not found', 404)
        if row['expires'] <= self.clock(): raise Fault('file_expired', 'Center temporary copy expired; request republication from B', 410)
        if row['owner'] == p.node_id:
            if row['task_id']: self.task_access(p, row['task_id'], 'file:read:own')
            return row
        if row['task_id']:
            self.task_access(p, row['task_id'], 'file:read:own'); return row
        if p.role != 'B': raise Fault('not_found', 'Private file not found', 404)
        tasks = self.db.execute('SELECT * FROM tasks WHERE b_id=? AND tenant=?', (p.node_id, p.tenant_id)).fetchall()
        for task in tasks:
            descriptor = json.loads(task['descriptor'])
            if file_id in self.input_files(descriptor['inputs'], descriptor['deployment'].get('inputs', [])):
                try: self.task_access(p, task['id'], 'file:read:own')
                except Fault: continue
                if task['state'] in {'accepted', 'running', 'result_staging', 'completed'}: return row
        raise Fault('not_found', 'File is not granted to this B task', 404)

    def read_file(self, p, file_id, offset=0, length=None):
        with self.lock:
            row = self.file_access(p, file_id)
            if not integer(offset, 0, row['size']): raise Fault('range', 'Invalid file range', 416)
            count = min(row['size']-offset, row['size'] if length is None else length)
            metadata = {'file_id': file_id, 'size': row['size'], 'sha256': row['sha'], 'name': row['name'], 'length': count}
        def chunks():
            with (self.files / file_id).open('rb') as file:
                file.seek(offset); remaining = count
                while remaining:
                    with self.lock: self.file_access(p, file_id)
                    data = file.read(min(16384, remaining))
                    if not data: raise Fault('file_truncated', 'Private file is truncated', 500)
                    remaining -= len(data); yield data
        return metadata, chunks()

    @staticmethod
    def input_files(inputs, specifications):
        result = []
        names = {spec['id'] for spec in specifications if spec['kind'] in {'file','files'}}
        for name, value in inputs.items():
            if name not in names: continue
            if isinstance(value, str) and identity(value): result.append(value)
            elif isinstance(value, list): result.extend(v for v in value if isinstance(v, str) and identity(v))
        return result

    def validate_inputs(self, p, deployment, inputs, parameters):
        if not isinstance(inputs, dict) or not isinstance(parameters, dict): raise Fault('task_input', 'Inputs and parameters must be objects')
        specs = {spec['id']: spec for spec in deployment['inputs']}
        if inputs.keys()-specs.keys(): raise Fault('input_contract', 'Unknown deployment input')
        for name, spec in specs.items():
            if name not in inputs:
                if spec.get('required', True): raise Fault('missing_input', 'Required deployment input is missing')
                continue
            value = inputs[name]
            if spec['kind'] in {'file', 'files'}:
                values = value if spec['kind'] == 'files' else [value]
                if not isinstance(values, list) or not 1 <= len(values) <= 8: raise Fault('file_inputs', 'File inputs are bounded to 1–8 references')
                for file_id in values:
                    row = self.upload_access(p, file_id)
                    if row['state'] != 'ready' or Path(row['name']).suffix.lower().lstrip('.') not in spec['formats']:
                        raise Fault('file_input', 'Complete an owned file with a declared format first')
            elif spec['kind'] == 'text': validate(value, spec.get('schema', {'type':'string','maxLength':4096}), 'inputs.'+name)
            else: validate(value, spec.get('schema', {'type': spec['kind']}), 'inputs.'+name)
        validate(parameters, deployment['parameters_schema'])

    def create_group(self, p, key, inputs, targets):
        self.scope(p, 'task:submit')
        if p.role != 'A' or not identifier(key) or not isinstance(targets, list) or not 1 <= len(targets) <= 8:
            raise Fault('group_contract', 'A submits 1–8 targets with an idempotency key')
        payload_hash = digest({'inputs': inputs, 'targets': targets})
        with self.transaction():
            old = self.db.execute('SELECT * FROM groups_table WHERE a_id=? AND idem=?', (p.node_id, key)).fetchone()
            if old:
                if old['payload_hash'] != payload_hash: raise Fault('idempotency_conflict', 'Logical retry changed the task request', 409)
                return self.get_group(p, old['id'])
            group = new_id(); now = self.clock()
            self.db.execute('INSERT INTO groups_table VALUES(?,?,?,?,?,?)', (group, p.tenant_id, p.node_id, key, payload_hash, now))
            for target in targets:
                task_id = new_id(); state, error, pair_id = 'queued', None, ''
                descriptor = {'inputs': inputs, 'parameters': target.get('parameters', {}), 'deployment': {}, 'catalog_revision': ''}
                b_id = target.get('node_id', '')
                try:
                    if not identity(b_id): raise Fault('target', 'Target B identity is invalid')
                    pair, policy = self.pairing(p.node_id, b_id, 'task:submit', target.get('deployment_id'))
                    pair_id = pair['id']; node = self.db.execute('SELECT * FROM devices WHERE id=? AND tenant=? AND role=? AND enabled=1', (b_id, p.tenant_id, 'B')).fetchone()
                    if not node or node['online_until'] <= now or not node['accepting']: raise Fault('b_unavailable', 'B is offline or not accepting tasks', 409)
                    catalog = self.catalog(p, b_id)
                    item = next((d for d in catalog['deployments'] if d['deployment_id'] == target.get('deployment_id') and d['deployment_version'] == target.get('deployment_version')), None)
                    if not item or item['availability'] != 'ready': raise Fault('deployment_unavailable', 'Exact deployment version is unavailable', 409)
                    active = self.db.execute('SELECT COUNT(*) FROM tasks WHERE pairing_id=? AND state NOT IN (?,?,?,?)', (pair_id, *sorted(TERMINAL))).fetchone()[0]
                    if active >= policy['max_active']: raise Fault('pair_quota', 'Pairing active-task quota reached', 429)
                    self.validate_inputs(p, item, inputs, descriptor['parameters'])
                    descriptor['input_metadata']={}
                    for file_id in self.input_files(inputs,item['inputs']):
                        file=self.upload_access(p,file_id)
                        descriptor['input_metadata'][file_id]={'file_id':file_id,'name':file['name'],'size':file['size'],'sha256':file['sha']}
                    descriptor['deployment'], descriptor['catalog_revision'] = item, catalog['revision']
                except Fault as failure:
                    state, error = 'rejected', failure.code
                self.db.execute('INSERT INTO tasks(id,group_id,tenant,a_id,b_id,pairing_id,state,descriptor,error,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (task_id, group, p.tenant_id, p.node_id, b_id, pair_id, state, canonical(descriptor).decode(), error, now, now))
                task = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
                self.event(task, 'task.'+state, {'state': state})
        return self.get_group(p, group)

    def task_access(self, p, task_id, permission='task:read:own'):
        self.scope(p, permission)
        row = self.db.execute('SELECT * FROM tasks WHERE id=? AND tenant=?', (task_id, p.tenant_id)).fetchone()
        if not row or p.node_id not in (row['a_id'], row['b_id']): raise Fault('not_found', 'Task not owned by this node', 404)
        pair, _ = self.pairing(row['a_id'], row['b_id'], permission, json.loads(row['descriptor'])['deployment'].get('deployment_id'))
        if pair['id'] != row['pairing_id']: raise Fault('pairing_replaced', 'New pairing cannot resurrect an old authorization', 403)
        return row

    def task_dict(self, row):
        return {**{k: row[k] for k in ['id','group_id','a_id','b_id','pairing_id','state','attempt_id','lease_epoch','error','created','updated']},
                'task_id': row['id'], 'snapshot': json.loads(row['descriptor']), 'result': json.loads(row['result']) if row['result'] else None}

    def get_task(self, p, task_id):
        with self.lock: return self.task_dict(self.task_access(p, task_id))

    def list_tasks(self,p,active=False,limit=16,offset=0):
        self.scope(p,'task:read:own')
        if not integer(limit,1,32) or not integer(offset):raise Fault('page','Invalid bounded task page')
        with self.lock:
            sql='SELECT * FROM tasks WHERE tenant=? AND (a_id=? OR b_id=?) '
            parameters=[p.tenant_id,p.node_id,p.node_id]
            if active:sql+='AND state NOT IN (?,?,?,?) ';parameters.extend(sorted(TERMINAL))
            sql+='ORDER BY created DESC LIMIT ? OFFSET ?';parameters.extend([limit,offset])
            rows=self.db.execute(sql,parameters).fetchall();result=[]
            for row in rows:
                if active and row['state'] in TERMINAL:continue
                try:result.append(self.task_dict(self.task_access(p,row['id'])))
                except Fault:continue
            return result

    def get_group(self, p, group_id):
        self.scope(p, 'task:read:own')
        with self.lock:
            group = self.db.execute('SELECT * FROM groups_table WHERE id=? AND tenant=? AND a_id=?', (group_id, p.tenant_id, p.node_id)).fetchone()
            if not group: raise Fault('not_found', 'Task group not owned by this node', 404)
            tasks = []
            for row in self.db.execute('SELECT * FROM tasks WHERE group_id=? ORDER BY created,id', (group_id,)):
                try: tasks.append(self.task_dict(self.task_access(p, row['id'])))
                except Fault: tasks.append({'task_id': row['id'], 'state': 'access_revoked' if row['state'] != 'rejected' else 'rejected', 'error': row['error'] or 'authorization_required'})
            return {'group_id': group_id, 'tasks': tasks}

    def pending(self, p, instance, epoch):
        if p.role != 'B' or p.token_id: raise Fault('role_denied', 'B node credentials required', 403)
        with self.lock:
            self.session(p, instance, epoch); result = []
            for row in self.db.execute("SELECT * FROM tasks WHERE b_id=? AND state IN ('queued','accepted','running','cancel_requested') ORDER BY created LIMIT 32", (p.node_id,)):
                try: result.append(self.task_dict(self.task_access(p, row['id'])))
                except Fault: continue
            return result

    def event(self, task, kind, details):
        for node in {task['a_id'], task['b_id']}:
            if not identity(node): continue
            event_id = new_id(); body = {'schema_version':1,'event_id':event_id,'event_type':kind,'task_id':task['id'],'target_node_id':node,**details}
            self.db.execute('INSERT INTO events(id,target,tenant,task_id,kind,body,created) VALUES(?,?,?,?,?,?,?)',
                            (event_id, node, task['tenant'], task['id'], kind, canonical(body).decode(), self.clock()))
            self.db.execute('INSERT INTO outbox VALUES(?,?,?,0)', (event_id, node, canonical(body).decode()))

    def claim(self, p, task_id, instance, session_epoch):
        with self.transaction():
            task = self.task_access(p, task_id); self.session(p, instance, session_epoch)
            if p.role != 'B' or p.node_id != task['b_id'] or p.token_id: raise Fault('claim_denied', 'Only assigned B node may claim', 403)
            if task['state'] in {'accepted','running'} and task['lease_until'] > self.clock() and task['instance'] == instance and task['session_epoch'] == session_epoch:
                return self.task_dict(task)
            if task['state'] != 'queued': raise Fault('claim_conflict', 'Task needs explicit retry; no blind duplicate execution', 409)
            # Catalog may have been disabled after submission: recheck at acceptance.
            catalog = self.db.execute('SELECT content FROM catalogs WHERE b_id=?', (p.node_id,)).fetchone()
            frozen = json.loads(task['descriptor'])['deployment']
            current = next((d for d in json.loads(catalog[0]) if d.get('deployment_id') == frozen.get('deployment_id') and d.get('deployment_version') == frozen.get('deployment_version')), None) if catalog else None
            if current != frozen or current.get('availability') != 'ready': raise Fault('catalog_changed', 'Frozen deployment no longer matches B catalog', 409)
            attempt = new_id(); epoch = task['lease_epoch']+1
            self.db.execute("UPDATE tasks SET state='accepted',attempt_id=?,lease_epoch=?,instance=?,session_epoch=?,lease_until=?,updated=? WHERE id=?",
                            (attempt, epoch, instance, session_epoch, self.clock()+60, self.clock(), task_id))
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,NULL,?)', (attempt, task_id, epoch, instance, session_epoch, 'accepted', self.clock()))
            task = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone(); self.event(task, 'task.accepted', {'attempt_id':attempt,'lease_epoch':epoch})
        return self.get_task(p, task_id)

    def fence(self, p, task, instance, session_epoch, attempt, epoch):
        self.session(p, instance, session_epoch)
        if p.role != 'B' or task['b_id'] != p.node_id or task['instance'] != instance or task['session_epoch'] != session_epoch or task['attempt_id'] != attempt or task['lease_epoch'] != epoch or (task['lease_until'] <= self.clock() and task['state'] not in TERMINAL):
            raise Fault('stale_attempt', 'Attempt or instance lease is stale; output was not accepted', 409)

    def renew(self, p, task_id, instance, session_epoch, attempt, epoch):
        with self.transaction():
            task = self.task_access(p, task_id); self.fence(p, task, instance, session_epoch, attempt, epoch)
            if task['state'] in TERMINAL: raise Fault('terminal_task', 'Terminal task cannot be renewed', 409)
            self.db.execute('UPDATE tasks SET lease_until=? WHERE id=?', (self.clock()+60, task_id))
        return {'lease_seconds':60}

    def transition(self, p, task_id, state, instance, session_epoch, attempt, epoch, result=None):
        with self.transaction():
            task = self.task_access(p, task_id); self.fence(p, task, instance, session_epoch, attempt, epoch)
            if task['state'] == state and state in TERMINAL:
                if state == 'completed' and canonical(result).decode() != task['result']: raise Fault('result_conflict', 'Terminal result differs from durable result', 409)
                return self.task_dict(task)
            allowed = {'accepted':{'running','failed','cancelled'},'running':{'result_staging','failed','cancelled'},
                       'result_staging':{'completed','failed','cancelled'},'cancel_requested':{'cancelled'}}
            if state not in allowed.get(task['state'], set()): raise Fault('state_conflict', 'Invalid task transition; terminal results are immutable', 409)
            raw = None
            if state == 'completed':
                if not isinstance(result, dict) or result.get('task_id') != task_id or result.get('attempt_id') != attempt or len(canonical(result)) > 8192:
                    raise Fault('result_contract', 'Bounded result with matching task and attempt required')
                for artifact in result.get('artifacts', []):
                    file = self.db.execute('SELECT * FROM uploads WHERE id=? AND owner=? AND task_id=? AND state=?', (artifact.get('id'), p.node_id, task_id, 'ready')).fetchone()
                    if not file: raise Fault('result_file', 'Every result artifact must be private, complete and task-owned')
                    if artifact.get('sha256') != file['sha']: raise Fault('result_hash', 'Result artifact hash does not match staged bytes')
                raw = canonical(result).decode()
            self.db.execute('UPDATE tasks SET state=?,result=COALESCE(?,result),updated=? WHERE id=?', (state, raw, self.clock(), task_id))
            self.db.execute('UPDATE attempts SET state=?,result=COALESCE(?,result) WHERE id=?', (state, raw, attempt))
            updated = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone(); self.event(updated, 'task.'+state, {'attempt_id':attempt,'lease_epoch':epoch})
        return self.get_task(p, task_id)

    def cancel(self, p, task_id):
        with self.transaction():
            task = self.task_access(p, task_id, 'task:cancel:own')
            if p.node_id != task['a_id']: raise Fault('cancel_denied', 'Only task originator requests remote cancellation', 403)
            if task['state'] in TERMINAL: return self.task_dict(task)
            state = 'cancelled' if task['state'] == 'queued' else 'cancel_requested'
            self.db.execute('UPDATE tasks SET state=?,updated=? WHERE id=?', (state, self.clock(), task_id)); self.event(task, 'task.'+state, {'state':state})
        return self.get_task(p, task_id)

    def events(self, p, after=0, limit=32):
        self.scope(p, 'task:read:own')
        if not integer(after) or not integer(limit,1,32): raise Fault('event_cursor', 'Invalid bounded event cursor')
        with self.lock:
            rows = self.db.execute('SELECT * FROM events WHERE target=? AND tenant=? AND seq>? ORDER BY seq LIMIT ?', (p.node_id, p.tenant_id, after, limit)).fetchall()
            result = []
            for row in rows:
                try: self.task_access(p, row['task_id'])
                except Fault: continue
                result.append({**json.loads(row['body']), 'event_seq':row['seq']})
            return {'events':result,'cursor':rows[-1]['seq'] if rows else after}

    def api_key(self, p, scopes):
        allowed = {'nodes:read','catalog:read','task:submit','task:read:own','task:cancel:own','file:write:own','file:read:own'}
        if p.token_id or not isinstance(scopes,list) or not set(scopes) <= allowed: raise Fault('scope_denied', 'Node owner may create only bounded API grants', 403)
        secret = secrets.token_urlsafe(32); key_id = new_id()
        with self.transaction(): self.db.execute('INSERT INTO api_keys VALUES(?,?,?,?,?,NULL)', (key_id,p.node_id,p.tenant_id,hashlib.sha256(secret.encode()).hexdigest(),canonical(scopes).decode()))
        return {'id':key_id,'token':secret,'scopes':scopes}

    def revoke_key(self, p, key_id):
        if p.token_id: raise Fault('scope_denied', 'Node owner required', 403)
        with self.transaction(): self.db.execute('UPDATE api_keys SET revoked=? WHERE id=? AND owner=?', (self.clock(),key_id,p.node_id))
        return {'revoked':True}

    def rotate(self, p):
        if p.token_id: raise Fault('scope_denied', 'Node owner required', 403)
        secret = secrets.token_urlsafe(32)
        with self.transaction():
            self.db.execute("UPDATE devices SET credential=?,epoch=epoch+1,instance='',online_until=0 WHERE id=?", (hashlib.sha256(secret.encode()).hexdigest(),p.node_id))
            self.audit(p.node_id,'rotate_credential',p.node_id)
        return {'node_id':p.node_id,'credential':secret}

    def backup(self, directory):
        directory = Path(directory); directory.mkdir(parents=True,exist_ok=False)
        import shutil
        with self.lock:
            target = sqlite3.connect(directory/'center.db')
            try: self.db.backup(target)
            finally: target.close()
            shutil.copytree(self.files,directory/'private_files')
        return directory

    def close(self): self.db.close()
