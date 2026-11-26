import importlib.metadata
import importlib.util
import json
import platform
import sys

report={'python_version':platform.python_version(),'platform':platform.platform(),'machine':platform.machine(),
        'executable':sys.executable,'packages':{},'cuda_available':False,'gpus':[]}
for name in ['torch','numpy','safetensors','onnxruntime','pandas','scikit-learn']:
    try:report['packages'][name]=importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:report['packages'][name]=None
if importlib.util.find_spec('torch'):
    try:
        import torch
        report['cuda_available']=bool(torch.cuda.is_available())
        if report['cuda_available']:
            for index in range(torch.cuda.device_count()):
                gpu=torch.cuda.get_device_properties(index)
                report['gpus'].append({'index':index,'name':gpu.name,'total_memory':gpu.total_memory})
    except Exception as error:report['torch_probe_error']=type(error).__name__
print(json.dumps(report,separators=(',',':'),allow_nan=False))
