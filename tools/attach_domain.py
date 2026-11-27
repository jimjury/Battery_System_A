"""Attach a named, validated TLS certificate through pinned SSH.

The configured source directory supplies only the leaf key/chain and public CA.
The CA signing key is never sent to the server.
"""
import argparse
import json
from pathlib import Path
import shlex
from deploy_center import connect,ROOT


def attach(args):
    config=json.loads(args.credentials.read_text());client=connect(config,args.known_hosts)
    try:
        staging='/root/ab-center-domain-stage'
        _,out,err=client.exec_command('umask 077; mkdir -p '+staging)
        if out.channel.recv_exit_status():raise RuntimeError('Cannot prepare private domain staging')
        with client.open_sftp() as sftp:
            for local,name in [(ROOT/'deploy/enable_domain.py','enable_domain.py'),(args.tls_directory/'server.pem','server.pem'),
                               (args.tls_directory/'server.key','server.key'),(args.tls_directory/'ca.pem','ca.pem')]:
                sftp.put(str(local),staging+'/'+name);sftp.chmod(staging+'/'+name,0o600)
        command=['python3',staging+'/enable_domain.py','--hostname',args.hostname,'--cert',staging+'/server.pem','--key',staging+'/server.key','--ca',staging+'/ca.pem']
        _,out,err=client.exec_command(shlex.join(command),timeout=60)
        output=out.read().decode();errors=err.read().decode();status=out.channel.recv_exit_status()
        (ROOT/'.local/domain-attach.log').write_text(output+errors)
        if status:raise RuntimeError('Named certificate installation failed; see private log')
        receipt=json.loads(output.strip().splitlines()[-1]);(ROOT/'.local/domain-attach-receipt.json').write_text(json.dumps(receipt,indent=2))
        print(json.dumps(receipt,indent=2))
    finally:client.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--hostname',required=True)
    parser.add_argument('--credentials',type=Path,required=True);parser.add_argument('--known-hosts',type=Path,required=True)
    parser.add_argument('--tls-directory',type=Path,required=True);attach(parser.parse_args())
