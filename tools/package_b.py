"""Build a portable LIGHT source package. No identities, keys, data or environments."""
import hashlib
from pathlib import Path
import zipfile
from release import ROOT


def package(destination=None):
    destination=Path(destination or ROOT/'dist/AB_B_light_v3.zip');destination.parent.mkdir(parents=True,exist_ok=True)
    files={}
    for folder in ['ab_platform','server_b','sdk']:
        for file in (ROOT/folder).rglob('*'):
            if not file.is_file():continue
            rel=file.relative_to(ROOT)
            if any(part in {'.runtime','__pycache__','.venv','private_models','logs','data','output'} for part in rel.parts):continue
            if file.suffix not in {'.py','.json','.html','.css','.js','.md','.txt','.ps1'}:continue
            files[rel.as_posix()]=file
    for name in ['web/platform.js','web/sha256.js','web/style.css','Start_B.cmd','Start_B.sh','docs/portable_b.md','docs/model_plugin_guide.md']:
        file=ROOT/name
        if file.exists():files[name]=file
    forbidden=[file.read_bytes().strip() for file in [ROOT/'.local/update_key.txt',ROOT/'.local/admin_password.txt',ROOT/'.local/release_private.pem'] if file.exists()]
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,file in sorted(files.items()):
            data=file.read_bytes()
            if any(secret and secret in data for secret in forbidden):raise ValueError('Refusing to package a local secret')
            archive.writestr(name,data)
    with zipfile.ZipFile(destination) as archive:
        for name in archive.namelist():
            if '.runtime/' in name or name.endswith(('.pem','.key','.db')):raise ValueError('Private state entered portable package')
    return {'path':str(destination),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'files':len(files),'runtime_bundled':False,'identity_included':False}


if __name__=='__main__':
    import json
    print(json.dumps(package(),indent=2))
