import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets

from ab_platform.contracts import Fault, integer, strict_json
from ab_platform.http_service import Response, Sessions, json_body
from .core import Center


class CenterAPI:
    def __init__(self,root,admin_token=None,broker_config=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        secret_file=self.root/'admin_token.txt'
        if admin_token is None:
            if not secret_file.exists():secret_file.write_text(secrets.token_urlsafe(32)+'\n',encoding='ascii')
            admin_token=secret_file.read_text(encoding='ascii').strip()
        self.admin_token=admin_token;self.sessions=Sessions(admin_token);self.center=Center(self.root)
        self.broker=None
        if broker_config:
            from .mqtt_bridge import BrokerBridge
            self.broker=BrokerBridge(self.center,broker_config)

    def dispatch(self,method,path,query,headers,raw,peer):
        if path=='/health' and method=='GET':return {'service':'center','status':'ok','api_version':1,'model_execution':False,'database':'ok'}
        if path=='/api/v1/session' and method=='POST':
            body=json_body(headers,raw);token,csrf=self.sessions.login(body.get('password'),peer)
            return Response({'csrf':csrf},200,{'Set-Cookie':f'ab_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600'})
        if path in {'/','/admin.js','/admin.css'} and method=='GET':
            file=Path(__file__).parent/'ui'/('index.html' if path=='/' else path[1:])
            return Response(file.read_bytes(),headers={'Content-Type':'text/html; charset=utf-8' if path=='/' else 'text/javascript' if path.endswith('.js') else 'text/css',
                'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"},binary=True)
        if path=='/openapi.json' and method=='GET':return openapi()
        bearer=headers.get('Authorization','').removeprefix('Bearer ')
        is_admin=bool(bearer) and hmac.compare_digest(bearer,self.admin_token)
        if path.startswith('/api/v1/admin/'):
            if not is_admin:self.sessions.authenticate(headers,method!='GET')
            body=json_body(headers,raw)
            if path=='/api/v1/admin/enrollments' and method=='POST':return {'code':self.center.issue_enrollment(body.get('role'),body.get('tenant_id','default')),'expires_in':300}
            if path=='/api/v1/admin/status' and method=='GET':
                with self.center.lock:
                    return {name:self.center.db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ['devices','pairings','tasks','uploads']}
            if path=='/api/v1/admin/nodes' and method=='GET':
                with self.center.lock:
                    rows=self.center.db.execute('SELECT id,alias,role,enabled,online_until FROM devices ORDER BY role,alias LIMIT 200').fetchall()
                    return {'nodes':[{k:row[k] for k in ['id','alias','role','enabled']}|{'online':row['online_until']>self.center.clock()} for row in rows]}
            raise Fault('not_found','Admin endpoint not found',404)
        if path=='/api/v1/devices/enroll' and method=='POST':
            self.center.throttle('enroll-peer:'+peer,30)
            body=json_body(headers,raw);return self.center.enroll(body.get('code'),body.get('role'),body.get('node_id'),body.get('alias'))
        p=self.center.authenticate(bearer)
        body={} if method=='GET' else ({} if '/parts/' in path else json_body(headers,raw,256*1024 if path=='/api/v1/catalog' else 16384))
        if path=='/api/v1/heartbeat' and method=='POST':return self.center.heartbeat(p,body.get('instance_id'),body.get('session_epoch'),body.get('accepting',True),body.get('load',0))
        if path=='/api/v1/mqtt/session' and method=='POST':
            if not self.broker:raise Fault('mqtt_not_configured','HTTPS polling is available; broker is not configured',503)
            with self.center.lock:self.center.session(p,headers.get('X-Instance-ID'),epoch(headers))
            return self.broker.session(p,bearer)
        if path=='/api/v1/credentials/rotate' and method=='POST':
            if self.broker:self.broker.disable(p.node_id)
            return self.center.rotate(p)
        if path=='/api/v1/nodes' and method=='GET':return {'nodes':self.center.nodes(p,query.get('role',[None])[0])}
        if path=='/api/v1/pairing-codes' and method=='POST':return self.center.pair_code(p,body.get('deployments'),body.get('scopes'),body.get('lifetime',300),body.get('max_active',2))
        if path=='/api/v1/pairings' and method=='POST':return self.center.pair(p,body.get('b_id'),body.get('code'))
        if path=='/api/v1/pairings' and method=='GET':return {'pairings':self.center.list_pairings(p)}
        if path.startswith('/api/v1/pairings/') and method=='DELETE':return self.center.revoke(p,path.rsplit('/',1)[1])
        if path=='/api/v1/catalog' and method=='PUT':return self.center.publish_catalog(p,body.get('deployments'))
        match=re.fullmatch(r'/api/v1/nodes/([0-9a-f-]{36})/deployments',path)
        if match and method=='GET':
            result=self.center.catalog(p,match[1]);offset=int(query.get('offset',['0'])[0]);limit=int(query.get('limit',['4'])[0])
            if not integer(offset) or not integer(limit,1,8):raise Fault('page','Invalid bounded catalog page')
            values=result['deployments'];result['deployments']=values[offset:offset+limit];result.update(offset=offset,total=len(values));return result
        if path=='/api/v1/uploads' and method=='POST':
            return self.center.create_upload(p,body.get('name'),body.get('size'),body.get('sha256'),body.get('task_id'),body.get('attempt_id'),body.get('lease_epoch'),headers.get('X-Instance-ID'),epoch(headers))
        match=re.fullmatch(r'/api/v1/uploads/([0-9a-f-]{36})(?:/(finalize|parts/([0-9]+)))?',path)
        if match:
            if method=='GET' and not match[2]:return self.center.upload_status(p,match[1])
            if method=='POST' and match[2]=='finalize':return self.center.finalize(p,match[1])
            if method=='PUT' and match[3]:return self.center.put_part(p,match[1],int(match[3]),raw,headers.get('X-Part-SHA256'))
        match=re.fullmatch(r'/api/v1/files/([0-9a-f-]{36})',path)
        if match and method=='GET':
            if p.role=='B':
                with self.center.lock:self.center.session(p,headers.get('X-Instance-ID'),epoch(headers))
            offset=0;length=None;status=200;range_header=headers.get('Range')
            if range_header:
                selected=re.fullmatch(r'bytes=([0-9]+)-([0-9]*)',range_header)
                if not selected:raise Fault('range','Only one explicit byte range is supported',416)
                offset=int(selected[1]);length=int(selected[2])-offset+1 if selected[2] else None;status=206
                if length is not None and length<1:raise Fault('range','Invalid byte range',416)
            meta,chunks=self.center.read_file(p,match[1],offset,length)
            out={'Content-Type':'application/octet-stream','Content-Length':meta['length'],'X-File-SHA256':meta['sha256'],'Accept-Ranges':'bytes',
                 'Content-Disposition':f'attachment; filename="{match[1]}.bin"'}
            if status==206:out['Content-Range']=f"bytes {offset}-{offset+meta['length']-1}/{meta['size']}"
            return Response(chunks,status,out,True)
        if path=='/api/v1/task-groups' and method=='POST':
            result=self.center.create_group(p,body.get('idempotency_key'),body.get('inputs'),body.get('targets'))
            return compact_group(result) if headers.get('X-Client-Capabilities')=='esp32-light' else result
        if path.startswith('/api/v1/task-groups/') and method=='GET':
            result=self.center.get_group(p,path.rsplit('/',1)[1]);return compact_group(result) if headers.get('X-Client-Capabilities')=='esp32-light' else result
        if path=='/api/v1/tasks' and method=='GET':return {'tasks':[compact_task(t) for t in self.center.list_tasks(p,query.get('active',['false'])[0]=='true',int(query.get('limit',['16'])[0]),int(query.get('offset',['0'])[0]))]}
        if path=='/api/v1/tasks/pending' and method=='GET':return {'tasks':self.center.pending(p,headers.get('X-Instance-ID'),epoch(headers))}
        match=re.fullmatch(r'/api/v1/tasks/([0-9a-f-]{36})(?:/(claim|renew|events|complete|cancel|results))?',path)
        if match:
            task_id,action=match[1],match[2]
            if method=='GET':
                task=self.center.get_task(p,task_id)
                return {'task_id':task_id,'state':task['state'],'result':task['result']} if action=='results' else (compact_task(task) if headers.get('X-Client-Capabilities')=='esp32-light' else task)
            if method=='POST' and action=='cancel':return self.center.cancel(p,task_id)
            if method=='POST' and action=='claim':return self.center.claim(p,task_id,headers.get('X-Instance-ID'),epoch(headers))
            args=(p,task_id,headers.get('X-Instance-ID'),epoch(headers),body.get('attempt_id'),body.get('lease_epoch'))
            if method=='POST' and action=='renew':return self.center.renew(*args)
            if method=='POST' and action in {'events','complete'}:
                return self.center.transition(p,task_id,'completed' if action=='complete' else body.get('state'),headers.get('X-Instance-ID'),epoch(headers),body.get('attempt_id'),body.get('lease_epoch'),body.get('result'))
        if path=='/api/v1/events' and method=='GET':return self.center.events(p,int(query.get('after',['0'])[0]),int(query.get('limit',['32'])[0]))
        if path=='/api/v1/api-keys' and method=='POST':return self.center.api_key(p,body.get('scopes'))
        if path.startswith('/api/v1/api-keys/') and method=='DELETE':return self.center.revoke_key(p,path.rsplit('/',1)[1])
        raise Fault('not_found','Versioned endpoint not found',404)

    def close(self):
        if self.broker:self.broker.close()
        self.center.close()


def epoch(headers):
    try:return int(headers.get('X-Session-Epoch','-1'))
    except ValueError:raise Fault('session_epoch','Integer session epoch required')


def compact_task(task):
    return {key:value for key,value in task.items() if key not in {'snapshot','result'}}


def compact_group(group):return {**group,'tasks':[compact_task(task) for task in group['tasks']]}


def openapi():
    paths={}
    for path,methods in {'/health':['get'],'/api/v1/devices/enroll':['post'],'/api/v1/heartbeat':['post'],
        '/api/v1/nodes':['get'],'/api/v1/pairing-codes':['post'],'/api/v1/pairings':['get','post'],
        '/api/v1/nodes/{id}/deployments':['get'],'/api/v1/uploads':['post'],
        '/api/v1/uploads/{id}/parts/{offset}':['put'],'/api/v1/uploads/{id}/finalize':['post'],
        '/api/v1/files/{id}':['get'],'/api/v1/task-groups':['post'],'/api/v1/tasks/{id}':['get'],
        '/api/v1/tasks/{id}/claim':['post'],'/api/v1/tasks/{id}/renew':['post'],
        '/api/v1/tasks/{id}/complete':['post'],'/api/v1/tasks/{id}/cancel':['post'],'/api/v1/events':['get']}.items():
        paths[path]={method:{'responses':{'200':{'description':'Authorized response'},'403':{'description':'Permission denied'}},'security':[{'bearerAuth':[]}]} for method in methods}
    return {'openapi':'3.1.0','info':{'title':'AB Independent Center C','version':'3.0.0'},'paths':paths,
            'components':{'securitySchemes':{'bearerAuth':{'type':'http','scheme':'bearer'}}}}
