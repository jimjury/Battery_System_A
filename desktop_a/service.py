import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid

from ab_platform.contracts import Fault,canonical,new_id
from ab_platform.http_service import Response,Sessions,json_body
from sdk import ABClient,APIError

SCOPES={'status:read','catalog:read','task:submit','task:read:own','task:cancel:own','file:write:own','file:read:own'}


class AService:
    def __init__(self,root,admin_token=None):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock()
        password=self.root/'admin_password.txt'
        if admin_token is None:
            if not password.exists():password.write_text(secrets.token_urlsafe(24)+'\n',encoding='ascii')
            admin_token=password.read_text().strip()
        self.secret=admin_token;self.sessions=Sessions(admin_token)
        self.config_file=self.root/'identity.json'
        self.config=json.loads(self.config_file.read_text()) if self.config_file.exists() else {'node_id':new_id(),'url':'','credential':'','alias':'PC A','development_http':False,'ca_file':None}
        profile=Path(__file__).resolve().parents[1]/'client_config/center.json'
        if not self.config_file.exists() and profile.exists():
            public=json.loads(profile.read_text());self.config.update(url=public['url'],ca_file=str((profile.parent/public['ca_file']).resolve()))
        self.save();self.client=None;self.online=False;self.error='not_configured';self.started=time.monotonic();self.stop=threading.Event()
        self.keys_file=self.root/'api_keys.json';self.keys=json.loads(self.keys_file.read_text()) if self.keys_file.exists() else {}
        self.thread=threading.Thread(target=self.loop,name='pc-a-heartbeat',daemon=True);self.thread.start()

    def save(self):
        tmp=self.config_file.with_suffix('.tmp');tmp.write_bytes(canonical(self.config));tmp.replace(self.config_file)

    def principal(self,headers,write=False):
        bearer=headers.get('Authorization','').removeprefix('Bearer ')
        if bearer:
            if hmac.compare_digest(bearer,self.secret):return {'admin':True,'scopes':SCOPES}
            digest=hashlib.sha256(bearer.encode()).hexdigest()
            with self.lock:
                for value in self.keys.values():
                    if not value['revoked'] and hmac.compare_digest(value['hash'],digest):return {'admin':False,'scopes':set(value['scopes'])}
            raise Fault('authentication_required','Invalid local A credential',401)
        self.sessions.authenticate(headers,write);return {'admin':True,'scopes':SCOPES}

    @staticmethod
    def require(principal,scope):
        if not principal['admin'] and scope not in principal['scopes']:raise Fault('scope_denied','Local API key does not grant this operation',403)

    def get_client(self):
        if not self.config.get('credential'):raise Fault('not_registered','Configure and enroll this PC A first',409)
        if self.client is None:
            self.client=ABClient(self.config['url'],self.config['credential'],target='C',ca_file=self.config.get('ca_file'),allow_local_http=self.config.get('development_http',False))
        return self.client

    def configure(self,body):
        url=body.get('url');alias=body.get('alias','PC A');ca=body.get('ca_file') or None
        if not isinstance(alias,str) or not 1<=len(alias)<=64:raise Fault('alias','Alias must contain 1..64 characters')
        try:ABClient(url,target='C',ca_file=ca,allow_local_http=body.get('development_http',False))
        except (ValueError,OSError,TypeError) as error:raise Fault('center_config','Use a valid HTTPS origin and readable CA file') from error
        with self.lock:
            if self.config['credential'] and url.rstrip('/')!=self.config['url']:raise Fault('identity_bound','Transfer identity explicitly before changing C',409)
            self.config.update(url=url.rstrip('/'),alias=alias,ca_file=ca,development_http=body.get('development_http',False) is True);self.client=None;self.save()
        return self.status()

    def register(self,body):
        with self.lock:
            if self.config['credential']:raise Fault('already_registered','A is already enrolled',409)
            if not self.config['url']:raise Fault('center_config','Configure C first')
            client=ABClient(self.config['url'],target='C',ca_file=self.config.get('ca_file'),allow_local_http=self.config.get('development_http',False))
            result=client.enroll(body.get('code'),'A',body.get('alias',self.config['alias']),self.config['node_id'])
            self.config['credential']=result['credential'];self.save();self.client=client
            client.heartbeat();self.online=True;self.error=''
        return {'registered':True,'node_id':self.config['node_id']}

    def status(self):
        return {'node_id':self.config['node_id'],'alias':self.config['alias'],'url':self.config['url'],
            'configured':bool(self.config['url']),'registered':bool(self.config['credential']),'online':self.online,
            'error':self.error,'runtime':'pc','control_transport':'HTTPS','mqtt_status':'polling_fallback',
            'uptime_s':time.monotonic()-self.started,'ca_file':self.config.get('ca_file')}

    def loop(self):
        while not self.stop.wait(1):
            if not self.config['credential']:continue
            try:
                with self.lock:self.get_client().heartbeat()
                self.online=True;self.error=''
            except (APIError,Fault,OSError,ValueError):self.online=False;self.error='center_unavailable_or_identity_rejected'
            self.stop.wait(14)

    def dispatch(self,method,path,query,headers,raw,peer):
        if method=='GET' and path=='/health':return {'service':'desktop_A','runtime':'pc','api_version':1,'center_online':self.online}
        if method=='GET' and path=='/openapi.json':return openapi()
        if method=='GET' and path in {'/','/platform.js','/sha256.js','/style.css'}:
            root=Path(__file__).resolve().parents[1]
            file=Path(__file__).parent/'ui/index.html' if path=='/' else root/'web'/path[1:]
            mime='text/html; charset=utf-8' if path=='/' else 'text/css' if path.endswith('.css') else 'text/javascript'
            return Response(file.read_bytes(),headers={'Content-Type':mime,'Content-Security-Policy':"default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"},binary=True)
        if path in {'/api/session','/api/v1/session'} and method=='POST':
            token,csrf=self.sessions.login(json_body(headers,raw).get('password'),peer)
            return Response({'csrf':csrf},headers={'Set-Cookie':f'ab_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600'})
        principal=self.principal(headers,method!='GET')
        if path in {'/api/session','/api/v1/session'} and method=='GET':return {'authenticated':True,'csrf':self.sessions.authenticate(headers)['csrf']}
        if path=='/api/v1/device/status' and method=='GET':
            self.require(principal,'status:read');return {'runtime':'pc','device_id':self.config['node_id'],'center':self.status(),'uptime_s':time.monotonic()-self.started,'firmware':'PC-A','ui_version':'3.1.0','version':'pc-a-3.1.0','wifi_online':None}
        if path in {'/api/v1/center/status','/api/v1/center/config'} and method=='GET':
            self.require(principal,'status:read');return self.status()
        if path=='/api/v1/center/config' and method in {'POST','PUT'}:
            self.require(principal,'admin');return self.configure(json_body(headers,raw))
        if path=='/api/v1/center/register' and method=='POST':
            self.require(principal,'admin');return self.register(json_body(headers,raw))
        if path=='/api/v1/api-keys' and method=='POST':
            self.require(principal,'admin');body=json_body(headers,raw)
            with self.lock:
                if body.get('revoke_id'):
                    if body['revoke_id'] in self.keys:self.keys[body['revoke_id']]['revoked']=True
                    result={'revoked':True}
                else:
                    scopes=body.get('scopes')
                    if not isinstance(scopes,list) or not scopes or not set(scopes)<=SCOPES:raise Fault('scope_denied','Unsupported API grant')
                    token=secrets.token_urlsafe(32);identity=new_id();self.keys[identity]={'hash':hashlib.sha256(token.encode()).hexdigest(),'scopes':scopes,'revoked':False};result={'id':identity,'token':token,'scopes':scopes}
                self.keys_file.write_bytes(canonical(self.keys));return result
        scope=operation_scope(method,path)
        self.require(principal,scope)
        upstream='/api/v1/files/'+path.rsplit('/',1)[1] if path.startswith('/api/v1/artifacts/') else path
        if query:
            from urllib.parse import urlencode
            upstream+='?'+urlencode(query,doseq=True)
        try:
            if method=='GET' and re.fullmatch(r'/api/v1/files/[0-9a-f-]{36}',upstream):return self.download(upstream)
            with self.lock:
                client=self.get_client()
                if '/parts/' in path:
                    if len(raw)>16384:raise Fault('part_limit','File parts are limited to 16 KiB',413)
                    result=client.request(method,upstream,raw=raw,headers={'Content-Type':'application/octet-stream','X-Part-SHA256':headers.get('X-Part-SHA256','')})
                else:result=client.request(method,upstream,json_body(headers,raw) if method!='GET' else None)
            self.online=True;self.error='';return result
        except APIError as error:raise Fault(error.body.get('code','center_error'),error.body.get('message','C rejected the request'),error.status) from None
        except (OSError,ValueError) as error:
            self.online=False;self.error='center_unavailable';raise Fault('center_unavailable','C cannot be reached; local A page remains available',503) from error

    def download(self,path):
        with self.lock:
            client=self.get_client();request=urllib.request.Request(client.base+path,headers=client.headers())
            response=client.opener.open(request,timeout=45)
        length=response.headers.get('Content-Length');digest=response.headers.get('X-File-SHA256')
        if not length or not length.isdigit() or int(length)>64*1024*1024 or not re.fullmatch('[0-9a-f]{64}',digest or ''):
            response.close();raise Fault('file_metadata','C returned invalid private file metadata',502)
        def chunks():
            sha=hashlib.sha256();size=0
            try:
                while True:
                    block=response.read(16384)
                    if not block:break
                    size+=len(block);sha.update(block)
                    if size>int(length):raise Fault('file_size','Private file exceeded declared size',502)
                    yield block
                if size!=int(length) or sha.hexdigest()!=digest:raise Fault('file_integrity','Private file integrity failed',502)
            finally:response.close()
        return Response(chunks(),headers={'Content-Type':'application/octet-stream','Content-Length':length,'X-File-SHA256':digest},binary=True)

    def close(self):self.stop.set();self.thread.join(timeout=50)


