import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time
import uuid

from ab_platform.contracts import Fault,canonical,identity,identifier,new_id
from ab_platform.http_service import Response,Sessions,json_body
from .engine import Engine
from .environments import discover,probe
from .remote import RemoteAgent


class BService:
    def __init__(self,root,admin_token=None):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        file=self.root/'admin_password.txt'
        if admin_token is None:
            if not file.exists():file.write_text(secrets.token_urlsafe(24)+'\n',encoding='ascii')
            admin_token=file.read_text().strip()
        self.secret=admin_token;self.sessions=Sessions(admin_token);self.engine=Engine(self.root);self.remote=RemoteAgent(self.engine)
        self.started=time.monotonic()

    def principal(self,headers,write=False):
        bearer=headers.get('Authorization','').removeprefix('Bearer ')
        if bearer:
            if hmac.compare_digest(bearer,self.secret):return {'owner':'local:administrator','admin':True,'scopes':{'admin'}}
            with self.engine.lock:
                row=self.engine.db.execute('SELECT * FROM api_keys WHERE hash=? AND revoked IS NULL',(hashlib.sha256(bearer.encode()).hexdigest(),)).fetchone()
            if row:return {'owner':'api:'+row['id'],'admin':False,'scopes':set(json.loads(row['scopes']))}
            raise Fault('authentication_required','Invalid or revoked local API credential',401)
        self.sessions.authenticate(headers,write);return {'owner':'local:administrator','admin':True,'scopes':{'admin'}}

    @staticmethod
    def require(p,scope):
        if not p['admin'] and scope not in p['scopes']:raise Fault('scope_denied','API credential does not grant this local operation',403)

    def dispatch(self,method,path,query,headers,raw,peer):
        if path=='/health' and method=='GET':return {'service':'portable_B','local_engine':'ok','database':'ok','api_version':1,
            'center':self.remote.status()['online'],'local_inference_independent':True,'scheduler_alive':bool(self.engine.scheduler and self.engine.scheduler.is_alive())}
        if path=='/openapi.json' and method=='GET':return openapi()
        if path in {'/','/platform.js','/sha256.js','/style.css'} and method=='GET':
            name='index.html' if path=='/' else path[1:];file=Path(__file__).parent/'ui'/name if name=='index.html' else Path(__file__).resolve().parents[1]/'web'/name
            if not file.exists():raise Fault('ui_not_built','Portable B UI is not available in this package',503)
            mime={'index.html':'text/html; charset=utf-8','platform.js':'text/javascript; charset=utf-8','sha256.js':'text/javascript; charset=utf-8','style.css':'text/css; charset=utf-8'}[name]
            return Response(file.read_bytes(),headers={'Content-Type':mime,'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"},binary=True)
        if path=='/api/v1/session' and method=='POST':
            body=json_body(headers,raw);token,csrf=self.sessions.login(body.get('password'),peer)
            return Response({'csrf':csrf},200,{'Set-Cookie':f'ab_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600'})
        p=self.principal(headers,method!='GET');owner=p['owner'];body={} if method=='GET' or '/parts/' in path else json_body(headers,raw)
        if path=='/api/v1/session' and method=='GET':return {'authenticated':True,'csrf':self.sessions.authenticate(headers)['csrf']}
        if path=='/api/v1/status' and method=='GET':
            self.require(p,'task:read:own')
            with self.engine.lock:
                counts={row[0]:row[1] for row in self.engine.db.execute('SELECT state,COUNT(*) FROM tasks GROUP BY state')}
            return {'node_id':self.engine.node_id,'mode':'local_and_remote','center':self.remote.status(),'tasks':counts,
                    'uptime_s':time.monotonic()-self.started,'scheduler_capacity':1,'queue_limit':32,'per_origin_limit':4,
                    'models_loaded':len(self.engine.registry.deployments)}
        if path=='/api/v1/environments' and method=='GET':
            self.require(p,'environments:manage');return {'environments':self.engine.environments}
        if path=='/api/v1/environments/probe' and method=='POST':
            self.require(p,'environments:manage')
            if body.get('interpreter'):
                reports=[{'available':True,**probe(body['interpreter'])}]
                by_path={r['interpreter']:r for r in self.engine.environments};by_path[reports[0]['interpreter']]=reports[0];self.engine.environments=list(by_path.values())
            else:self.engine.environments=discover(body.get('search_paths',[]))
            return {'environments':self.engine.environments,'environment_changes':False}
        if path=='/api/v1/models/import' and method=='POST':
            self.require(p,'models:manage')
            if peer not in {'127.0.0.1','::1'}:raise Fault('local_import','Trusted model registration is local administrator-only',403)
            self.engine.registry.import_package(body.get('path',''),body.get('trusted') is True);return {'deployments':self.engine.registry.catalog()}
        if path=='/api/v1/models' and method=='GET':
            self.require(p,'catalog:read');models={}
            for item in self.engine.registry.catalog():
                key=item['model_id']+'@'+item['model_version'];models.setdefault(key,{'model_id':item['model_id'],'model_version':item['model_version'],'title':item['title'],'mode':item['mode'],'variants':[]})
                variant={'variant_id':item['variant_id'],'variant_version':item['variant_version'],'weights_sha256':item['weights_sha256']}
                if variant not in models[key]['variants']:models[key]['variants'].append(variant)
            return {'models':list(models.values())}
        if (path=='/api/v1/deployments' or re.fullmatch(r'/api/v1/nodes/[0-9a-f-]{36}/deployments',path)) and method=='GET':
            self.require(p,'catalog:read');offset=int(query.get('offset',['0'])[0]);limit=min(8,max(1,int(query.get('limit',['8'])[0])))
            if path.startswith('/api/v1/nodes/') and path.split('/')[4]!=self.engine.node_id:raise Fault('not_found','This local B does not proxy another node',404)
            items=self.engine.registry.catalog();return {'deployments':items[offset:offset+limit],'total':len(items),'offset':offset}
        match=re.fullmatch(r'/api/v1/deployments/([A-Za-z0-9_.-]+)/validate',path)
        if match and method=='POST':
            self.require(p,'models:manage');return self.engine.validate_deployment(match[1],body.get('deployment_version'),body.get('interpreter'),body.get('device','auto'))
        match=re.fullmatch(r'/api/v1/deployments/([A-Za-z0-9_.-]+)/availability',path)
        if match and method=='POST':
            self.require(p,'models:manage');return self.engine.registry.set_enabled(match[1],body.get('deployment_version'),body.get('enabled'))
        if path=='/api/v1/uploads' and method=='POST':
            self.require(p,'file:write:own');return self.engine.create_upload(owner,body.get('name'),body.get('size'),body.get('sha256'))
        match=re.fullmatch(r'/api/v1/uploads/([0-9a-f-]{36})(?:/(finalize|parts/([0-9]+)))?',path)
        if match:
            self.require(p,'file:write:own')
            if method=='GET' and not match[2]:return self.engine.upload_status(owner,match[1])
            if method=='POST' and match[2]=='finalize':return self.engine.finalize(owner,match[1])
            if method=='PUT' and match[3]:return self.engine.put_part(owner,match[1],int(match[3]),raw,headers.get('X-Part-SHA256'))
        if path=='/api/v1/tasks' and method=='POST':
            self.require(p,'task:submit');key=body.get('idempotency_key')
            task_id=str(uuid.uuid5(uuid.NAMESPACE_URL,owner+':'+key)) if isinstance(key,str) and 1<=len(key)<=80 else None
            return self.engine.submit(owner,body.get('deployment_id'),body.get('deployment_version'),body.get('inputs'),body.get('parameters',{}),
                interpreter=body.get('interpreter'),device=body.get('device','auto'),task_id=task_id)
        if path=='/api/v1/tasks' and method=='GET':
            self.require(p,'task:read:own');return {'tasks':self.engine.list_tasks(owner,p['admin'])}
        match=re.fullmatch(r'/api/v1/tasks/([0-9a-f-]{36})(?:/(cancel|results))?',path)
        if match:
            self.require(p,'task:cancel:own' if match[2]=='cancel' else 'task:read:own')
            if method=='POST' and match[2]=='cancel':return self.engine.cancel(owner,match[1],p['admin'])
            if method=='GET':
                result=self.engine.task(owner,match[1],p['admin'])
                return {'task_id':match[1],'state':result['state'],'result':result['result']} if match[2]=='results' else result
        match=re.fullmatch(r'/api/v1/(?:files|artifacts)/([0-9a-f-]{36})',path)
        if match and method=='GET':
            self.require(p,'file:read:own');info=self.engine.file_info(owner,match[1],p['admin'])
            def chunks():
                with info['path'].open('rb') as file:
                    while True:
                        data=file.read(16384)
                        if not data:break
                        yield data
            return Response(chunks(),headers={'Content-Type':'application/octet-stream','Content-Length':info['size'],
                'X-File-SHA256':info['sha256'],'Content-Disposition':f'attachment; filename="{match[1]}.bin"'},binary=True)
        if path=='/api/v1/remote/status' and method=='GET':self.require(p,'remote:manage');return self.remote.status()
        if path=='/api/v1/remote/config' and method=='POST':
            self.require(p,'remote:manage');return self.remote.configure(body.get('url'),body.get('development_http',False),body.get('ca_file'))
        if path=='/api/v1/remote/register' and method=='POST':
            self.require(p,'remote:manage');return self.remote.register(body.get('code'),body.get('alias','B'))
        if path=='/api/v1/remote/accepting' and method=='POST':self.require(p,'remote:manage');return self.remote.set_accepting(body.get('accepting'))
        if path=='/api/v1/remote/nodes' and method=='GET':self.require(p,'remote:manage');return self.remote.get_client().list_nodes('A')
        if path=='/api/v1/remote/pairings' and method=='GET':self.require(p,'remote:manage');return self.remote.get_client().request('GET','/api/v1/pairings')
        if path=='/api/v1/remote/pairing-codes' and method=='POST':self.require(p,'remote:manage');return self.remote.get_client().create_pairing_code(**body)
        if path.startswith('/api/v1/remote/pairings/') and method=='DELETE':self.require(p,'remote:manage');return self.remote.get_client().revoke_pairing(path.rsplit('/',1)[1])
        if path=='/api/v1/api-keys' and method=='POST':
            self.require(p,'admin');scopes=body.get('scopes');allowed={'catalog:read','task:submit','task:read:own','task:cancel:own','file:write:own','file:read:own'}
            if not isinstance(scopes,list) or not set(scopes)<=allowed:raise Fault('scope_denied','Unsupported local client scopes')
            secret=secrets.token_urlsafe(32);key_id=new_id()
            with self.engine.transaction():self.engine.db.execute('INSERT INTO api_keys VALUES(?,?,?,NULL)',(key_id,hashlib.sha256(secret.encode()).hexdigest(),canonical(scopes).decode()))
            return {'id':key_id,'token':secret,'scopes':scopes}
        if path.startswith('/api/v1/api-keys/') and method=='DELETE':
            self.require(p,'admin')
            with self.engine.transaction():self.engine.db.execute('UPDATE api_keys SET revoked=? WHERE id=?',(time.time(),path.rsplit('/',1)[1]))
            return {'revoked':True}
        if path=='/api/v1/backup' and method=='POST':
            self.require(p,'admin');target=self.root/'backups'/new_id();self.engine.backup(target)
            return {'backup_id':target.name,'contains_credentials':False,'database_and_models':True,'identity_in_database':True,
                    'notice':'Local backup contains private task data; exclude it from public portable packages'}
        raise Fault('not_found','Portable B endpoint not found',404)

    def close(self):self.remote.close();self.engine.close()


def openapi():
    paths={path:{method:{'responses':{'200':{'description':'Authorized versioned response'},'403':{'description':'Denied'}},'security':[{'bearerAuth':[]}]} for method in methods}
        for path,methods in {'/health':['get'],'/api/v1/environments':['get'],'/api/v1/environments/probe':['post'],
            '/api/v1/models':['get'],'/api/v1/models/import':['post'],'/api/v1/deployments':['get'],
            '/api/v1/deployments/{id}/validate':['post'],'/api/v1/uploads':['post'],
            '/api/v1/uploads/{id}/parts/{offset}':['put'],'/api/v1/tasks':['get','post'],
            '/api/v1/tasks/{id}':['get'],'/api/v1/tasks/{id}/results':['get'],'/api/v1/tasks/{id}/cancel':['post'],
            '/api/v1/remote/status':['get'],'/api/v1/remote/pairing-codes':['post']}.items()}
    return {'openapi':'3.1.0','info':{'title':'Portable B Local/Remote AI','version':'3.0.0'},'paths':paths,
            'components':{'securitySchemes':{'bearerAuth':{'type':'http','scheme':'bearer'}}}}
