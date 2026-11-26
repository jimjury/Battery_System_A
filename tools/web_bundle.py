"""Deterministic, uncompressed, bounded bundle; no third-party packages."""
import hashlib
from html.parser import HTMLParser
import json
import re
import shutil
import struct
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

HEADER = struct.Struct('<8sII32s')
ENTRY = struct.Struct('<96sII32s')
LIMIT = 700 * 1024  # Reserve space for SPIFFS metadata and copy-on-write overhead.
SUFFIXES = {'.html', '.css', '.js', '.json', '.svg', '.png', '.ico', '.woff2'}


class ResourceParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.resources = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'img', 'source'} and attrs.get('src'):
            self.resources.append(attrs['src'])
        if tag == 'link' and attrs.get('rel') in {'stylesheet', 'icon', 'preload'} and attrs.get('href'):
            self.resources.append(attrs['href'])


def check_resources(files):
    for name, data in files.items():
        if not name.endswith('.html'):
            continue
        parser = ResourceParser(); parser.feed(data.decode('utf-8')); parser.close()
        for resource in parser.resources:
            parsed = urlsplit(resource)
            if parsed.scheme or parsed.netloc:
                raise ValueError(f'External web dependency is forbidden: {resource}')
            resolved = parsed.path if parsed.path.startswith('/') else name.rsplit('/', 1)[0] + '/' + parsed.path
            if resolved not in files:
                raise ValueError(f'Missing HTML dependency in bundle: {resource}')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def valid_path(name):
    return (1 < len(name.encode('ascii')) < 96 and
            re.fullmatch(r'/[A-Za-z0-9/._-]+', name) is not None and
            '..' not in name and '//' not in name and not name.startswith('/api/'))


def build(directory):
    directory = Path(directory).resolve()
    files = {}
    for file in sorted(directory.rglob('*')):
        if file.is_symlink():
            raise ValueError(f'Symlinks are forbidden: {file}')
        if not file.is_file():
            continue
        name = '/' + file.relative_to(directory).as_posix()
        if not valid_path(name) or file.suffix.lower() not in SUFFIXES:
            raise ValueError(f'Unsupported web resource: {name}')
        data = file.read_bytes()
        if file.suffix in {'.html', '.css', '.js', '.json', '.svg'}:
            data.decode('utf-8')
        if file.suffix == '.json':
            json.loads(data)
        files[name] = data
    if '/index.html' not in files or not 1 <= len(files) <= 32:
        raise ValueError('Require index.html and between 1 and 32 files')
    check_resources(files)
    if '/asset-set.json' in files or len(files) >= 32:
        raise ValueError('Reserve asset-set.json and one directory entry for version isolation')
    asset_set = sha256(b''.join(name.encode('ascii') + hashlib.sha256(data).digest() for name, data in files.items()))
    def version_reference(match):
        attribute, quote, reference = match.group(1), match.group(2), match.group(3)
        parsed = urlsplit(reference)
        if parsed.path in files and not parsed.scheme and not parsed.netloc:
            return attribute + '=' + quote + parsed.path + '?v=' + asset_set + quote
        return match.group(0)
    for name in list(files):
        if name.endswith('.html'):
            files[name] = re.sub(r'''(src|href)=(["'])([^"']*)\2''', version_reference, files[name].decode('utf-8')).encode('utf-8')
    files['/asset-set.json'] = json.dumps({'asset_set': asset_set}, separators=(',', ':')).encode('ascii')
    files = dict(sorted(files.items()))
    offset = ENTRY.size * len(files)
    table = []
    for name, data in files.items():
        table.append(ENTRY.pack(name.encode('ascii'), offset, len(data), hashlib.sha256(data).digest()))
        offset += len(data)
    body = b''.join(table) + b''.join(files.values())
    bundle = HEADER.pack(b'BSAWEB1\0', len(files), len(body), hashlib.sha256(body).digest()) + body
    if len(bundle) > LIMIT:
        raise ValueError(f'Bundle exceeds {LIMIT} bytes')
    validate(bundle)
    return bundle, files


def check_javascript(directory):
    """Use an existing Node if present; never install one."""
    node = shutil.which('node')
    if node is None:
        return False
    for file in sorted(Path(directory).rglob('*.js')):
        result = subprocess.run([node, '--check', str(file)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f'JavaScript syntax check failed: {file.name}\n{result.stderr}')
    return True


def validate(bundle):
    if len(bundle) < HEADER.size or len(bundle) > LIMIT:
        raise ValueError('Invalid bundle size')
    magic, count, size, digest = HEADER.unpack_from(bundle)
    body = bundle[HEADER.size:]
    if magic != b'BSAWEB1\0' or not 1 <= count <= 32 or size != len(body) or hashlib.sha256(body).digest() != digest:
        raise ValueError('Invalid bundle header or digest')
    end = count * ENTRY.size
    if end > size:
        raise ValueError('Truncated bundle table')
    files = {}
    for i in range(count):
        raw_name, offset, length, file_hash = ENTRY.unpack_from(body, i * ENTRY.size)
        if b'\0' not in raw_name:
            raise ValueError('Unterminated file path')
        name = raw_name.split(b'\0', 1)[0].decode('ascii')
        if not valid_path(name) or name in files or offset != end or length > size - end:
            raise ValueError('Invalid file table')
        data = body[offset:offset + length]
        if hashlib.sha256(data).digest() != file_hash:
            raise ValueError('File digest mismatch')
        files[name] = data
        end += length
    if end != size or '/index.html' not in files:
        raise ValueError('Incomplete bundle')
    return files
