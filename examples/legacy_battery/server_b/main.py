"""Battery Cloud Lab HTTP(S) service. Standard-library runtime; no environment installation.

HTTPS RPC is the explicit alternative to MQTT allowed by the project architecture.
This server does not claim to be a MQTT broker. Public deployments require TLS.
"""
import argparse
import csv
import hashlib
import hmac
import http.server
import io
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import secrets
import shutil
import socketserver
import ssl
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from .data_store import DataStore
from .inference import ModelRegistry
from .run_manager import ProtocolError, RunManager, identifier

SERVER_ROOT = Path(__file__).resolve().parents[1]


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON')))


def load_config(runtime):
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / 'config.json'
    if not path.exists():
        password = secrets.token_hex(16)
        config = {'schema': 1, 'admin_hash': hashlib.sha256(password.encode()).hexdigest(),
                  'device_tokens': {'A01': secrets.token_hex(32)}, 'max_database_bytes': 256 * 1024 * 1024}
        path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
        (runtime / 'admin_password.txt').write_text(password + '\n', encoding='ascii')
    config = strict_json(path.read_bytes())
    if config.get('schema') != 1 or not isinstance(config.get('device_tokens'), dict) or len(config.get('admin_hash', '')) != 64:
        raise ValueError('Invalid local B configuration')
    for device, token in config['device_tokens'].items():
        if not identifier(device) or not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError('Device IDs and tokens must be independently provisioned')
    return config


class Service:
    def __init__(self, runtime, config=None):
        self.runtime = Path(runtime)
        self.config = config or load_config(runtime)
        self.store = DataStore(self.runtime / 'battery.db', self.config['max_database_bytes'])
        self.registry = ModelRegistry(self.runtime / 'models.json', self.runtime / 'private_models')
        self.manager = RunManager(self.store, self.registry)
        self.sessions = {}
        self.session_lock = threading.RLock()
        self.login_failures = {}
        self.started = time.monotonic()

    def close(self):
        self.manager.close(); self.store.close()


class BoundedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service):
        self.slots = threading.BoundedSemaphore(8)
        self.service = service
        self.tls = False
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        request.settimeout(15)
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request); return
        try: super().process_request(request, client_address)
        except BaseException:
            self.slots.release(); raise

    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self.slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'BatteryCloudLab/0.3'

    def log_message(self, format, *args):
        # Do not record URLs, query strings, tokens, passwords or sample bodies.
        logging.getLogger('battery_b').info('HTTP %s from %s', self.command, self.client_address[0])

    @property
    def service(self): return self.server.service

    def send_json(self, data, status=200, headers=None):
        raw = json.dumps(data, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in (headers or {}).items(): self.send_header(key, value)
        self.end_headers(); self.wfile.write(raw)

    def read_json(self):
        if self.headers.get('Transfer-Encoding'):
            raise ProtocolError('content_length_required', 'Chunked request bodies are not accepted', 411)
        try: length = int(self.headers.get('Content-Length', '-1'))
        except ValueError: length = -1
        if not 0 <= length <= 8192: raise ProtocolError('body_size', 'JSON request exceeds 8192 bytes', 413)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ProtocolError('content_type', 'Use application/json', 415)
        raw = self.rfile.read(length)
        if len(raw) != length: raise ProtocolError('truncated_body', 'Incomplete request body', 400)
        try: data = strict_json(raw)
        except (ValueError, UnicodeError) as exc: raise ProtocolError('invalid_json', 'Invalid, duplicate-key or non-finite JSON', 400) from exc
        if not isinstance(data, dict): raise ProtocolError('json_object_required', 'Request must be a JSON object', 400)
        return data

    def same_origin(self):
        origin = self.headers.get('Origin')
        if origin is None: return True  # Native admin clients still require credentials.
        parsed = urlsplit(origin)
        return parsed.scheme == ('https' if self.server.tls else 'http') and parsed.netloc == self.headers.get('Host') and parsed.path in ('', '/')

    def admin(self, write=False):
        token = None
        for part in self.headers.get('Cookie', '').split(';'):
            if part.strip().startswith('battery_b='): token = part.strip().split('=', 1)[1]
        with self.service.session_lock:
            self.service.sessions = {k: v for k, v in self.service.sessions.items() if v['expires'] > time.monotonic()}
            session = self.service.sessions.get(token)
            if session is None: raise ProtocolError('authentication_required', 'B administrator login required', 401)
            if write and (not self.same_origin() or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf'])):
                raise ProtocolError('csrf_rejected', 'Invalid management origin or CSRF token', 403)
            return session

    def do_POST(self):
        message = {}; device = ''
        try:
            path = urlsplit(self.path).path
            if path == '/api/edge/rpc':
                if self.headers.get('Origin') is not None:
                    raise ProtocolError('edge_only', 'Browsers may not bypass ESP32 and call the edge data API', 403)
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                device = next((name for name, secret in self.service.config['device_tokens'].items()
                               if hmac.compare_digest(token, secret)), '')
                if not device: raise ProtocolError('device_authentication', 'Invalid device credential', 401)
                message = self.read_json()
                self.send_json(self.service.manager.handle(device, message)); return
            if path == '/api/session':
                if not self.same_origin(): raise ProtocolError('origin_rejected', 'Invalid login origin', 403)
                message = self.read_json(); password = message.get('password')
                if not isinstance(password, str) or len(password) > 128:
                    raise ProtocolError('invalid_credentials', 'Invalid administrator password', 401)
                peer = self.client_address[0]; now = time.monotonic()
                with self.service.session_lock:
                    if now < self.service.login_failures.get(peer, 0):
                        raise ProtocolError('login_rate_limit', 'Retry in three seconds', 429)
                    self.service.login_failures[peer] = now + 3
                    if not hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), self.service.config['admin_hash']):
                        raise ProtocolError('invalid_credentials', 'Invalid administrator password', 401)
                    token = secrets.token_hex(32); csrf = secrets.token_hex(32)
                    self.service.sessions[token] = {'csrf': csrf, 'expires': now+3600}
                    if len(self.service.sessions) > 16:
                        oldest = min(self.service.sessions, key=lambda k: self.service.sessions[k]['expires'])
                        del self.service.sessions[oldest]
                secure = '; Secure' if self.server.tls else ''
                self.send_json({'authenticated': True, 'csrf': csrf}, headers={
                    'Set-Cookie': f'battery_b={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600{secure}'})
                return
            self.admin(write=True); message = self.read_json()
            parts = path.strip('/').split('/')
            if len(parts) == 5 and parts[:2] == ['api', 'runs'] and parts[4] == 'control':
                device, run = parts[2:4]
                if not identifier(device) or not identifier(run): raise ProtocolError('invalid_id', 'Invalid device/run ID')
                result = self.service.manager.handle(device, {'schema': 1, 'device_id': device, 'request_id': secrets.token_hex(16),
                    'run_id': run, 'operation': 'control', 'action': message.get('action')})
                self.send_json(result); return
            if path == '/api/backup':
                target = self.service.runtime / 'backups' / f'battery-{time.time_ns()}.db'
                self.service.store.backup(target)
                self.send_json({'file': target.name, 'bytes': target.stat().st_size,
                                'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}); return
            if path == '/api/models/register':
                raise ProtocolError('local_model_registration', 'Install a trusted model manifest locally; model binaries are never accepted from HTTP', 409)
            raise ProtocolError('not_found', 'Management endpoint not found', 404)
        except ProtocolError as exc:
            response = {'ok': False, 'code': exc.code, 'message': exc.message, 'schema': 1,
                        'device_id': device, 'request_id': message.get('request_id', '')}
            self.send_json(response, exc.status)
        except (ValueError, KeyError, TypeError) as exc:
            logging.getLogger('battery_b').warning('Rejected invalid request: %s', type(exc).__name__)
            self.send_json({'ok': False, 'code': 'invalid_request', 'message': 'Request or model input is invalid',
                            'schema': 1, 'device_id': device, 'request_id': message.get('request_id', '')}, 422)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            logging.getLogger('battery_b').exception('Service request failed')
            self.send_json({'ok': False, 'code': 'service_error', 'message': 'B failed to process request; no successful ACK issued',
                            'schema': 1, 'device_id': device, 'request_id': message.get('request_id', '')}, 500)

    def do_GET(self):
        try:
            parsed = urlsplit(self.path); path = unquote(parsed.path); query = parse_qs(parsed.query)
            if path == '/health':
                with self.service.store.lock:
                    self.service.store.db.execute('SELECT 1').fetchone()
                self.send_json({'service': 'ok', 'database': 'ok', 'transport': 'https-rpc',
                                'uptime_s': time.monotonic()-self.service.started, 'tls': self.server.tls,
                                'models_loaded': len(self.service.registry.models)}); return
            if path == '/api/session':
                session = self.admin(); self.send_json({'authenticated': True, 'csrf': session['csrf']}); return
            if path.startswith('/api/'):
                self.admin()
                with self.service.store.lock:
                    self.get_api(path, query)
                return
            name = 'index.html' if path == '/' else path.removeprefix('/')
            if name not in {'index.html', 'app.js', 'style.css'}:
                raise ProtocolError('not_found', 'Resource not found', 404)
            resource = SERVER_ROOT / 'web' / name
            raw = resource.read_bytes()
            content_type = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}[name.rsplit('.', 1)[1]]
            self.send_response(200); self.send_header('Content-Type', content_type); self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store'); self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers(); self.wfile.write(raw)
        except ProtocolError as exc: self.send_json({'code': exc.code, 'message': exc.message}, exc.status)
        except (ValueError, KeyError): self.send_json({'code': 'invalid_query', 'message': 'Invalid bounded query'}, 400)
        except (BrokenPipeError, ConnectionResetError, TimeoutError): self.close_connection = True
        except Exception:
            logging.getLogger('battery_b').exception('Read request failed')
            self.send_json({'code': 'service_error', 'message': 'B could not serve the request'}, 500)

    def get_api(self, path, query):
        db = self.service.store.db
        limit = min(512, max(1, int(query.get('limit', ['100'])[0])))
        offset = max(0, int(query.get('offset', ['0'])[0]))
        if path == '/api/dashboard':
            now = time.time()
            self.send_json({'online_devices': db.execute('SELECT COUNT(*) FROM devices WHERE last_seen>?', (now-30,)).fetchone()[0],
                'active_runs': db.execute("SELECT COUNT(*) FROM runs WHERE state IN ('accepted','running','paused')").fetchone()[0],
                'samples': db.execute('SELECT COUNT(*) FROM samples').fetchone()[0],
                'predictions': db.execute('SELECT COUNT(*) FROM predictions').fetchone()[0],
                'database_bytes': self.service.store.bytes_used(), 'quota_bytes': self.service.store.max_bytes,
                'disk_free_bytes': shutil.disk_usage(self.service.runtime).free,
                'uptime_s': time.monotonic()-self.service.started, 'threads': threading.active_count(),
                'transport': 'https-rpc', 'mode': 'demo_only' if all(m.metadata()['mode'] == 'demo' for m in self.service.registry.models.values()) else 'mixed',
                'cpu_percent': None, 'process_rss_bytes': None}); return
        if path == '/api/models': self.send_json({'models': self.service.registry.metadata()}); return
        if path == '/api/devices':
            rows = [dict(r) for r in db.execute('SELECT * FROM devices ORDER BY last_seen DESC LIMIT ? OFFSET ?', (limit, offset))]
            for row in rows: row['online'] = time.time()-row['last_seen'] < 30; row['metadata'] = json.loads(row['metadata'])
            self.send_json({'devices': rows}); return
        if path == '/api/runs':
            rows = db.execute('SELECT device_id,run_id FROM runs ORDER BY updated_at DESC LIMIT ? OFFSET ?', (limit, offset))
            self.send_json({'runs': [self.service.manager.summary(row[0], row[1]) for row in rows], 'limit': limit, 'offset': offset}); return
        if path == '/api/events':
            self.send_json({'events': [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset))]}); return
        parts = path.strip('/').split('/')
        if len(parts) not in (4, 5) or parts[:2] != ['api', 'runs']:
            raise ProtocolError('not_found', 'API endpoint not found', 404)
        device, run = parts[2:4]
        if not identifier(device) or not identifier(run): raise ProtocolError('invalid_id', 'Invalid device/run ID')
        summary = self.service.manager.summary(device, run)
        if len(parts) == 4: self.send_json(summary); return
        suffix = parts[4]
        after = int(query.get('after', ['-1'])[0])
        if after < -1: raise ValueError('Invalid cursor')
        if suffix == 'samples':
            rows = db.execute('SELECT seq,sample_time_s,voltage_v,current_a,temperature_c,received_at FROM samples WHERE device_id=? AND run_id=? AND seq>? ORDER BY seq LIMIT ?', (device, run, after, limit))
            self.send_json({'samples': [dict(r) for r in rows]}); return
        if suffix == 'predictions':
            rows = db.execute('SELECT result FROM predictions WHERE device_id=? AND run_id=? AND seq>? ORDER BY seq LIMIT ?', (device, run, after, limit))
            self.send_json({'predictions': [json.loads(r[0]) for r in rows]}); return
        if suffix == 'export': self.export(device, run, after); return
        raise ProtocolError('not_found', 'Run endpoint not found', 404)

    def export(self, device, run, after):
        # Stream bounded pages; never construct the full database export in memory.
        self.send_response(200); self.send_header('Content-Type', 'text/csv; charset=utf-8')
        self.send_header('Content-Disposition', f'attachment; filename="{device}-{run}.csv"')
        self.send_header('X-Export-Max-Rows', '100000'); self.send_header('Connection', 'close'); self.end_headers()
        buffer = io.StringIO(); writer = csv.writer(buffer)
        fields = ['device_id', 'run_id', 'seq', 'sample_time_s', 'voltage_v', 'current_a', 'temperature_c', 'predicted_soc',
                  'valid', 'model_id', 'model_version', 'inference_ms', 'mode', 'source']
        writer.writerow(fields); self.wfile.write(buffer.getvalue().encode('utf-8-sig')); buffer.seek(0); buffer.truncate(0)
        source = self.service.manager.summary(device, run)['metadata']['source']
        rows = self.service.store.db.execute('''SELECT s.*,p.result FROM samples s JOIN predictions p USING(device_id,run_id,seq)
            WHERE s.device_id=? AND s.run_id=? AND s.seq>? ORDER BY s.seq LIMIT 100000''', (device, run, after))
        for sample in rows:
            result = json.loads(sample['result'])
            writer.writerow([device, run, sample['seq'], sample['sample_time_s'], sample['voltage_v'], sample['current_a'], sample['temperature_c'],
                             result['value'], result['valid'], result['model_id'], result['model_version'], result['inference_ms'], result['mode'], source])
            self.wfile.write(buffer.getvalue().encode('utf-8')); buffer.seek(0); buffer.truncate(0)
        self.close_connection = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--runtime', type=Path, default=SERVER_ROOT / '.runtime')
    parser.add_argument('--cert', type=Path)
    parser.add_argument('--key', type=Path)
    args = parser.parse_args()
    args.runtime.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('battery_b'); logger.setLevel(logging.INFO)
    logger.addHandler(RotatingFileHandler(args.runtime / 'service.log', maxBytes=2*1024*1024, backupCount=3, encoding='utf-8'))
    service = Service(args.runtime); server = BoundedServer((args.host, args.port), service)
    if args.cert or args.key:
        if not args.cert or not args.key: parser.error('Both --cert and --key are required')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key); server.socket = context.wrap_socket(server.socket, server_side=True); server.tls = True
    if args.host not in {'127.0.0.1', '::1', 'localhost'} and not server.tls:
        server.server_close(); service.close(); parser.error('Non-loopback deployment requires TLS; use a trusted certificate')
    print(f"Battery Cloud Lab: {'https' if server.tls else 'http'}://{args.host}:{server.server_port}", flush=True)
    print(f'Local credentials: {args.runtime / "config.json"}; admin password file remains local.', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); service.close()


if __name__ == '__main__': main()
