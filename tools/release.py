"""Signed release protocol shared by publishing, USB deployment, and host tests.

RSA-2048/PKCS#1 v1.5/SHA-256 over the exact decoded payload bytes.
No developer token or device credential is included in release artifacts.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TARGET = 'esp32s3-n16r8'
API = 1
ENVELOPE_LIMIT = 8192
HOSTS = {'github.com', 'raw.githubusercontent.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda s: (_ for _ in ()).throw(ValueError('Non-finite JSON')))


def trusted_url(url):
    p = urlsplit(url)
    origin = os.environ.get('BATTERY_UPDATE_ORIGIN', '').rstrip('/')
    mirror = bool(origin and url.startswith(origin + '/') and urlsplit(origin).scheme == 'https' and
                  urlsplit(origin).path == '' and not urlsplit(origin).username)
    return (p.scheme == 'https' and (p.hostname in HOSTS or mirror) and not p.username and not p.password and
            p.port in (None, 443) and not p.fragment and
            (p.hostname != 'github.com' or p.path.startswith('/jimjury/Battery_System_A/releases/download/')))


def validate_payload(payload):
    if payload.get('schema_version') != 2 or payload.get('target') != TARGET:
        raise ValueError('Wrong schema or hardware target')
    for kind, maximum in [('ui', 700 * 1024), ('firmware', 4 * 1024 * 1024)]:
        item = payload.get(kind)
        if item is None: continue
        if not isinstance(item, dict): raise ValueError('Invalid artifact')
        if type(item.get('sequence')) is not int or not 1 <= item['sequence'] <= 2147483647:
            raise ValueError('Invalid monotonic release sequence')
        if type(item.get('size')) is not int or not 0 < item['size'] <= maximum:
            raise ValueError('Invalid artifact size')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,31}', item.get('version', '')):
            raise ValueError('Invalid version')
        delivery = item.get('delivery', 'https')
        url_valid = trusted_url(item.get('url', '')) if delivery == 'https' else delivery == 'usb' and item.get('url') == ''
        if not re.fullmatch(r'[0-9a-f]{64}', item.get('sha256', '')) or not url_valid:
            raise ValueError('Invalid hash or untrusted artifact URL')
        if item.get('api') != API or item.get('min_firmware_api') != API:
            raise ValueError('Incompatible API')
        if item.get('format') != ('bsaweb1-spiffs' if kind == 'ui' else 'esp-idf-app'):
            raise ValueError('Unsupported artifact format')
    return payload


def generate_keys(private_path=None, public_path=None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    private_path = Path(private_path or ROOT / '.local/release_private.pem')
    public_path = Path(public_path or ROOT / 'updates/release_public.pem')
    if private_path.exists():
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path.parent.mkdir(parents=True, exist_ok=True)
        with private_path.open('xb') as file:
            file.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public_path.exists() and public_path.read_bytes() != public:
        raise ValueError('Refusing implicit release-key rotation')
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(public)
    header = public_path.with_suffix('.h')
    header.write_text('#pragma once\nstatic const char release_public_pem[] =\n' +
                      '\n'.join(json.dumps(line + '\n') for line in public.decode('ascii').splitlines()) + ';\n',
                      encoding='ascii', newline='\n')
    return private_path, public_path


def sign(payload, private_path=None):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    validate_payload(payload)
    key = serialization.load_pem_private_key(Path(private_path or ROOT / '.local/release_private.pem').read_bytes(), password=None)
    raw = canonical(payload)
    signature = key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    return {'schema_version': 2, 'key_id': 'release-v1', 'payload': base64.b64encode(raw).decode('ascii'),
            'signature': base64.b64encode(signature).decode('ascii')}


def verify(envelope, public_path=None):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.exceptions import InvalidSignature
    if isinstance(envelope, (bytes, str)): envelope = strict_json(envelope)
    if envelope.get('schema_version') != 2 or envelope.get('key_id') != 'release-v1':
        raise ValueError('Unknown signing scheme or key')
    try:
        raw = base64.b64decode(envelope['payload'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
        if len(raw) > 4096 or len(signature) != 256: raise ValueError('Signature size')
        key = serialization.load_pem_public_key(Path(public_path or ROOT / 'updates/release_public.pem').read_bytes())
        key.verify(signature, raw, padding.PKCS1v15(), hashes.SHA256())
        payload = strict_json(raw)
        if canonical(payload) != raw: raise ValueError('Noncanonical payload')
        return validate_payload(payload)
    except (KeyError, TypeError, InvalidSignature) as exc:
        raise ValueError('Release signature verification failed') from exc


def frame(envelope, artifact, kind):
    payload = verify(envelope)
    item = payload.get(kind)
    if not item or item['size'] != len(artifact) or item['sha256'] != hashlib.sha256(artifact).hexdigest():
        raise ValueError('Artifact does not match signed metadata')
    raw = canonical(envelope)
    if len(raw) > ENVELOPE_LIMIT: raise ValueError('Oversize signed metadata')
    return struct.pack('<I', len(raw)) + raw + artifact


def allocate_sequence(kind, floor=0):
    path = ROOT / '.local/release_sequences.json'
    state = strict_json(path.read_bytes()) if path.exists() else {}
    value = max(int(time.time()), int(state.get(kind, 0)) + 1, int(floor) + 1)
    if value > 2147483647: raise ValueError('Release sequence range exhausted')
    state[kind] = value; path.parent.mkdir(parents=True, exist_ok=True)
    # Local publishing/watch commands are serialized by the deployment lock.
    path.write_bytes(canonical(state))
    return value


def usb_envelope(kind, artifact, floor=0, version=None):
    digest = hashlib.sha256(artifact).hexdigest()
    payload = {'schema_version': 2, 'target': TARGET, 'ui': None, 'firmware': None}
    payload[kind] = {'version': version or 'usb-' + digest[:12], 'sequence': allocate_sequence(kind, floor),
        'size': len(artifact), 'sha256': digest, 'url': '', 'delivery': 'usb', 'api': API, 'min_firmware_api': API,
        'format': 'bsaweb1-spiffs' if kind == 'ui' else 'esp-idf-app'}
    return sign(payload)


def package(kind, version, sequence, url):
    if kind == 'ui':
        from web_bundle import build, check_javascript
        check_javascript(ROOT / 'web')
        artifact, _ = build(ROOT / 'web')
    else:
        build_dir = ROOT / ('build' if (ROOT / 'partitions.csv').exists() else 'build-review')
        description = strict_json((build_dir / 'project_description.json').read_bytes())
        artifact = (build_dir / description['app_bin']).read_bytes()
        for name in ['update_key.txt', 'admin_password.txt']:
            secret = ROOT / '.local' / name
            if secret.exists() and secret.read_bytes().strip() in artifact:
                raise ValueError('Device credential found in firmware; public release forbidden')
    payload = {'schema_version': 2, 'target': TARGET, 'ui': None, 'firmware': None}
    payload[kind] = {'version': version, 'sequence': sequence, 'size': len(artifact),
                     'sha256': hashlib.sha256(artifact).hexdigest(), 'url': url, 'api': API, 'min_firmware_api': API,
                     'format': 'bsaweb1-spiffs' if kind == 'ui' else 'esp-idf-app'}
    envelope = sign(payload)
    verify(envelope)
    directory = ROOT / '.deploy/releases' / f'{kind}-{sequence}-{version}'
    directory.mkdir(parents=True, exist_ok=False)
    (directory / f'{kind}.bin').write_bytes(artifact)
    (directory / 'manifest.json').write_bytes(canonical(envelope))
    (directory / 'upload.bin').write_bytes(frame(envelope, artifact, kind))
    (directory / 'validation.json').write_bytes(canonical({'software_checked': True, 'hardware_accepted': False,
        'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip()}))
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['keys', 'package', 'verify'])
    parser.add_argument('--kind', choices=['ui', 'firmware'], default='ui')
    parser.add_argument('--version')
    parser.add_argument('--sequence', type=int)
    parser.add_argument('--url')
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    if args.action == 'keys':
        generate_keys(); print('Publisher public key ready. Private key remains local and ignored.'); return
    if args.action == 'verify':
        print(json.dumps(verify(args.manifest.read_bytes()), indent=2)); return
    if not args.version or not args.sequence or not args.url: parser.error('package requires --version, --sequence, --url')
    print(package(args.kind, args.version, args.sequence, args.url))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr); sys.exit(1)
