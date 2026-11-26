"""Portable local/remote execution engine. Local work never depends on C."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

from ab_platform.contracts import Fault,canonical,digest,identity,integer,new_id,validate
from .environments import environment_for,probe,select
from .plugins import Registry

WORKER=Path(__file__).with_name('workers')/'execute.py'


def sha_file(path):
    sha=hashlib.sha256()
    with Path(path).open('rb') as file:
        while True:
            block=file.read(65536)
            if not block:break
            sha.update(block)
    return sha.hexdigest()


class Engine:
    def __init__(self,root,*,start_scheduler=True):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        for name in ['files','tasks','validation','models']: (self.root/name).mkdir(exist_ok=True)
        self.lock=threading.RLock();self.stop=threading.Event();self.wake=threading.Event();self.processes={};self.last_owner=None
        self.db=sqlite3.connect(self.root/'local.db',check_same_thread=False);self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;PRAGMA foreign_keys=ON;PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,owner TEXT,name TEXT,size INTEGER,sha TEXT,offset INTEGER,state TEXT);
        CREATE TABLE IF NOT EXISTS parts(id TEXT,offset INTEGER,size INTEGER,sha TEXT,PRIMARY KEY(id,offset));
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,owner TEXT,origin TEXT,state TEXT,snapshot TEXT,inputs TEXT,
          parameters TEXT,result TEXT,error TEXT,created REAL,updated REAL,priority INTEGER DEFAULT 0,attempt_id TEXT);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,task_id TEXT,kind TEXT,message TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS sync_outbox(task_id TEXT PRIMARY KEY,context TEXT,state TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS api_keys(id TEXT PRIMARY KEY,hash TEXT UNIQUE,scopes TEXT,revoked REAL);
        PRAGMA user_version=1;''')
        with self.db:
            if not self.db.execute("SELECT 1 FROM metadata WHERE key='node_id'").fetchone():self.db.execute('INSERT INTO metadata VALUES(?,?)',('node_id',new_id()))
            self.db.execute("UPDATE tasks SET state='failed',error='worker_interrupted_by_restart',updated=? WHERE state='running'",(time.time(),))
        self.node_id=self.db.execute("SELECT value FROM metadata WHERE key='node_id'").fetchone()[0]
        self.registry=Registry(self.root/'models')
        for path in (Path(__file__).parent/'models').iterdir():
            if path.is_dir() and (path/'model.json').exists():self.registry.import_package(path,trusted=True)
        self.environments=[{'available':True,**probe(sys.executable)}]
        # Only bundled deterministic demos receive automatic local validation.
        for binding in list(self.registry.deployments.values()):
            if binding['public']['mode']=='demo' and binding['public']['availability']!='ready':
                self.validate_deployment(binding['public']['deployment_id'],binding['public']['deployment_version'])
        self.scheduler=None
        if start_scheduler:
            self.scheduler=threading.Thread(target=self._scheduler,name='b-local-scheduler',daemon=True);self.scheduler.start()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            try:self.db.execute('BEGIN IMMEDIATE');yield;self.db.commit()
            except BaseException:self.db.rollback();raise

    def validate_deployment(self,identifier,version,interpreter=None,device='auto'):
        binding=self.registry.get(identifier,version)
        reports=self.environments if interpreter is None else [{'available':True,**probe(interpreter)}]
        environment=select(reports,binding['requirements'],interpreter,device,binding['devices'])
        directory=self.root/'validation'/new_id();directory.mkdir()
        response=self._worker({'operation':'validate','binding':binding},environment,directory,30)
        if not response.get('ok'):raise Fault('model_validation','Trusted model self-test failed: '+response.get('message','unknown error'))
        self.registry.accept(identifier,version,environment,response['self_test']);return {'validated':True,'environment':environment,'evidence':response['self_test']}

    def create_upload(self,owner,name,size,sha):
        if not isinstance(name,str) or not 1<=len(name)<=128 or any(x in name for x in ['/', '\\','\x00','\r','\n']):raise Fault('file_name','Use a filename without a path')
        if not integer(size,0,64*1024*1024) or not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise Fault('file_metadata','Bounded file size and SHA-256 required')
        file_id=new_id()
        with self.transaction():
            total=self.db.execute('SELECT COALESCE(SUM(size),0) FROM uploads').fetchone()[0]
            if total+size>1024*1024*1024:raise Fault('quota','Local prototype storage quota reached',507)
            self.db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,0,?)',(file_id,owner,name,size,sha,'uploading'));(self.root/'files'/file_id).touch(exist_ok=False)
        return {'upload_id':file_id,'file_id':file_id,'offset':0,'part_size':16384,'state':'uploading'}

    def _file(self,owner,file_id,admin=False):
        with self.lock:row=self.db.execute('SELECT * FROM uploads WHERE id=?',(file_id,)).fetchone()
        if not row or (not admin and row['owner']!=owner):raise Fault('not_found','Private local file not found',404)
        return row

    def upload_status(self,owner,file_id,admin=False):
        with self.lock:
            row=self._file(owner,file_id,admin);return {'upload_id':file_id,'offset':row['offset'],'size':row['size'],'sha256':row['sha'],'state':row['state']}

    def put_part(self,owner,file_id,offset,data,sha):
        if not integer(offset) or not 1<=len(data)<=16384 or hashlib.sha256(data).hexdigest()!=sha:raise Fault('part_hash','Invalid bounded file part')
        with self.transaction():
            row=self._file(owner,file_id);old=self.db.execute('SELECT * FROM parts WHERE id=? AND offset=?',(file_id,offset)).fetchone()
            if old:
                if old['sha']!=sha or old['size']!=len(data):raise Fault('part_conflict','Retry part differs',409)
                return {'offset':row['offset'],'state':'persisted','duplicate':True}
            if row['state']!='uploading' or row['offset']!=offset or offset+len(data)>row['size']:raise Fault('offset_conflict','Resume from durable offset',409)
            with (self.root/'files'/file_id).open('r+b') as file:file.seek(offset);file.write(data);file.flush();os.fsync(file.fileno())
            self.db.execute('INSERT INTO parts VALUES(?,?,?,?)',(file_id,offset,len(data),sha));self.db.execute('UPDATE uploads SET offset=? WHERE id=?',(offset+len(data),file_id))
        return {'offset':offset+len(data),'state':'persisted','duplicate':False}

    def finalize(self,owner,file_id):
        with self.transaction():
            row=self._file(owner,file_id)
            if row['offset']!=row['size'] or sha_file(self.root/'files'/file_id)!=row['sha']:raise Fault('file_hash','Upload incomplete or corrupt',409)
            self.db.execute("UPDATE uploads SET state='ready' WHERE id=?",(file_id,))
        return {'file_id':file_id,'size':row['size'],'sha256':row['sha'],'state':'ready'}

    def import_file(self,owner,path):
        path=Path(path);row=self.create_upload(owner,path.name,path.stat().st_size,sha_file(path));offset=0
        with path.open('rb') as file:
            while True:
                block=file.read(16384)
                if not block:break
                offset=self.put_part(owner,row['file_id'],offset,block,hashlib.sha256(block).hexdigest())['offset']
        return self.finalize(owner,row['file_id'])

    def submit(self,owner,deployment_id,version,inputs,parameters,*,origin='local',interpreter=None,device='auto',task_id=None,remote_context=None,priority=0):
        binding=self.registry.get(deployment_id,version)
        if binding['public']['availability']!='ready':raise Fault('deployment_not_ready','Validate the exact deployment and environment before execution',409)
        environment=select(self.environments if interpreter is None else [{'available':True,**probe(interpreter)}],binding['requirements'],interpreter,device,binding['devices'])
        specs={item['id']:item for item in binding['public']['inputs']}
        if not isinstance(inputs,dict) or inputs.keys()-specs.keys():raise Fault('input_contract','Unknown or invalid inputs')
        prepared={};files=[]
        for name,spec in specs.items():
            if name not in inputs:
                if spec.get('required',True):raise Fault('input_contract','Required input missing')
                continue
            value=inputs[name]
            if spec['kind'] in {'file','files'}:
                values=value if spec['kind']=='files' else [value]
                if not isinstance(values,list) or not 1<=len(values)<=8:raise Fault('input_files','Input file count exceeds bound')
                checked=[]
                for file_id in values:
                    row=self._file(owner,file_id)
                    if row['state']!='ready' or Path(row['name']).suffix.lower().lstrip('.') not in spec['formats']:raise Fault('input_format','Input file format or state conflicts with deployment')
                    checked.append({'file_id':file_id,'sha256':row['sha'],'name':row['name']});files.append(file_id)
                prepared[name]=checked if spec['kind']=='files' else checked[0]
            else:
                validate(value,spec.get('schema',{'type':'string' if spec['kind']=='text' else spec['kind']}),'inputs.'+name);prepared[name]=value
        validate(parameters,binding['public']['parameters_schema'])
        task_id=task_id or new_id()
        if not identity(task_id) or not integer(priority,0,10):raise Fault('task_metadata','Invalid task identity or priority')
        # Immutable model/variant hashes, selected environment and input hashes are frozen here.
        snapshot={'deployment':binding['public'],'binding':binding,'environment':environment,'device':environment['device'],
                  'inputs':prepared,'parameters':parameters,'origin':origin}
        with self.transaction():
            old=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if old:
                if old['owner']!=owner or digest(json.loads(old['snapshot']))!=digest(snapshot):raise Fault('task_conflict','Task retry changed its immutable execution snapshot',409)
                return self.task(owner,task_id)
            queued=self.db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('queued','running')").fetchone()[0]
            active=self.db.execute("SELECT COUNT(*) FROM tasks WHERE owner=? AND state IN ('queued','running')",(owner,)).fetchone()[0]
            if queued>=32 or active>=4:raise Fault('queue_full','Bounded scheduler or per-origin quota reached',429)
            now=time.time();attempt=new_id()
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (task_id,owner,origin,'queued',canonical(snapshot).decode(),canonical(prepared).decode(),canonical(parameters).decode(),None,None,now,now,priority,attempt))
            if remote_context:self.db.execute('INSERT INTO sync_outbox VALUES(?,?,?,NULL)',(task_id,canonical(remote_context).decode(),'pending'))
            self.db.execute('INSERT INTO events(task_id,kind,message,created) VALUES(?,?,?,?)',(task_id,'queued','Immutable deployment and environment bound',now))
        self.wake.set();return self.task(owner,task_id)

    def task(self,owner,task_id,admin=False):
        with self.lock:
            row=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row or (not admin and owner!=row['owner']):raise Fault('not_found','Task not owned by this local principal',404)
            value=dict(row);snapshot=json.loads(value.pop('snapshot'));value['snapshot']={'deployment':snapshot['deployment'],'environment':snapshot['environment'],'device':snapshot['device'],'inputs':snapshot['inputs'],'parameters':snapshot['parameters']}
            value['task_id']=value.pop('id');value.pop('inputs');value.pop('parameters');value['result']=json.loads(value['result']) if value['result'] else None
            sync=self.db.execute('SELECT state,error FROM sync_outbox WHERE task_id=?',(task_id,)).fetchone()
            value['sync_state']=sync['state'] if sync else 'local_only'
            if value['origin']=='remote' and value['state']=='completed' and value['sync_state']!='synced':value['state']='local_completed'
            return value

    def list_tasks(self,owner,admin=False,limit=32):
        with self.lock:
            rows=self.db.execute('SELECT id FROM tasks '+('' if admin else 'WHERE owner=? ')+'ORDER BY created DESC LIMIT ?', (limit,) if admin else (owner,limit)).fetchall()
            return [self.task(owner,row[0],admin) for row in rows]

    def cancel(self,owner,task_id,admin=False):
        with self.transaction():
            row=self.task(owner,task_id,admin)
            if row['state'] in {'completed','local_completed','failed','cancelled'}:return row
            process=self.processes.get(task_id)
            self.db.execute("UPDATE tasks SET state='cancelled',error='cancelled_by_owner',updated=? WHERE id=?",(time.time(),task_id))
            if process and process.poll() is None:process.terminate()
        return self.task(owner,task_id,admin)

    def file_info(self,owner,file_id,admin=False):
        with self.lock:
            row=self._file(owner,file_id,admin)
            if row['state']!='ready':raise Fault('file_incomplete','File not finalized',409)
            return {'file_id':file_id,'path':self.root/'files'/file_id,'size':row['size'],'sha256':row['sha'],'name':row['name']}

    def _worker(self,request,environment,directory,timeout,task_id=None):
        request={**request,'device':environment.get('device','cpu')}
        with (directory/'worker.log').open('wb') as log:
            process=subprocess.Popen([environment['interpreter'],'-I',str(WORKER)],cwd=directory,env=environment_for(environment['interpreter']),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log)
            if task_id:
                with self.lock:self.processes[task_id]=process
            try:output,_=process.communicate(canonical(request)+b'\n',timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill();process.communicate();raise Fault('worker_timeout','Execution time limit exceeded')
            finally:
                if task_id:
                    with self.lock:self.processes.pop(task_id,None)
            if log.tell()>65536:log.truncate(65536)
        if len(output)>65536:raise Fault('worker_output','Worker IPC exceeds output limit')
        try:value=json.loads(output)
        except ValueError as error:raise Fault('worker_crashed','Worker did not return a valid result contract') from error
        return value

    def _execute(self,task_id):
        with self.transaction():
            row=self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if row['state']!='queued':return
            snapshot=json.loads(row['snapshot']);self.db.execute("UPDATE tasks SET state='running',updated=? WHERE id=?",(time.time(),task_id))
        directory=self.root/'tasks'/task_id
        inputs={}
        def copy(value):
            if isinstance(value,dict) and 'file_id' in value:
                source=self.root/'files'/value['file_id'];suffix=Path(value['name']).suffix.lower();target=directory/(value['file_id']+suffix)
                shutil.copyfile(source,target);return {'path':str(target),'sha256':value['sha256']}
            if isinstance(value,list):return [copy(v) for v in value]
            return value
        try:
            directory.mkdir(exist_ok=True)
            inputs={name:copy(value) for name,value in snapshot['inputs'].items()}
            response=self._worker({'operation':'execute','binding':snapshot['binding'],'inputs':inputs,'parameters':snapshot['parameters']},snapshot['environment'],directory,120,task_id)
            if not response.get('ok'):raise Fault('model_error',response.get('message','Trusted model failed'))
            result=response['result'];artifacts=[]
            for item in result['artifacts']:
                imported=self.import_file(row['owner'],item['path'])
                if imported['sha256']!=item['sha256']:raise Fault('output_hash','Result changed during persistence')
                artifacts.append({'id':imported['file_id'],'kind':item['kind'],'format':item['format'],'size':imported['size'],'sha256':imported['sha256']})
            result.update(task_id=task_id,attempt_id=row['attempt_id'],artifacts=artifacts,deployment=snapshot['deployment'],input_hashes={name:value for name,value in snapshot['inputs'].items()})
            with self.transaction():
                current=self.db.execute('SELECT state FROM tasks WHERE id=?',(task_id,)).fetchone()[0]
                if current!='cancelled':self.db.execute("UPDATE tasks SET state='completed',result=?,updated=? WHERE id=?",(canonical(result).decode(),time.time(),task_id))
        except Exception as error:
            code=error.code if isinstance(error,Fault) else 'execution_failed'
            with self.transaction():self.db.execute("UPDATE tasks SET state='failed',error=?,updated=? WHERE id=? AND state<>'cancelled'",(code,time.time(),task_id))

    def _scheduler(self):
        while not self.stop.is_set():
            with self.lock:
                rows=self.db.execute("SELECT id,owner FROM tasks WHERE state='queued' ORDER BY priority DESC,created LIMIT 32").fetchall()
                chosen=next((row for row in rows if row['owner']!=self.last_owner),rows[0] if rows else None)
            if chosen:self.last_owner=chosen['owner'];self._execute(chosen['id'])
            else:self.wake.wait(.2);self.wake.clear()

    def backup(self,destination):
        destination=Path(destination);destination.mkdir(parents=True,exist_ok=False)
        with self.lock:
            target=sqlite3.connect(destination/'local.db')
            try:self.db.backup(target)
            finally:target.close()
            for name in ['files','models']:shutil.copytree(self.root/name,destination/name)
        return destination

    def close(self):
        self.stop.set();self.wake.set()
        with self.lock:
            for process in list(self.processes.values()):
                if process.poll() is None:process.terminate()
        if self.scheduler:self.scheduler.join(timeout=35)
        self.db.close()
