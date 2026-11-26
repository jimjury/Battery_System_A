"""MCP stdio adapter for the same scoped API. No shell, arbitrary paths or model installs.

File references are explicitly allowlisted by the operator. Submit/upload are
disabled unless the launcher policy includes --allow-submit.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from ab_platform.contracts import Fault,canonical,validate
from .client import ABClient,APIError


def object_schema(properties,required=()):return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}
STRING={'type':'string','minLength':1,'maxLength':80}
TOOLS=[
    {'name':'list_nodes','description':'List nodes visible through the explicitly selected A, B or C endpoint. Does not bypass A.',
     'inputSchema':object_schema({'role':{'type':'string','enum':['A','B']}}),'annotations':{'readOnlyHint':True}},
    {'name':'list_deployments','description':'Read only the current authorized model/variant/deployment catalog.',
     'inputSchema':object_schema({'node_id':STRING},['node_id']),'annotations':{'readOnlyHint':True}},
    {'name':'upload_authorized_file','description':'Upload an operator-approved file reference. Arbitrary filesystem paths are forbidden.',
     'inputSchema':object_schema({'file_ref':STRING},['file_ref']),'annotations':{'readOnlyHint':False,'destructiveHint':False}},
    {'name':'submit_task','description':'Submit a bounded task using existing authorized file IDs and deployments; requires operator submit policy.',
     'inputSchema':object_schema({'inputs':{'type':'object','additionalProperties':True},'targets':{'type':'array','items':{'type':'object','additionalProperties':True},'minItems':1,'maxItems':8},'idempotency_key':STRING},['inputs','targets']),
     'annotations':{'readOnlyHint':False,'destructiveHint':False}},
    {'name':'get_task','description':'Read one task using the caller scopes and ownership.', 'inputSchema':object_schema({'task_id':STRING},['task_id']),'annotations':{'readOnlyHint':True}},
    {'name':'cancel_task','description':'Request cancellation of an authorized task; a request is not a worker cancellation ACK.',
     'inputSchema':object_schema({'task_id':STRING},['task_id']),'annotations':{'readOnlyHint':False,'destructiveHint':False}},
    {'name':'get_result','description':'Read actual results; demo/research labels and immutable execution identities are retained.',
     'inputSchema':object_schema({'task_id':STRING},['task_id']),'annotations':{'readOnlyHint':True}},
]


class MCP:
    def __init__(self,client,*,file_refs=None,allow_submit=False):
        self.client=client;self.allow_submit=allow_submit;self.files={name:Path(path).resolve() for name,path in (file_refs or {}).items()}
        self.initialized=False;self.ready=False
    def handle(self,message):
        if not isinstance(message,dict) or message.get('jsonrpc')!='2.0':return {'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid JSON-RPC request'}}
        identifier=message.get('id');method=message.get('method');params=message.get('params',{})
        if method=='notifications/initialized':self.ready=self.initialized;return None
        if 'id' not in message:return None
        response={'jsonrpc':'2.0','id':identifier}
        if not isinstance(params,dict):
            response['error']={'code':-32602,'message':'Parameters must be an object'}
            return response
        try:
            if method=='initialize':
                requested=params.get('protocolVersion');version=requested if requested in {'2025-06-18','2024-11-05'} else '2025-06-18'
                self.initialized=True;response['result']={'protocolVersion':version,'capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'ab-scoped-api','version':'3.0.0'}}
            elif method=='ping':response['result']={}
            elif not self.ready:response['error']={'code':-32000,'message':'Complete MCP initialization first'}
            elif method=='tools/list':response['result']={'tools':TOOLS}
            elif method=='tools/call':
                name=params.get('name');definition=next((item for item in TOOLS if item['name']==name),None)
                if definition is None:raise Fault('unknown_tool','Tool is not exposed')
                arguments=params.get('arguments',{});validate(arguments,definition['inputSchema'],'arguments')
                result=self.call(name,arguments)
                response['result']={'content':[{'type':'text','text':canonical(result).decode()}],'structuredContent':result,'isError':False}
            else:response['error']={'code':-32601,'message':'Method not found'}
        except (Fault,APIError,ValueError,OSError) as error:
            # Returned errors contain no interpreter paths, file contents or credentials.
            code=error.code if isinstance(error,Fault) else error.body.get('code','api_denied') if isinstance(error,APIError) else 'invalid_arguments'
            response['result']={'content':[{'type':'text','text':'Request rejected: '+code}],'isError':True}
        return response
    def call(self,name,args):
        if name in {'submit_task','upload_authorized_file','cancel_task'} and not self.allow_submit:
            raise Fault('operator_read_only','Operator policy is read-only; enable submission explicitly')
        if name=='list_nodes':return self.client.list_nodes(args.get('role','B'))
        if name=='list_deployments':return self.client.list_deployments(args['node_id'])
        if name=='upload_authorized_file':
            path=self.files.get(args['file_ref'])
            if path is None or not path.is_file() or path.is_symlink():raise Fault('file_not_authorized','Only operator-approved references may be uploaded',403)
            return self.client.upload_file(path)
        if name=='submit_task':
            if self.client.target=='B':
                if len(args['targets'])!=1:raise Fault('local_target','Local B task expects one explicit deployment')
                target=args['targets'][0]
                return self.client.submit_local(target['deployment_id'],target['deployment_version'],args['inputs'],target.get('parameters',{}),idempotency_key=args.get('idempotency_key'))
            return self.client.submit_group(args['inputs'],args['targets'],args.get('idempotency_key'))
        if name=='get_task':return self.client.get_task(args['task_id'])
        if name=='cancel_task':return self.client.cancel_task(args['task_id'])
        if name=='get_result':return self.client.get_result(args['task_id'])
        raise Fault('unknown_tool','Tool is not exposed')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--base-url',required=True);parser.add_argument('--target',choices=['A','B','C'],required=True)
    parser.add_argument('--allow-local-http',action='store_true');parser.add_argument('--allow-submit',action='store_true');parser.add_argument('--file-refs',type=Path);parser.add_argument('--ca-file')
    args=parser.parse_args();token=os.environ.get('AB_API_TOKEN')
    if not token:parser.error('Set process-local AB_API_TOKEN; never put a secret in argv or source')
    refs=json.loads(args.file_refs.read_text()) if args.file_refs else {}
    server=MCP(ABClient(args.base_url,token,target=args.target,allow_local_http=args.allow_local_http,ca_file=args.ca_file),file_refs=refs,allow_submit=args.allow_submit)
    for raw in sys.stdin.buffer:
        if len(raw)>65536:
            print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Message limit exceeded'}}),flush=True);continue
        try:response=server.handle(json.loads(raw))
        except (ValueError,TypeError):response={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Invalid JSON'}}
        if response is not None:print(json.dumps(response,separators=(',',':'),allow_nan=False),flush=True)


if __name__=='__main__':main()
