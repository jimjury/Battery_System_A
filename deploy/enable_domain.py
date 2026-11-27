#!/usr/bin/env python3
"""Attach a named certificate to the managed C; retain the original IP TLS."""
import argparse
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import ssl
import subprocess
import time


def health(host,ca=None):
    context=ssl.create_default_context()
    if ca:context.load_verify_locations(cafile=str(ca))
    with socket.create_connection(('127.0.0.1',443),timeout=5) as raw:
        with context.wrap_socket(raw,server_hostname=host) as secure:
            secure.sendall(('GET /health HTTP/1.0\r\nHost: '+host+'\r\n\r\n').encode())
            response=http.client.HTTPResponse(secure);response.begin();value=json.loads(response.read(65536))
            if response.status!=200 or value.get('service')!='center':raise RuntimeError('Named C health check failed')
            return value


def main(args):
    if os.geteuid()!=0:raise RuntimeError('Run on authorized C as root')
    if not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?',args.hostname) or '..' in args.hostname:raise ValueError('Explicit hostname required')
    if 'Managed by AB Platform center installer' not in Path('/etc/systemd/system/ab-center.service').read_text():raise RuntimeError('Unmanaged service')
    probe=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);probe.load_cert_chain(args.cert,args.key)
    dropin=Path('/etc/systemd/system/ab-center.service.d/ab-domain.conf');config=Path('/etc/ab-center/tls_names.json')
    if dropin.exists() and '# Managed AB domain TLS' not in dropin.read_text():raise RuntimeError('Unmanaged domain service configuration')
    backup=Path('/opt/ab-center/backups')/('domain-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(mode=0o700)
    old_config=config.read_bytes() if config.exists() else None;old_dropin=dropin.read_bytes() if dropin.exists() else None
    if old_config:(backup/'tls_names.json').write_bytes(old_config)
    if old_dropin:(backup/'ab-domain.conf').write_bytes(old_dropin)
    account=pwd.getpwnam('ab-center');directory=Path('/etc/ab-center/domains')/args.hostname
    directory.mkdir(parents=True,exist_ok=True)
    for path in [directory.parent,directory]:os.chown(path,0,account.pw_gid);os.chmod(path,0o750)
    for source,name in [(args.cert,'fullchain.pem'),(args.key,'privkey.pem')]:
        target=directory/name
        if target.exists():shutil.copy2(target,backup/name)
        shutil.copy2(source,target);os.chown(target,0,account.pw_gid);os.chmod(target,0o640)
    entries=json.loads(old_config) if old_config else []
    entries=[entry for entry in entries if entry['hostname']!=args.hostname]
    entries.append({'hostname':args.hostname,'cert':str(directory/'fullchain.pem'),'key':str(directory/'privkey.pem')})
    config.write_text(json.dumps(entries,indent=2));os.chown(config,0,account.pw_gid);os.chmod(config,0o640)
    dropin.parent.mkdir(parents=True,exist_ok=True);dropin.write_text('# Managed AB domain TLS\n[Service]\nEnvironment=AB_TLS_NAMES=/etc/ab-center/tls_names.json\n')
    try:
        subprocess.run(['systemctl','daemon-reload'],check=True);subprocess.run(['systemctl','restart','ab-center'],check=True)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            try:value=health(args.hostname,args.ca);break
            except (OSError,ValueError):time.sleep(.5)
        else:raise RuntimeError('Named TLS failed after restart')
        print(json.dumps({'hostname':args.hostname,'domain_tls_valid':True,'health':value,'ip_tls_kept':True,'backup':str(backup)}))
    except BaseException:
        if old_config:config.write_bytes(old_config)
        elif config.exists():config.unlink()
        if old_dropin:dropin.write_bytes(old_dropin)
        elif dropin.exists():dropin.unlink()
        for name in ['fullchain.pem','privkey.pem']:
            if (backup/name).exists():shutil.copy2(backup/name,directory/name)
        subprocess.run(['systemctl','daemon-reload'],check=False);subprocess.run(['systemctl','restart','ab-center'],check=False)
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--hostname',required=True)
    for name in ['cert','key']:parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--ca',type=Path);main(parser.parse_args())
