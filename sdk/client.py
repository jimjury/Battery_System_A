"""Explicit target SDK. Uses no proxy, never disables TLS or redirects credentials."""
import hashlib
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
import time


class APIError(Exception):
    def __init__(self,status,body):
        self.status=status;self.body=body;super().__init__(body.get('message','API request failed'))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):raise APIError(502,{'message':'Credentialed API redirects are forbidden'})


class ABClient:
    def __init__(self,base_url,token=None,*,target='C',ca_file=None,allow_local_http=False):
        url=urllib.parse.urlsplit(base_url)
        if url.scheme!='https' and not (allow_local_http and url.scheme=='http' and url.hostname in {'127.0.0.1','localhost','192.168.7.1'}):
            raise ValueError('HTTPS required; explicitly opt into loopback or direct USB HTTP')
        if url.username or url.password or url.query or url.fragment or url.path not in {'','/'}:raise ValueError('Expected a credential-free service origin')
        if target not in {'A','B','C'}:raise ValueError('Select explicit A, B or C target; no implicit route bypass')
        self.base=base_url.rstrip('/');self.token=token;self.target=target;self.instance=None;self.epoch=None
        context=ssl.create_default_context(cafile=ca_file)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),NoRedirect())

    def headers(self):
        headers={'X-Request-ID':str(uuid.uuid4())}
        if self.token:headers['Authorization']='Bearer '+self.token
        if self.instance:headers['X-Instance-ID']=self.instance
        if self.epoch is not None:headers['X-Session-Epoch']=str(self.epoch)
        return headers

    def request(self,method,path,value=None,*,raw=None,headers=None):
        if not path.startswith('/api/v1/') and path not in {'/health','/openapi.json'}:raise ValueError('SDK only calls versioned API paths')
        options=self.headers();options.update(headers or {})
        if raw is None and value is not None:raw=json.dumps(value,separators=(',',':'),allow_nan=False).encode();options['Content-Type']='application/json'
        request=urllib.request.Request(self.base+path,data=raw,headers=options,method=method)
        try:
            with self.opener.open(request,timeout=45) as response:
                data=response.read(1024*1024+1)
                if len(data)>1024*1024:raise APIError(502,{'code':'response_limit','message':'Use the bounded file download API for large data'})
                if 'application/json' not in response.headers.get('Content-Type',''):return data
                value=json.loads(data)
                if self.target=='A' and response.status==202 and value.get('job_id'):
                    deadline=time.monotonic()+60
                    while time.monotonic()<deadline:
                        job=self.request('GET','/api/v1/center/jobs/'+value['job_id'])
                        if job['state']=='done':return job['response']
                        if job['state']=='failed':raise APIError(job.get('http_status',502),job.get('response',{'message':'C operation failed'}))
                        time.sleep(.15)
                    raise APIError(504,{'message':'A accepted the request but no durable C result arrived before timeout'})
                return value
        except urllib.error.HTTPError as error:
            try:body=json.loads(error.read(65536))
            except ValueError:body={'message':'API returned a non-JSON error'}
            raise APIError(error.code,body) from None

    def enroll(self,code,role,alias,node_id=None):
        if self.target=='A':raise ValueError('A enrollment is a USB administrator operation; unlock its local management session first')
        value=self.request('POST','/api/v1/devices/enroll',{'code':code,'role':role,'alias':alias,'node_id':node_id or str(uuid.uuid4())})
        self.token=value['credential'];return value

    def heartbeat(self,accepting=True,load=0):
        if not self.instance:self.instance=str(uuid.uuid4())
        result=self.request('POST','/api/v1/heartbeat',{'instance_id':self.instance,'session_epoch':self.epoch,'accepting':accepting,'load':load})
        self.epoch=result['session_epoch'];return result

    def list_nodes(self,role='B'):return self.request('GET','/api/v1/nodes?role='+role)
    def list_deployments(self,node_id,offset=0):return self.request('GET',f'/api/v1/nodes/{node_id}/deployments?offset={offset}&limit=4')
    def pair(self,b_id,code):return self.request('POST','/api/v1/pairings',{'b_id':b_id,'code':code})
    def create_pairing_code(self,**policy):return self.request('POST','/api/v1/pairing-codes',policy)
    def revoke_pairing(self,pairing_id):return self.request('DELETE','/api/v1/pairings/'+pairing_id,{})
    def publish_catalog(self,deployments):return self.request('PUT','/api/v1/catalog',{'deployments':deployments})

    def upload_file(self,path,*,upload_id=None,task=None,name=None,progress=None):
        path=Path(path);sha=hashlib.sha256()
        with path.open('rb') as file:
            while True:
                block=file.read(65536)
                if not block:break
                sha.update(block)
        if upload_id:
            info=self.request('GET','/api/v1/uploads/'+upload_id)
            if info['sha256']!=sha.hexdigest() or info['size']!=path.stat().st_size:raise ValueError('Resume file identity mismatch')
        else:
            fields={'name':name or path.name,'size':path.stat().st_size,'sha256':sha.hexdigest()}
            if task:fields.update(task_id=task['task_id'],attempt_id=task['attempt_id'],lease_epoch=task['lease_epoch'])
            info=self.request('POST','/api/v1/uploads',fields);upload_id=info['upload_id']
        with path.open('rb') as file:
            offset=info['offset'];file.seek(offset)
            while True:
                block=file.read(16384)
                if not block:break
                receipt=self.request('PUT',f'/api/v1/uploads/{upload_id}/parts/{offset}',raw=block,
                                     headers={'X-Part-SHA256':hashlib.sha256(block).hexdigest(),'Content-Type':'application/octet-stream'})
                if receipt['offset']!=offset+len(block):raise ValueError('Remote durable offset mismatch')
                offset=receipt['offset']
                if progress:progress()
        return self.request('POST',f'/api/v1/uploads/{upload_id}/finalize',{})

    def submit_group(self,inputs,targets,idempotency_key=None):
        return self.request('POST','/api/v1/task-groups',{'inputs':inputs,'targets':targets,'idempotency_key':idempotency_key or str(uuid.uuid4())})
    def get_group(self,group_id):return self.request('GET','/api/v1/task-groups/'+group_id)
    def get_task(self,task_id):return self.request('GET','/api/v1/tasks/'+task_id)
    def cancel_task(self,task_id):return self.request('POST',f'/api/v1/tasks/{task_id}/cancel',{})
    def get_result(self,task_id):return self.request('GET',f'/api/v1/tasks/{task_id}/results')
    def pending(self):return self.request('GET','/api/v1/tasks/pending')['tasks']
    def claim(self,task_id):return self.request('POST',f'/api/v1/tasks/{task_id}/claim',{})
    def transition(self,task,state,result=None):
        value={'attempt_id':task['attempt_id'],'lease_epoch':task['lease_epoch'],'state':state,'result':result}
        action='complete' if state=='completed' else 'events'
        return self.request('POST',f"/api/v1/tasks/{task['task_id']}/{action}",value)
    def renew(self,task):return self.request('POST',f"/api/v1/tasks/{task['task_id']}/renew",{'attempt_id':task['attempt_id'],'lease_epoch':task['lease_epoch']})

    def download_file(self,file_id,destination,progress=None):
        destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
        headers=self.headers();request=urllib.request.Request(self.base+'/api/v1/files/'+file_id,headers=headers)
        try:
            with self.opener.open(request,timeout=45) as response, destination.open('wb') as output:
                expected=response.headers.get('X-File-SHA256');sha=hashlib.sha256();size=0
                while True:
                    block=response.read(16384)
                    if not block:break
                    output.write(block);sha.update(block);size+=len(block)
                    if progress:progress()
                if expected and sha.hexdigest()!=expected:raise ValueError('Downloaded result checksum mismatch')
        except urllib.error.HTTPError as error:
            try:body=json.load(error)
            except ValueError:body={'message':'Private file access failed'}
            raise APIError(error.code,body) from None
        return {'path':str(destination),'sha256':sha.hexdigest(),'size':size}

    def submit_local(self,deployment_id,version,inputs,parameters,**options):
        if self.target!='B':raise ValueError('submit_local is only for an explicit local B target')
        return self.request('POST','/api/v1/tasks',{'deployment_id':deployment_id,'deployment_version':version,'inputs':inputs,'parameters':parameters,**options})
