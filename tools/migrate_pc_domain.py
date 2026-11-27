"""Move existing local A/B identities to an operator-approved TLS alias of C.

Requires idle/stopped local services, a pinned certificate fingerprint, verified
TLS and identical pairing records through both origins before touching state.
No enrollment or cloud administrator credential is used.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import socket
import ssl
import sys
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from sdk import ABClient


def migrate(url,ca_file,expected_certificate):
    candidate=ABClient(url,ca_file=str(ca_file))
    for port in [8766,8765]:
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1',port))==0:raise RuntimeError('Stop local PC A/B before changing their endpoint')
    origin=urlsplit(url);context=ssl.create_default_context();context.load_verify_locations(cafile=str(ca_file))
    with socket.create_connection((origin.hostname,origin.port or 443),timeout=10) as raw:
        with context.wrap_socket(raw,server_hostname=origin.hostname) as secure:digest=hashlib.sha256(secure.getpeercert(binary_form=True)).hexdigest()
    if digest!=expected_certificate:raise RuntimeError('Domain certificate differs from the operator-approved server certificate')
    if candidate.request('GET','/health').get('service')!='center':raise RuntimeError('Domain is not the expected C API')
    files=[ROOT/'desktop_a/.runtime/identity.json',ROOT/'server_b/.runtime/center.json'];values=[]
    for path in files:
        config=json.loads(path.read_text());token=config['credential']
        old=ABClient(config['url'],token,ca_file=config.get('ca_file'))
        new=ABClient(url,token,ca_file=str(ca_file))
        left=old.request('GET','/api/v1/pairings')['pairings'];right=new.request('GET','/api/v1/pairings')['pairings']
        if left!=right:raise RuntimeError('Old and new C pairing state disagree; migration stopped')
        values.append(config)
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S');backup=ROOT/'.local/domain-client-backups'/stamp;backup.mkdir(parents=True)
    old_profile=ROOT/'client_config/center.json';shutil.copy2(old_profile,backup/'public-profile.json')
    for index,(path,config) in enumerate(zip(files,values)):
        shutil.copy2(path,backup/('A.json' if index==0 else 'B.json'))
        config.update(url=url,ca_file=str(ca_file.resolve()))
        temporary=path.with_suffix('.migrate.tmp');temporary.write_text(json.dumps(config,indent=2));temporary.replace(path)
    profile={'url':url,'ca_file':'cloud-ca.pem','legacy_url':values[0].get('url'),'legacy_ca_file':'cloud-ca.pem'}
    # Store the actual previous origin, before mutation, in the public profile.
    profile['legacy_url']=json.loads((backup/'A.json').read_text())['url']
    old_profile.write_text(json.dumps(profile,indent=2))
    report={'url':url,'certificate_pin_verified':True,'pairing_state_equal':True,'a_node_id':values[0]['node_id'],
        'node_credentials_preserved':all(json.loads(path.read_text())['credential']==old['credential'] for path,old in zip(files,values)),
        'new_enrollment_performed':False,'backup':str(backup),'public_certificate':False}
    (ROOT/'.local/domain-client-migration.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--url',required=True);parser.add_argument('--ca',type=Path,required=True)
    parser.add_argument('--certificate-sha256',required=True);args=parser.parse_args();migrate(args.url,args.ca,args.certificate_sha256)
