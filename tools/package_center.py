"""Create a versioned C source bundle without credentials or private state."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def package(version):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',version):raise ValueError('Invalid immutable version')
    files={}
    for folder in ['ab_platform','control_center']:
        for file in (ROOT/folder).rglob('*'):
            if not file.is_file() or file.suffix not in {'.py','.html','.css','.js'}:continue
            if any(part in {'.runtime','__pycache__'} for part in file.parts):continue
            files[file.relative_to(ROOT).as_posix()]=file.read_bytes()
    files['VERSION']=version.encode()
    manifest={'version':version,'files':{name:hashlib.sha256(data).hexdigest() for name,data in sorted(files.items())}}
    directory=ROOT/'dist';directory.mkdir(exist_ok=True);path=directory/('center-'+version+'.zip')
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):archive.writestr(name,data)
        archive.writestr('MANIFEST.json',json.dumps(manifest,sort_keys=True).encode())
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'files':len(files),'version':version}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--version',required=True)
    print(json.dumps(package(parser.parse_args().version),indent=2))
