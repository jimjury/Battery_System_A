#!/usr/bin/env python3
"""Install/upgrade an isolated C systemd service using existing Python 3.10+.

No pip, Docker pull or package-manager changes. Call as root on the authorized
Linux server with a verified bundle and a valid certificate/key pair.
"""
import argparse
import compileall
import hashlib
import http.client
import json
import os
from pathlib import Path,PurePosixPath
try:
    import pwd
except ImportError:
    pwd=None  # Bundle validation is also used on Windows before deployment.
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit
import zipfile

BASE=Path('/opt/ab-center');DATA=Path('/var/lib/ab-center');CONFIG=Path('/etc/ab-center')
UNIT=Path('/etc/systemd/system/ab-center.service')
MARKER='Managed by AB Platform center installer'


def run(*args,check=True):return subprocess.run(args,check=check,capture_output=True,text=True)


def verify_bundle(bundle,expected):
    if hashlib.sha256(bundle.read_bytes()).hexdigest()!=expected:raise ValueError('Bundle SHA-256 mismatch')
    with zipfile.ZipFile(bundle) as archive:
        manifest=json.loads(archive.read('MANIFEST.json'));version=manifest['version']
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',version):raise ValueError('Invalid release identifier')
        names=archive.namelist()
        if len(names)!=len(set(names)) or set(names)!=set(manifest['files'])|{'MANIFEST.json'}:raise ValueError('Unexpected bundle files')
        if len(names)>1000 or sum(info.file_size for info in archive.infolist())>10*1024*1024:raise ValueError('Bundle limit exceeded')
        result={}
        for name,digest in manifest['files'].items():
            path=PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name:raise ValueError('Unsafe archive path')
            if ((archive.getinfo(name).external_attr>>16)&0o170000)==0o120000:raise ValueError('Archive symlink forbidden')
            data=archive.read(name)
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Source hash mismatch')
            result[name]=data
    return version,result


