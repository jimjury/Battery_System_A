import hashlib
import json
from pathlib import Path
import shutil
import sys

from ab_platform.contracts import Fault,canonical,digest,identifier,public_deployment
from .environments import probe,select


class Registry:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.deployments={}
        self.load()

    def import_package(self,path,trusted=False):
        if not trusted:raise Fault('trust_required','Model code must be reviewed and explicitly trusted by the local B administrator',403)
        path=Path(path).resolve()
        if not path.is_dir() or any(p.is_symlink() for p in path.rglob('*')):raise Fault('model_package','Model package must be a real directory without symlinks')
        try:model=json.loads((path/'model.json').read_text(encoding='utf-8'))
        except (OSError,ValueError) as error:raise Fault('model_manifest','Readable model.json required') from error
        if not identifier(model.get('model_id')) or not identifier(model.get('model_version')):raise Fault('model_id','Versioned model identity required')
        adapter=(path/model.get('adapter','')).resolve()
        if not adapter.is_relative_to(path) or not adapter.is_file():raise Fault('adapter_path','Trusted adapter must be inside its package')
        name=model['model_id']+'@'+model['model_version'];destination=self.root/name
        if destination.exists():
            if (destination/'model.json').read_bytes()!=(path/'model.json').read_bytes() or (destination/model['adapter']).read_bytes()!=adapter.read_bytes():
                raise Fault('immutable_model','Install a new model version rather than overwriting trusted code',409)
            return self.load()
        shutil.copytree(path,destination)
        (destination/'.trust.json').write_bytes(canonical({'trusted':True,'adapter_sha256':hashlib.sha256(adapter.read_bytes()).hexdigest()}))
        self.load()

    def load(self):
        deployments={}
        for path in self.root.iterdir():
            if not path.is_dir() or not (path/'.trust.json').exists():continue
            try:
                model=json.loads((path/'model.json').read_text(encoding='utf-8'));trust=json.loads((path/'.trust.json').read_text())
                adapter=(path/model['adapter']).resolve()
                if not adapter.is_relative_to(path.resolve()) or hashlib.sha256(adapter.read_bytes()).hexdigest()!=trust['adapter_sha256']:
                    raise Fault('code_changed','Trusted adapter changed; install and validate a new model version')
                variants={(v['variant_id'],v['variant_version']):v for v in model['variants']}
                for declared in model['deployments']:
                    variant=variants[(declared['variant_id'],declared['variant_version'])]
                    key=declared['deployment_id']+'@'+declared['deployment_version']
                    if key in deployments:raise Fault('duplicate_deployment','Deployment identity must be unique')
                    resources=[]
                    for name in variant.get('resources',[]):
                        file=(path/name).resolve()
                        if not file.is_relative_to(path.resolve()) or not file.is_file():raise Fault('weight_path','Weights/resources must be trusted files inside the package')
                        resources.append({'name':name,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
                    public={'schema_version':1,**model['contract'],**declared,'model_id':model['model_id'],'model_version':model['model_version'],
                        'title':model.get('title',model['model_id']),'mode':model['mode'],'availability':'not_validated',
                        'weights_sha256':digest({'variant':variant,'resources':resources}),'code_sha256':trust['adapter_sha256'],
                        'preprocessing_sha256':digest(variant.get('preprocessing',{}))}
                    binding={'public':public,'package':str(path.resolve()),'adapter':model['adapter'],'variant':variant,'resources':resources,
                             'requirements':model.get('requirements',{'python':'>=3.10','packages':{}}),'devices':model.get('devices',['cpu'])}
                    binding['snapshot_hash']=digest({k:v for k,v in binding.items() if k!='package'});public['snapshot_hash']=binding['snapshot_hash']
                    acceptance=path/(key+'.validated.json')
                    if acceptance.exists():
                        record=json.loads(acceptance.read_text())
                        if record.get('snapshot_hash')==binding['snapshot_hash'] and record.get('ok') is True:public['availability']='ready';binding['environment']=record['environment']
                    public_deployment(public);deployments[key]=binding
            except (OSError,ValueError,KeyError) as error:raise Fault('model_package','Malformed trusted model package') from error
        self.deployments=deployments
        return self.catalog()

    def catalog(self):return [dict(item['public']) for item in self.deployments.values()]
    def get(self,deployment_id,version):
        try:return self.deployments[deployment_id+'@'+version]
        except KeyError as error:raise Fault('deployment_not_found','Exact deployment version is unavailable',404) from error

    def accept(self,deployment_id,version,environment,self_test):
        binding=self.get(deployment_id,version)
        if self_test.get('ok') is not True:raise Fault('model_self_test','Model did not pass its explicit self-test')
        record={'snapshot_hash':binding['snapshot_hash'],'environment':environment,'ok':True,'self_test':self_test}
        (Path(binding['package'])/(deployment_id+'@'+version+'.validated.json')).write_bytes(canonical(record));self.load()
