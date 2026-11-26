"""Authenticated web deployment over USB NCM; Python standard library only."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from web_bundle import build, sha256, check_javascript
from release import usb_envelope, frame

ROOT = Path(__file__).resolve().parents[1]


class Device:
    def __init__(self, url='http://192.168.7.1', key=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
            raise ValueError('Expected a device origin URL')
        self.url = url.rstrip('/')
        self.key = key
        # Never send local device requests via a corporate or Internet proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, data=None, headers=None, timeout=45):
        req = urllib.request.Request(self.url + path, data=data, headers=headers or {})
        with self.opener.open(req, timeout=timeout) as response:
            return response.read()

    def status(self):
        result = json.loads(self.request('/api/status'))
        if result.get('protocol') != 1:
            raise RuntimeError('Device needs the approved initial firmware migration')
        return result

    def post(self, path, data=b''):
        if not self.key:
            raise RuntimeError('Run tools/firmware.py setup to create a local update key')
        nonce = json.loads(self.request('/api/challenge'))['nonce']
        digest = sha256(data)
        message = f'{nonce}\nPOST\n{path}\n{digest}'.encode('ascii')
        signature = hmac.new(self.key.encode('ascii'), message, hashlib.sha256).hexdigest()
        return json.loads(self.request(path, data, {
            'X-Nonce': nonce, 'X-SHA256': digest, 'X-Signature': signature,
            'Content-Type': 'application/octet-stream',
        }, timeout=180))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Device redirects are forbidden')


def load_key():
    key = os.environ.get('BATTERY_UPDATE_KEY')
    if key is None:
        local = ROOT / '.local' / 'update_key.txt'
        key = local.read_text(encoding='ascii').strip() if local.exists() else None
    if key is not None and (len(key) != 64 or any(c not in '0123456789abcdef' for c in key)):
        raise ValueError('Update key must contain 64 lowercase hexadecimal characters')
    return key


def verify_files(device, files):
    for name, expected in files.items():
        actual = device.request(name)
        if sha256(actual) != sha256(expected):
            raise RuntimeError(f'Device resource verification failed: {name}')


def archive(bundle, files):
    directory = ROOT / '.deploy' / sha256(bundle)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'web.bin').write_bytes(bundle)
    (directory / 'manifest.json').write_text(json.dumps({
        'version': sha256(bundle), 'bytes': len(bundle),
        'files': {name: sha256(data) for name, data in files.items()},
    }, indent=2), encoding='utf-8')
    return directory


def deploy(device, bundle, files):
    before = device.status()
    version = sha256(bundle)
    if not before.get('storage_ready'):
        raise RuntimeError('Web A/B partitions are absent; initial migration is required')
    directory = archive(bundle, files)
    if before.get('pending') and before.get('version') != version:
        raise RuntimeError('Another release is pending; verify it or roll back first')
    if before.get('version') != version:
        try:
            signed = usb_envelope('ui', bundle, before.get('sequence_floor', 0))
            device.post('/api/web', frame(signed, bundle, 'ui'))
        except (OSError, ValueError):
            # A lost response must never cause a blind second upload.
            current = device.status()
            if current.get('version') != version or not current.get('pending'):
                raise
    try:
        current = device.status()
        if current.get('version') != version:
            raise RuntimeError('Device did not select the expected release')
        verify_files(device, files)
        if current.get('pending'):
            device.post('/api/web/confirm')
        final = device.status()
        if final.get('pending') or final.get('version') != version:
            raise RuntimeError('Release confirmation did not persist')
    except Exception:
        # Roll back only the release this process just selected.
        try:
            current = device.status()
            if current.get('pending') and current.get('version') == version:
                device.post('/api/web/rollback')
                if device.status().get('version') != before.get('version'):
                    raise RuntimeError('Rollback verification failed')
                print('Verification failed; previous release restored.', file=sys.stderr)
        except Exception as rollback_error:
            print(f'Rollback needs attention: {rollback_error}', file=sys.stderr)
        raise
    receipt = {'version': version, 'time': time.time(), 'device': device.url, 'verified': True}
    (directory / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(f'Committed and verified {version[:12]} ({len(bundle)} bytes)', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'push', 'watch', 'status', 'rollback'], nargs='?', default='watch')
    parser.add_argument('--url', default='http://192.168.7.1')
    parser.add_argument('--source', type=Path, default=ROOT / 'web')
    parser.add_argument('--open', action='store_true', help='Open browser once after successful deployment')
    args = parser.parse_args()
    device = Device(args.url, load_key())
    if args.action == 'status':
        print(json.dumps(device.status(), indent=2)); return
    if args.action == 'rollback':
        before = device.status()
        if not before.get('previous'):
            raise RuntimeError('No previous release available')
        device.post('/api/web/rollback')
        after = device.status()
        if after.get('version') != before['previous'] or after.get('pending'):
            raise RuntimeError('Rollback verification failed')
        print(json.dumps(after, indent=2)); return
    if args.action == 'check':
        bundle, files = build(args.source)
        checked = check_javascript(args.source)
        archive(bundle, files)
        print(f'Validated {len(files)} files, {len(bundle)} bytes, SHA-256 {sha256(bundle)}')
        print('JavaScript syntax: checked with existing Node' if checked else 'JavaScript syntax: Node unavailable; browser validation still required')
        return
    if args.action == 'push':
        bundle, files = build(args.source); check_javascript(args.source); deploy(device, bundle, files)
        if args.open: webbrowser.open(device.url)
        return
    print('Watching web sources; Ctrl+C stops. Browser refreshes after commit.', flush=True)
    last = None; opened = False; seen = None; stable_since = 0; retry_at = 0; last_error = None
    while True:
        try:
            bundle, files = build(args.source)
            version = sha256(bundle)
            now = time.monotonic()
            if version != seen:
                seen = version; stable_since = now
            if version != last and now - stable_since >= 0.8 and now >= retry_at:
                check_javascript(args.source)
                deploy(device, bundle, files)
                last = version; last_error = None
                if args.open and not opened:
                    webbrowser.open(device.url); opened = True
        except (OSError, ValueError, RuntimeError) as exc:
            message = str(exc)
            if message != last_error:
                print(f'Deployment paused: {message}', file=sys.stderr, flush=True); last_error = message
            retry_at = time.monotonic() + 5
        time.sleep(0.5)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped.')
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr); sys.exit(1)