def install(args):
    if sys.version_info<(3,10) or not hasattr(os,'geteuid') or os.geteuid()!=0:raise RuntimeError('Existing Python >=3.10 and Linux root are required')
    version,files=verify_bundle(args.bundle,args.sha256)
    if UNIT.exists() and MARKER not in UNIT.read_text():raise RuntimeError('Refusing to overwrite an unmanaged service')
    if not UNIT.exists():
        with socket.socket() as listener:
            try:listener.bind(('0.0.0.0',args.port))
            except OSError as error:raise RuntimeError('Requested HTTPS port is already occupied; existing services were not changed') from error
    if not re.fullmatch(r'https://[A-Za-z0-9.:-]+',args.origin):raise ValueError('Explicit HTTPS origin required')
    probe=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);probe.load_cert_chain(args.cert,args.key)
    try:account=pwd.getpwnam('ab-center')
    except KeyError:
        run('useradd','--system','--home-dir',str(DATA),'--shell','/usr/sbin/nologin','ab-center');account=pwd.getpwnam('ab-center')
    for folder in [BASE,BASE/'releases',BASE/'backups',DATA,CONFIG]:folder.mkdir(parents=True,exist_ok=True)
    release=BASE/'releases'/version
    if release.exists():
        for name,data in files.items():
            if not (release/name).is_file() or (release/name).read_bytes()!=data:raise RuntimeError('Immutable release version conflicts')
    else:
        release.mkdir()
        for name,data in files.items():
            file=release/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(data)
    if not compileall.compile_dir(str(release),quiet=2):raise RuntimeError('Release syntax validation failed')
    current=BASE/'current';previous=current.resolve() if current.is_symlink() else None
    if current.exists() and not current.is_symlink():raise RuntimeError('Refusing to replace unmanaged current directory')
    backup=BASE/'backups'/time.strftime('%Y%m%d-%H%M%S');backup.mkdir(mode=0o700)
    was_running=run('systemctl','is-active','ab-center',check=False).returncode==0
    if UNIT.exists():run('systemctl','stop','ab-center')
    try:
        # Service is stopped, so DB, private files and credentials are a consistent set.
        shutil.copytree(DATA,backup/'data')
        if UNIT.exists():shutil.copy2(UNIT,backup/'ab-center.service')
        for name in ['fullchain.pem','privkey.pem']:
            if (CONFIG/name).exists():shutil.copy2(CONFIG/name,backup/name)
        for source,name in [(args.cert,'fullchain.pem'),(args.key,'privkey.pem')]:
            shutil.copy2(source,CONFIG/name);os.chown(CONFIG/name,0,account.pw_gid);os.chmod(CONFIG/name,0o640)
        os.chown(DATA,account.pw_uid,account.pw_gid);os.chmod(DATA,0o700)
        temporary=BASE/'next'
        if temporary.is_symlink():temporary.unlink()
        temporary.symlink_to(release);temporary.replace(current)
        UNIT.write_text(f'''# {MARKER}
[Unit]
Description=AB independent communication center C
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=ab-center
Group=ab-center
WorkingDirectory={current}
ExecStart={sys.executable} -m control_center.main --root {DATA} --host 0.0.0.0 --port {args.port} --cert {CONFIG}/fullchain.pem --key {CONFIG}/privkey.pem --origin {args.origin}
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={DATA}
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
[Install]
WantedBy=multi-user.target
''')
        run('systemctl','daemon-reload');run('systemctl','enable','ab-center');run('systemctl','restart','ab-center')
        context=ssl.create_default_context(cafile=str(args.ca));origin=urlsplit(args.origin)
        # On-server loopback connection verifies the configured public hostname/IP certificate.
        deadline=time.monotonic()+30;last=''
        while time.monotonic()<deadline:
            try:
                with socket.create_connection(('127.0.0.1',args.port),timeout=3) as raw_socket:
                    with context.wrap_socket(raw_socket,server_hostname=origin.hostname) as secure:
                        secure.sendall(('GET /health HTTP/1.1\r\nHost: '+origin.netloc+'\r\nConnection: close\r\n\r\n').encode('ascii'))
                        response=http.client.HTTPResponse(secure);response.begin();health=json.loads(response.read(65536))
                if health.get('service')=='center' and health.get('status')=='ok':break
            except Exception as error:last=type(error).__name__
            time.sleep(.5)
        else:raise RuntimeError('Installed HTTPS health check failed: '+last)
        receipt={'version':version,'bundle_sha256':args.sha256,'origin':args.origin,'backup':str(backup),'health':health,'previous':str(previous) if previous else None}
        (DATA/'deployment.json').write_text(json.dumps(receipt,indent=2));os.chown(DATA/'deployment.json',account.pw_uid,account.pw_gid)
        print(json.dumps(receipt))
    except BaseException:
        run('systemctl','stop','ab-center',check=False)
        # Restore only the managed data root from the newly created private backup.
        for child in DATA.iterdir():
            if child.is_dir() and not child.is_symlink():shutil.rmtree(child)
            else:child.unlink()
        shutil.copytree(backup/'data',DATA,dirs_exist_ok=True)
        for file in DATA.rglob('*'):os.chown(file,account.pw_uid,account.pw_gid)
        if previous:
            temporary=BASE/'rollback'
            if temporary.is_symlink():temporary.unlink()
            temporary.symlink_to(previous);temporary.replace(current)
        elif current.is_symlink():current.unlink()
        if (backup/'ab-center.service').exists():shutil.copy2(backup/'ab-center.service',UNIT)
        elif UNIT.exists() and MARKER in UNIT.read_text():
            run('systemctl','disable','ab-center',check=False);UNIT.unlink()
        for name in ['fullchain.pem','privkey.pem']:
            if (backup/name).exists():shutil.copy2(backup/name,CONFIG/name)
        run('systemctl','daemon-reload',check=False)
        if was_running:run('systemctl','start','ab-center',check=False)
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--sha256',required=True)
    parser.add_argument('--cert',type=Path,required=True);parser.add_argument('--key',type=Path,required=True);parser.add_argument('--ca',type=Path,required=True)
    parser.add_argument('--origin',required=True);parser.add_argument('--port',type=int,default=443)
    install(parser.parse_args())
