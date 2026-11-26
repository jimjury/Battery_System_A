"""Outbound C agent. Local tasks and local results remain independent of it."""
import json
from pathlib import Path
import threading
import time

from ab_platform.contracts import Fault,canonical
from sdk.client import ABClient,APIError
from ab_platform.mqtt import NodeNotifications


class RemoteAgent:
    def __init__(self,engine):
        self.engine=engine;self.path=engine.root/'center.json';self.lock=threading.RLock();self.stop=threading.Event()
        self.config=json.loads(self.path.read_text()) if self.path.exists() else {'url':'','credential':'','accepting':False,'development_http':False}
        self.client=None;self.online=False;self.error='';self.last_heartbeat=0;self.last_renew={};self.catalog_revision=None
        self.wake=threading.Event();self.mqtt_options=None;self.next_mqtt=0
        self.notifications=NodeNotifications(self.notification_config,self.wake)
        self.thread=threading.Thread(target=self._loop,name='b-outbound-center',daemon=True);self.thread.start()

    def configure(self,url,development_http=False,ca_file=None):
        ABClient(url,target='C',allow_local_http=development_http,ca_file=ca_file)
        with self.lock:
            if self.config.get('credential') and url!=self.config.get('url'):raise Fault('center_bound','Unregister/transfer this identity before changing its center',409)
            self.config.update(url=url,development_http=bool(development_http),ca_file=ca_file);self.save();self.client=None
        return self.status()

    def save(self):
        temporary=self.path.with_suffix('.tmp');temporary.write_bytes(canonical(self.config));temporary.replace(self.path)

    def register(self,code,alias):
        with self.lock:
            if not self.config.get('url'):raise Fault('center_config','Configure C first')
            if self.config.get('credential'):raise Fault('already_registered','This B identity is already registered',409)
            client=ABClient(self.config['url'],target='C',ca_file=self.config.get('ca_file'),allow_local_http=self.config.get('development_http',False))
            record=client.enroll(code,'B',alias,self.engine.node_id);self.config['credential']=record['credential'];self.config['accepting']=True;self.save();self.client=client
        return {'registered':True,'node_id':self.engine.node_id}

    def get_client(self):
        with self.lock:
            if not self.config.get('credential'):raise Fault('not_registered','B is not enrolled with C',409)
            if self.client is None:self.client=ABClient(self.config['url'],self.config['credential'],target='C',ca_file=self.config.get('ca_file'),allow_local_http=self.config.get('development_http',False))
            return self.client

    def status(self):
        with self.lock:return {'configured':bool(self.config.get('url')),'registered':bool(self.config.get('credential')),
            'url':self.config.get('url'),'node_id':self.engine.node_id,'online':self.online,'accepting':self.config.get('accepting',False),
            'error':self.error,'last_heartbeat_age_s':time.monotonic()-self.last_heartbeat if self.last_heartbeat else None,
            'transport':'https-authoritative-polling','mqtt_notifications':'connected' if self.notifications.online else self.notifications.error}

    def notification_config(self):
        with self.lock:return dict(self.mqtt_options) if self.mqtt_options else None

    def set_accepting(self,value):
        if type(value) is not bool:raise Fault('accepting','Explicit boolean required')
        with self.lock:self.config['accepting']=value;self.save()
        return self.status()

    def maintain(self,task=None):
        client=self.get_client();now=time.monotonic()
        if now-self.last_heartbeat>=15:
            client.heartbeat(self.config.get('accepting',False),len(self.engine.processes));self.last_heartbeat=now
        if not self.mqtt_options and now>=self.next_mqtt:
            self.next_mqtt=now+60
            try:
                session=client.request('POST','/api/v1/mqtt/session',{})
                with self.lock:self.mqtt_options={'uri':session['uri'],'username':self.engine.node_id,'password':self.config['credential'],
                    'client_id':session['client_id'],'ca_file':self.config.get('ca_file')}
            except (OSError,ValueError,APIError):pass
        if task and now-self.last_renew.get(task['task_id'],0)>=20:
            client.renew(task);self.last_renew[task['task_id']]=now

    def receive(self,task):
        client=self.get_client();owner='remote:'+task['a_id'];snapshot=task['snapshot'];inputs={}
        directory=self.engine.root/'remote_staging'/task['task_id'];directory.mkdir(parents=True,exist_ok=True)
        def transfer(value,spec):
            if spec['kind'] in {'file','files'}:
                values=value if spec['kind']=='files' else [value];ids=[]
                for file_id in values:
                    metadata=snapshot['input_metadata'][file_id];suffix=Path(metadata['name']).suffix
                    path=directory/(file_id+suffix)
                    self.maintain(task)
                    client.download_file(file_id,path,progress=lambda:self.maintain(task))
                    imported=self.engine.import_file(owner,path)
                    if imported['sha256']!=metadata['sha256']:raise Fault('input_hash','Downloaded input does not match immutable C snapshot')
                    ids.append(imported['file_id'])
                return ids if spec['kind']=='files' else ids[0]
            return value
        for spec in snapshot['deployment']['inputs']:
            if spec['id'] in snapshot['inputs']:inputs[spec['id']]=transfer(snapshot['inputs'][spec['id']],spec)
        selected=self.engine.registry.get(snapshot['deployment']['deployment_id'],snapshot['deployment']['deployment_version'])
        if selected['public']!=snapshot['deployment']:raise Fault('snapshot_changed','Local validated deployment differs from C frozen catalog',409)
        self.engine.submit(owner,selected['public']['deployment_id'],selected['public']['deployment_version'],inputs,snapshot['parameters'],origin='remote',task_id=task['task_id'],remote_context=task)

    def synchronize(self):
        client=self.get_client()
        with self.engine.lock:pending=self.engine.db.execute("SELECT * FROM sync_outbox WHERE state<>'synced' ORDER BY task_id LIMIT 32").fetchall()
        for row in pending:
            context=json.loads(row['context']);task_id=row['task_id'];self.maintain(context)
            current=client.get_task(task_id);local=self.engine.task('',task_id,admin=True)
            if current['state']=='cancel_requested':
                self.engine.cancel('',task_id,admin=True);client.transition(context,'cancelled')
                with self.engine.transaction():self.engine.db.execute("UPDATE sync_outbox SET state='synced',error=NULL WHERE task_id=?",(task_id,))
                continue
            if local['state']=='running' and current['state']=='accepted':client.transition(context,'running')
            elif local['state'] in {'failed','cancelled'}:
                if current['state'] not in {'completed','failed','cancelled'}:client.transition(context,local['state'])
                with self.engine.transaction():self.engine.db.execute("UPDATE sync_outbox SET state='synced' WHERE task_id=?",(task_id,))
            elif local['state']=='local_completed':
                if current['state']=='accepted':client.transition(context,'running')
                if current['state'] in {'accepted','running'}:client.transition(context,'result_staging')
                result=local['result'];artifacts=[]
                for item in result['artifacts']:
                    info=self.engine.file_info(local['owner'],item['id']);self.maintain(context)
                    remote=client.upload_file(info['path'],task=context,name=info['name'],progress=lambda:self.maintain(context))
                    artifacts.append({**item,'id':remote['file_id'],'sha256':remote['sha256']})
                environment={k:v for k,v in result.get('environment',{}).items() if k!='executable'}
                public={k:v for k,v in result.items() if k not in {'input_hashes','environment','artifacts','attempt_id'}}
                public.update(task_id=task_id,attempt_id=context['attempt_id'],artifacts=artifacts,environment=environment,
                              input_hashes={key:value['sha256'] for key,value in context['snapshot'].get('input_metadata',{}).items()})
                public['preview']=public.get('preview',[])[:32]
                while len(canonical(public))>7000 and public['preview']:public['preview'].pop()
                client.transition(context,'completed',public)
                with self.engine.transaction():self.engine.db.execute("UPDATE sync_outbox SET state='synced',error=NULL WHERE task_id=?",(task_id,))

    def _loop(self):
        backoff=1
        while not self.stop.is_set():
            if not self.config.get('credential'):self.stop.wait(.5);continue
            try:
                client=self.get_client();self.maintain()
                catalog=self.engine.registry.catalog();encoded=canonical(catalog)
                if encoded!=self.catalog_revision:client.publish_catalog(catalog);self.catalog_revision=encoded
                self.synchronize()
                if self.config.get('accepting'):
                    for task in client.pending():
                        if task['state']!='queued':continue
                        with self.engine.lock:exists=self.engine.db.execute('SELECT 1 FROM tasks WHERE id=?',(task['task_id'],)).fetchone()
                        if exists:continue
                        claimed=None
                        try:
                            claimed=client.claim(task['task_id']);self.receive(claimed)
                        except (Fault,APIError,OSError):
                            # One invalid input/deployment must not starve unrelated A queues.
                            if claimed:
                                try:client.transition(claimed,'failed')
                                except (Fault,APIError,OSError):pass
                            continue
                with self.lock:self.online=True;self.error=''
                backoff=1;self.wake.wait(.5);self.wake.clear()
            except Exception as error:
                with self.lock:self.online=False;self.error=error.body.get('code','center_unavailable') if isinstance(error,APIError) else (error.code if isinstance(error,Fault) else 'center_unavailable')
                # Persist diagnostics without claiming that local completion reached A.
                with self.engine.transaction():self.engine.db.execute("UPDATE sync_outbox SET error=? WHERE state<>'synced'",(self.error,))
                self.stop.wait(backoff);backoff=min(30,backoff*2)

    def close(self):self.stop.set();self.wake.set();self.notifications.close();self.thread.join(timeout=50)
