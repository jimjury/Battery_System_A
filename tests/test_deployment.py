import contextlib
import hashlib
import hmac
import http.server
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import deploy_web
import release
from web_bundle import HEADER, ENTRY, build, sha256, validate


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'index.html').write_text('<!doctype html><title>Test</title>', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_deterministic_roundtrip(self):
        (self.root / 'app.js').write_text('console.log(1);', encoding='utf-8')
        bundle, files = build(self.root)
        self.assertEqual(validate(bundle), files)
        self.assertEqual(build(self.root)[0], bundle)
        self.assertEqual(HEADER.size, 48)
        self.assertEqual(ENTRY.size, 136)

    def test_corruption_truncation_and_append(self):
        bundle, _ = build(self.root)
        for broken in [bundle[:-1], bundle + b'x', bundle[:60] + bytes([bundle[60] ^ 1]) + bundle[61:]]:
            with self.assertRaises(ValueError): validate(broken)

    def test_structural_attack_with_valid_outer_digest(self):
        bundle, _ = build(self.root)
        for name, offset in [(b'/../index.html', ENTRY.size), (b'/api/web', ENTRY.size), (b'/index.html', 0)]:
            body = bytearray(bundle[HEADER.size:])
            old = ENTRY.unpack_from(body)
            ENTRY.pack_into(body, 0, name, offset, old[2], old[3])
            broken = HEADER.pack(b'BSAWEB1\0', 1, len(body), hashlib.sha256(body).digest()) + body
            with self.assertRaises(ValueError): validate(broken)

    def test_disallow_secrets_and_missing_entry(self):
        (self.root / '.env').write_text('PASSWORD=secret')
        with self.assertRaises(ValueError): build(self.root)
        (self.root / '.env').unlink()
        (self.root / 'index.html').unlink()
        with self.assertRaises(ValueError): build(self.root)

    def test_oversize_and_file_count(self):
        (self.root / 'big.png').write_bytes(b'x' * (1024 * 1024))
        with self.assertRaises(ValueError): build(self.root)
        (self.root / 'big.png').unlink()
        for i in range(32): (self.root / f'{i}.js').write_text('')
        with self.assertRaises(ValueError): build(self.root)

    def test_missing_and_external_html_dependencies(self):
        for src in ['/missing.js', 'https://cdn.example/app.js']:
            (self.root / 'index.html').write_text(f'<script src="{src}"></script>', encoding='utf-8')
            with self.assertRaises(ValueError): build(self.root)


class MockDevice(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def reply(self, obj, code=200):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        s = self.server.state
        if self.path == '/api/challenge':
            s['nonce_number'] += 1
            s['nonce'] = f"{s['nonce_number']:064x}"
            self.reply({'nonce': s['nonce']}); return
        if self.path == '/api/status':
            self.reply({key: s[key] for key in ['protocol', 'storage_ready', 'version', 'previous', 'pending']}); return
        data = s['files'].get(self.path)
        if data is None: self.reply(b'missing', 404); return
        if s.get('corrupt') and s['pending']: data += b'corruption'
        self.reply(data)

    def do_POST(self):
        s = self.server.state
        data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        nonce = self.headers.get('X-Nonce', '')
        digest = self.headers.get('X-SHA256', '')
        message = f'{nonce}\nPOST\n{self.path}\n{digest}'.encode()
        expected = hmac.new(s['key'].encode(), message, hashlib.sha256).hexdigest()
        if not nonce or nonce != s['nonce'] or not hmac.compare_digest(expected, self.headers.get('X-Signature', '')) or digest != sha256(data):
            self.reply(b'auth failed', 401); return
        s['nonce'] = ''
        if self.path == '/api/web':
            if s['pending']: self.reply(b'pending', 409); return
            try:
                size = struct.unpack_from('<I', data)[0]
                envelope = json.loads(data[4:4+size])
                payload = release.verify(envelope)
                image = data[4+size:]
                self.assert_image = payload['ui']['sha256'] == sha256(image)
                if not self.assert_image: raise ValueError('artifact digest')
                files = validate(image)
            except ValueError: self.reply(b'invalid image', 400); return
            s['uploads'] += 1
            s['old_files'] = s['files']; s['previous'] = s['version']
            s['files'] = files; s['version'] = sha256(image); s['pending'] = True
        elif self.path == '/api/web/confirm':
            s['pending'] = False
        elif self.path == '/api/web/rollback':
            s['version'], s['previous'] = s['previous'], s['version']
            s['files'], s['old_files'] = s['old_files'], s['files']
            s['pending'] = False; s['rollbacks'] += 1
        else:
            self.reply(b'missing', 404); return
        self.reply({'ok': True})


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.web = self.root / 'web'; self.web.mkdir()
        (self.web / 'index.html').write_text('<!doctype html><title>new</title>', encoding='utf-8')
        self.bundle, self.files = build(self.web)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), MockDevice)
        self.server.state = dict(protocol=1, storage_ready=True, version='old', previous='', pending=False,
                                 key='a' * 64, nonce='', nonce_number=0, files={'/index.html': b'old'}, uploads=0, rollbacks=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.device = deploy_web.Device(f'http://127.0.0.1:{self.server.server_port}', 'a' * 64)
        self.root_patch = patch.object(deploy_web, 'ROOT', self.root); self.root_patch.start()
        self.release_patch = patch.object(release, 'ROOT', self.root); self.release_patch.start()
        release.generate_keys()

    def tearDown(self):
        self.root_patch.stop(); self.server.shutdown(); self.server.server_close(); self.thread.join(); self.release_patch.stop(); self.temp.cleanup()

    def deploy(self):
        with contextlib.redirect_stdout(io.StringIO()):
            deploy_web.deploy(self.device, self.bundle, self.files)

    def test_success_and_idempotent_repeat(self):
        self.deploy(); self.deploy()
        self.assertEqual(self.server.state['uploads'], 1)
        self.assertFalse(self.server.state['pending'])
        self.assertEqual(self.server.state['version'], sha256(self.bundle))
        self.assertTrue((self.root / '.deploy' / sha256(self.bundle) / 'receipt.json').exists())

    def test_corrupt_resources_trigger_verified_rollback(self):
        self.server.state['corrupt'] = True
        with self.assertRaisesRegex(RuntimeError, 'verification failed'): self.deploy()
        self.assertEqual(self.server.state['version'], 'old')
        self.assertFalse(self.server.state['pending'])
        self.assertEqual(self.server.state['rollbacks'], 1)

    def test_initial_upload_failure_returns_to_recovery(self):
        self.server.state.update(version='', files={}, corrupt=True)
        with self.assertRaises(RuntimeError): self.deploy()
        self.assertEqual(self.server.state['version'], '')
        self.assertFalse(self.server.state['pending'])

    def test_pending_other_version_blocks_upload(self):
        self.server.state['pending'] = True
        with self.assertRaisesRegex(RuntimeError, 'Another release'): self.deploy()
        self.assertEqual(self.server.state['uploads'], 0)

    def test_wrong_key_has_no_flash_side_effect(self):
        self.device.key = 'b' * 64
        with self.assertRaises(urllib.error.HTTPError): self.deploy()
        self.assertEqual(self.server.state['uploads'], 0)
        self.assertEqual(self.server.state['version'], 'old')

    def test_resume_lost_upload_reply_without_second_write(self):
        original = self.device.post
        def drop_reply(path, data=b''):
            result = original(path, data)
            if path == '/api/web': raise OSError('connection lost after upload')
            return result
        with patch.object(self.device, 'post', drop_reply): self.deploy()
        self.assertEqual(self.server.state['uploads'], 1)
        self.assertFalse(self.server.state['pending'])

    def test_stale_nonce_replay_rejected(self):
        nonce = json.loads(self.device.request('/api/challenge'))['nonce']
        path = '/api/web/confirm'; digest = sha256(b'')
        sig = hmac.new(b'a' * 64, f'{nonce}\nPOST\n{path}\n{digest}'.encode(), hashlib.sha256).hexdigest()
        headers = {'X-Nonce': nonce, 'X-SHA256': digest, 'X-Signature': sig}
        self.device.request(path, b'', headers)
        with self.assertRaises(urllib.error.HTTPError): self.device.request(path, b'', headers)


if __name__ == '__main__': unittest.main()
