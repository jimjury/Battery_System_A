"""Bounded interpreter discovery; never installs, upgrades or activates the manager."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from ab_platform.contracts import Fault

PROBE=Path(__file__).with_name('workers')/'probe.py'


def environment_for(executable):
    executable=Path(executable).resolve();env=os.environ.copy()
    for key in list(env):
        if any(word in key.upper() for word in ['TOKEN','SECRET','PASSWORD','CREDENTIAL','PRIVATE_KEY']):env.pop(key,None)
    prefix=executable.parent.parent if executable.parent.name.lower() in {'scripts','bin'} else executable.parent
    paths=[executable.parent,prefix/'Library/bin',prefix/'Scripts',prefix/'bin']
    env['PATH']=os.pathsep.join([str(p) for p in paths if p.exists()]+[env.get('PATH','')])
    env['PYTHONUTF8']='1';env['PYTHONNOUSERSITE']='1';env['CONDA_PREFIX']=str(prefix)
    env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
    return env


def probe(executable,timeout=25):
    executable=Path(executable).expanduser().resolve()
    if not executable.is_file():raise Fault('environment_missing','Selected interpreter does not exist')
    try:
        result=subprocess.run([str(executable),'-I',str(PROBE)],env=environment_for(executable),capture_output=True,timeout=timeout,check=False)
    except (OSError,subprocess.TimeoutExpired) as error:raise Fault('environment_probe','Interpreter failed or exceeded its probe timeout') from error
    if result.returncode or len(result.stdout)>16384:raise Fault('environment_probe','Interpreter probe failed; existing environment was not modified')
    try:report=json.loads(result.stdout)
    except ValueError as error:raise Fault('environment_probe','Interpreter did not return the probe contract') from error
    report['interpreter']=str(executable);return report


def discover(search_paths=()):
    candidates=[sys.executable,os.environ.get('AB_PYTHON'),shutil.which('python'),shutil.which('python3')]
    candidates.extend(str(Path(value)) for value in search_paths)
    conda=os.environ.get('CONDA_EXE') or shutil.which('conda.exe') or shutil.which('conda')
    if not conda:
        for path in [Path(sys.executable).parent/'Scripts/conda.exe',Path(sys.executable).parent/'bin/conda']:
            if path.is_file():conda=str(path);break
    if conda and Path(conda).suffix.lower() not in {'.bat','.cmd'}:
        try:
            result=subprocess.run([conda,'env','list','--json'],capture_output=True,timeout=10,check=False)
            for prefix in json.loads(result.stdout).get('envs',[])[:32]:
                candidates.append(str(Path(prefix)/('python.exe' if os.name=='nt' else 'bin/python')))
        except (OSError,ValueError,subprocess.TimeoutExpired):pass
    seen=set();reports=[]
    for value in candidates:
        if not value:continue
        candidate=Path(value).expanduser()
        if candidate.is_dir():candidate=candidate/('python.exe' if os.name=='nt' else 'bin/python')
        key=os.path.normcase(str(candidate.resolve()))
        if key in seen or not candidate.is_file():continue
        seen.add(key)
        try:reports.append({'available':True,**probe(candidate)})
        except Fault as error:reports.append({'available':False,'interpreter':str(candidate),'code':error.code,'message':error.message})
    return reports


def matches(version,constraint):
    def numbers(text):
        match=re.match(r'^(\d+)(?:\.(\d+))?(?:\.(\d+))?',str(text))
        if not match:raise Fault('version_constraint','Unrecognized dependency version')
        return tuple(int(x or 0) for x in match.groups())
    actual=numbers(version)
    for term in str(constraint).split(','):
        match=re.fullmatch(r'\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+){0,2})\s*',term)
        if not match:raise Fault('version_constraint','Use explicit comma-separated semantic version comparisons')
        target=numbers(match[2]);operation=match[1]
        if not {'>=':actual>=target,'<=':actual<=target,'==':actual==target,'>':actual>target,'<':actual<target}[operation]:return False
    return True


def select(reports,requirements,requested=None,device='auto',supported=('cpu',)):
    errors=[]
    for report in reports:
        if not report.get('available'):continue
        if requested and os.path.normcase(report['interpreter'])!=os.path.normcase(str(Path(requested).resolve())):continue
        if not matches(report['python_version'],requirements.get('python','>=3.10')):continue
        missing=[name for name,constraint in requirements.get('packages',{}).items()
                 if not report['packages'].get(name) or not matches(report['packages'][name],constraint)]
        if missing:errors.extend(missing);continue
        selected='cuda' if device=='auto' and 'cuda' in supported and report.get('cuda_available') else ('cpu' if device=='auto' else device)
        if selected not in supported or (selected=='cuda' and not report.get('cuda_available')):continue
        return {**report,'device':selected}
    raise Fault('environment_incompatible','No probed environment matches the deployment/device. Missing dependencies: '+(', '.join(sorted(set(errors))) or 'version or compute-device contract'))
