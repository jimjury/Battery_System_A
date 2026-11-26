"""Controlled real C backup/restore and failing-upgrade rollback verification.

Only run on the explicitly authorized test server while no user tasks are active.
The rollback candidate deliberately raises at startup and is never published.
"""
import hashlib
import json
from pathlib import Path
import shlex
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from deploy_center import connect
from package_center import package
from sdk import ABClient


def main():
    config=json.loads((ROOT/'.local/cloud-login.json').read_text());client=connect(config,ROOT/'.local/known_hosts')
    api=ABClient('https://'+config['host'],ca_file=str(ROOT/'.local/pki/ca.pem'))
    staging='/root/ab-center-recovery-test';private=ROOT/'.local/cloud-backups';private.mkdir(exist_ok=True)
    report={}
    def command(text,expected=0):
        _,stdout,stderr=client.exec_command(text,timeout=90);out=stdout.read().decode();err=stderr.read().decode();status=stdout.channel.recv_exit_status()
        if status!=expected:raise RuntimeError('Recovery command returned '+str(status)+': '+err[-500:])
        return out
    try:
        previous=command('readlink -f /opt/ab-center/current').strip();command('umask 077; mkdir -p '+staging)
        with client.open_sftp() as sftp:
            sftp.put(str(ROOT/'deploy/maintain_center.py'),staging+'/maintain_center.py');sftp.chmod(staging+'/maintain_center.py',0o600)
        archive=staging+'/private-center-backup.zip'
        report['backup']=json.loads(command('python3 '+staging+'/maintain_center.py backup --file '+archive))
        with client.open_sftp() as sftp:sftp.get(archive,str(private/'private-center-backup.zip'))
        report['restore']=json.loads(command('python3 '+staging+'/maintain_center.py restore --file '+archive).strip().splitlines()[-1])
        for _ in range(30):
            try:
                if api.request('GET','/health')['status']=='ok':break
            except OSError:time.sleep(.5)
        else:raise RuntimeError('C did not recover after private restore')
        # Build an explicitly failing, private candidate with a valid hash manifest.
        version='3.1.0-test-startup-failure';bundle=package(version);path=Path(bundle['path'])
        with zipfile.ZipFile(path) as archive:
            files={name:archive.read(name) for name in archive.namelist() if name!='MANIFEST.json'}
        files['control_center/main.py']=b'raise RuntimeError("explicit private rollback test candidate")\n'
        manifest={'version':version,'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,data in files.items():archive.writestr(name,data)
            archive.writestr('MANIFEST.json',json.dumps(manifest))
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with client.open_sftp() as sftp:sftp.put(str(path),staging+'/failing-candidate.zip')
        source='/root/ab-center-deploy-3.1.0-pc-ui-20261126'
        args=['python3',source+'/install_center.py','--bundle',staging+'/failing-candidate.zip','--sha256',digest,
            '--cert',source+'/server.pem','--key',source+'/server.key','--ca',source+'/ca.pem','--origin','https://'+config['host']]
        output=command(shlex.join(args),expected=1)
        for _ in range(30):
            try:
                if api.request('GET','/health')['status']=='ok':break
            except OSError:time.sleep(.5)
        else:raise RuntimeError('C did not recover after rejected upgrade')
        current=command('readlink -f /opt/ab-center/current').strip()
        if current!=previous:raise AssertionError('Failed candidate remained active')
        report.update(failed_startup_candidate_rejected=True,previous_release_restored=True,active_release=current,
            public_https_healthy=True,backup_download_sha256=hashlib.sha256((private/'private-center-backup.zip').read_bytes()).hexdigest())
        (ROOT/'.local/cloud-recovery-report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
    finally:client.close()


if __name__=='__main__':main()
