"""Portable PC A+B source package; no identities, tokens, research data or keys."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def package():
    files={}
    for folder in ['ab_platform','desktop_a','server_b','sdk','client_config']:
        for file in (ROOT/folder).rglob('*'):
            if not file.is_file() or any(part in {'.runtime','__pycache__','private_models','.venv'} for part in file.relative_to(ROOT).parts):continue
            if file.suffix not in {'.py','.html','.css','.js','.json','.pem','.ps1','.md','.txt'}:continue
            if file.suffix=='.pem' and file.name!='cloud-ca.pem':continue
            files[file.relative_to(ROOT).as_posix()]=file.read_bytes()
    for name in ['web/platform.js','web/sha256.js','web/style.css','Start_A.cmd','Start_A.sh','Start_B.cmd','Start_B.sh','docs/desktop_a.md','docs/portable_b.md','docs/model_plugin_guide.md']:
        files[name]=(ROOT/name).read_bytes()
    if any(b'PRIVATE KEY-----' in data for data in files.values()):raise ValueError('Private key entered package')
    output=ROOT/'dist/AB_PC_A_B_3.1.0.zip';output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):archive.writestr(name,data)
        archive.writestr('MANIFEST.json',json.dumps({name:hashlib.sha256(data).hexdigest() for name,data in sorted(files.items())},sort_keys=True))
    return {'path':str(output),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'files':len(files),'credentials_included':False}


if __name__=='__main__':print(json.dumps(package(),indent=2))
