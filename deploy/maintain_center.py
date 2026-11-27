#!/usr/bin/env python3
"""Consistent private backup/restore for an already managed Linux C service."""
import argparse
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import pwd
import shutil
import subprocess
import tempfile
import time
import zipfile

DATA=Path('/var/lib/ab-center');CONFIG=Path('/etc/ab-center');BACKUPS=Path('/opt/ab-center/backups')


def command(action):subprocess.run(['systemctl',action,'ab-center'],check=True)


def backup(path):
    path=path.resolve()
    if path.exists():raise ValueError('Refusing to overwrite a private backup')
    path.parent.mkdir(parents=True,exist_ok=True);command('stop')
    try:
        files={}
        for root,prefix in [(DATA,'data'),(CONFIG,'config')]:
            for file in root.rglob('*'):
                if file.is_symlink():raise ValueError('Private state symlinks require explicit migration')
                if file.is_file():files[prefix+'/'+file.relative_to(root).as_posix()]=file
        manifest={}
        with zipfile.ZipFile(path,'x',zipfile.ZIP_DEFLATED) as archive:
            for name,file in files.items():
                sha=hashlib.sha256()
                with file.open('rb') as source,archive.open(name,'w',force_zip64=True) as target:
                    for block in iter(lambda:source.read(65536),b''):sha.update(block);target.write(block)
                manifest[name]=sha.hexdigest()
            archive.writestr('MANIFEST.json',json.dumps(manifest,sort_keys=True))
        os.chmod(path,0o600)
        print(json.dumps({'backup':str(path),'files':len(files),'private_credentials_included':True,'bytes':path.stat().st_size}))
    finally:command('start')


def restore(path):
    with tempfile.TemporaryDirectory(prefix='ab-center-restore-') as temporary,zipfile.ZipFile(path) as archive:
        root=Path(temporary);manifest=json.loads(archive.read('MANIFEST.json'));names=archive.namelist()
        if len(names)!=len(set(names)) or set(names)!=set(manifest)|{'MANIFEST.json'}:raise ValueError('Backup manifest mismatch')
        for name,expected in manifest.items():
            part=PurePosixPath(name)
            if part.is_absolute() or '..' in part.parts or '\\' in name or part.parts[0] not in {'data','config'}:raise ValueError('Unsafe backup path')
            file=root/name;file.parent.mkdir(parents=True,exist_ok=True);digest=hashlib.sha256()
            with archive.open(name) as source,file.open('wb') as target:
                for block in iter(lambda:source.read(65536),b''):digest.update(block);target.write(block)
            if digest.hexdigest()!=expected:raise ValueError('Backup content failed SHA-256')
        if not (root/'data/center.db').is_file() or not (root/'data/admin_token.txt').is_file():raise ValueError('Incomplete private center state')
        # First preserve current state, then restore only the two owned roots.
        backup(BACKUPS/('before-restore-'+time.strftime('%Y%m%d-%H%M%S')+'.zip'));command('stop')
        try:
            account=pwd.getpwnam('ab-center')
            for source,destination in [(root/'data',DATA),(root/'config',CONFIG)]:
                if destination.is_symlink() or str(destination) not in {'/var/lib/ab-center','/etc/ab-center'}:raise ValueError('Managed root changed')
                for child in destination.iterdir():
                    if child.is_dir() and not child.is_symlink():shutil.rmtree(child)
                    else:child.unlink()
                shutil.copytree(source,destination,dirs_exist_ok=True)
                for file in destination.rglob('*'):
                    os.chown(file,account.pw_uid if destination==DATA else 0,account.pw_gid)
                    os.chmod(file,(0o700 if destination==DATA else 0o750) if file.is_dir() else 0o600 if destination==DATA else 0o640)
            print(json.dumps({'restored':str(path),'integrity_verified':True}))
        finally:command('start')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['backup','restore']);parser.add_argument('--file',type=Path,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('Run as root on the authorized managed server')
    (backup if args.action=='backup' else restore)(args.file)
