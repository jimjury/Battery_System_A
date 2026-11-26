"""One trusted task per subprocess. JSON stdin/stdout; no network/device credentials."""
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import platform
import sys
import time


class LimitedLog:
    def __init__(self,target,limit=65536):self.target=target;self.remaining=limit
    def write(self,text):
        if self.remaining>0:
            part=text[:self.remaining];self.target.write(part);self.remaining-=len(part)
        return len(text)
    def flush(self):self.target.flush()


def file_digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(65536),b''):value.update(block)
    return value.hexdigest()


def run(request):
    binding=request['binding'];package=Path(binding['package']).resolve();file=(package/binding['adapter']).resolve()
    if not file.is_relative_to(package) or file_digest(file)!=binding['public']['code_sha256']:
        raise ValueError('Trusted adapter checksum mismatch')
    for resource in binding.get('resources',[]):
        resource_path=(package/resource['name']).resolve()
        if not resource_path.is_relative_to(package) or file_digest(resource_path)!=resource['sha256']:
            raise ValueError('Trusted resource checksum mismatch')
    spec=importlib.util.spec_from_file_location('trusted_task_plugin',file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    adapter=module.create_adapter()
    if request['operation']=='validate':
        evidence=adapter.self_test(binding['variant'])
        return {'ok':bool(evidence.get('ok')),'self_test':evidence,'environment':environment()}
    root=Path.cwd().resolve();output=root/'output';output.mkdir(exist_ok=True)
    def inspect(value):
        if isinstance(value,dict) and 'path' in value:
            path=Path(value['path']).resolve()
            if not path.is_relative_to(root) or path.is_symlink() or file_digest(path)!=value['sha256']:
                raise ValueError('Task input escaped its private directory or failed integrity')
        elif isinstance(value,list):
            for entry in value:inspect(entry)
    for value in request['inputs'].values():inspect(value)
    adapter.validate(binding['public'],request['inputs'],request['parameters']);adapter.load(binding['public'],binding['variant'],request['device'])
    prepared=adapter.preprocess(request['inputs'],request['parameters']);started=time.perf_counter()
    result=adapter.postprocess(adapter.predict(prepared,lambda value:None,lambda:False,output))
    if result.get('mode')!=binding['public']['mode']:raise ValueError('Plugin output mode conflicts with immutable deployment')
    artifacts=[];total=0
    for item in result.get('artifacts',[]):
        path=(output/item['path']).resolve()
        if not path.is_relative_to(output) or path.is_symlink() or not path.is_file():raise ValueError('Result path outside private output directory')
        total+=path.stat().st_size
        if total>32*1024*1024:raise ValueError('Result quota exceeded')
        sha=hashlib.sha256()
        with path.open('rb') as source:
            while True:
                block=source.read(65536)
                if not block:break
                sha.update(block)
        artifacts.append({**item,'path':str(path),'size':path.stat().st_size,'sha256':sha.hexdigest()})
    result['artifacts']=artifacts;result['inference_ms']=(time.perf_counter()-started)*1000;result['environment']=environment()
    if len(json.dumps(result,allow_nan=False).encode())>65536:raise ValueError('Result summary exceeds limit')
    return {'ok':True,'result':result}


def environment():
    packages={}
    for name in ['torch','numpy','safetensors','onnxruntime']:
        try:packages[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:packages[name]=None
    return {'python_version':platform.python_version(),'executable':sys.executable,'platform':platform.platform(),'packages':packages}


def main():
    try:
        raw=sys.stdin.buffer.readline(262145)
        if len(raw)>262144:raise ValueError('IPC request limit exceeded')
        request=json.loads(raw)
        with contextlib.redirect_stdout(LimitedLog(sys.stderr)),contextlib.redirect_stderr(LimitedLog(sys.stderr)):
            response=run(request)
    except Exception as error:response={'ok':False,'code':'worker_failed','message':str(error)[:256],'error_type':type(error).__name__}
    print(json.dumps(response,separators=(',',':'),allow_nan=False),flush=True)


if __name__=='__main__':main()