def operation_scope(method,path):
    patterns=[('GET',r'/api/v1/nodes','catalog:read'),('GET',r'/api/v1/nodes/[0-9a-f-]{36}/deployments','catalog:read'),
        ('GET',r'/api/v1/pairings','catalog:read'),('POST',r'/api/v1/pairings','admin'),('DELETE',r'/api/v1/pairings/[0-9a-f-]{36}','admin'),
        ('POST',r'/api/v1/uploads','file:write:own'),('GET',r'/api/v1/uploads/[0-9a-f-]{36}','file:write:own'),
        ('PUT',r'/api/v1/uploads/[0-9a-f-]{36}/parts/[0-9]+','file:write:own'),('POST',r'/api/v1/uploads/[0-9a-f-]{36}/finalize','file:write:own'),
        ('GET',r'/api/v1/(?:files|artifacts)/[0-9a-f-]{36}','file:read:own'),('POST',r'/api/v1/task-groups','task:submit'),
        ('GET',r'/api/v1/task-groups/[0-9a-f-]{36}','task:read:own'),('GET',r'/api/v1/tasks','task:read:own'),
        ('GET',r'/api/v1/tasks/[0-9a-f-]{36}(?:/results)?','task:read:own'),('POST',r'/api/v1/tasks/[0-9a-f-]{36}/cancel','task:cancel:own'),
        ('GET',r'/api/v1/events','task:read:own')]
    for verb,pattern,scope in patterns:
        if method==verb and re.fullmatch(pattern,path):return scope
    raise Fault('not_found','PC A exposes declared A operations only',404)


def openapi():
    return {'openapi':'3.1.0','info':{'title':'Desktop A','version':'3.1.0'},'paths':{path:{method:{'responses':{'200':{'description':'Authenticated A operation'}}} for method in methods} for path,methods in
        {'/health':['get'],'/api/v1/center/config':['get','put'],'/api/v1/center/register':['post'],'/api/v1/nodes':['get'],'/api/v1/task-groups':['post'],'/api/v1/tasks/{id}/results':['get']}.items()}}
