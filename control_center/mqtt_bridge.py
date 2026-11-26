"""Optional Mosquitto dynamic-security provisioning and transactional outbox.

Separate operator credentials for dynsec and application publishing are loaded
from a private server file. No default broker password or anonymous access.
"""
import json
from pathlib import Path
import threading
import time
import uuid

from ab_platform.contracts import Fault,canonical
from ab_platform.mqtt import MQTT

CONTROL='$CONTROL/dynamic-security/v1'


def role_for(node_id):
    topic=f'ab/v1/nodes/{node_id}/down/events'
    return {'command':'createRole','rolename':'node-'+node_id,'acls':[
        {'acltype':'subscribeLiteral','topic':topic,'priority':10,'allow':True},
        {'acltype':'publishClientReceive','topic':topic,'priority':10,'allow':True}]}


class BrokerBridge:
    def __init__(self,center,configuration):
        self.center=center;self.config=json.loads(Path(configuration).read_text(encoding='utf-8'));self.lock=threading.RLock()
        if set(self.config)-{'uri','public_uri','ca_file','admin','publisher','dedicated'}:raise ValueError('Unknown broker configuration fields')
        if self.config.get('dedicated') is not True:raise ValueError('A dedicated C broker with explicit operator authorization is required')
        self.initialized=False
        self.stop=threading.Event();self.online=False;self.error='not_connected';self.thread=threading.Thread(target=self.loop,name='c-mqtt-outbox',daemon=True);self.thread.start()
    def connection(self,role):
        credentials=self.config[role]
        return MQTT(self.config['uri'],credentials['username'],credentials['password'],'c-'+role+'-'+uuid.uuid4().hex[:12],ca_file=self.config.get('ca_file'))
    def command(self,command):
        client=self.connection('admin');correlation=uuid.uuid4().hex
        try:
            client.connect();client.subscribe(CONTROL+'/response')
            client.publish(CONTROL,canonical({'commands':[{**command,'correlationData':correlation}]}))
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                message=client.poll()
                if not message or message[0]!=CONTROL+'/response':continue
                result=json.loads(message[1])
                for response in result.get('responses',[]):
                    if response.get('correlationData')==correlation:
                        if response.get('error'):raise Fault('broker_control','Broker rejected credential/ACL operation',503)
                        return response
            raise Fault('broker_timeout','Broker control acknowledgment was not received',503)
        except (OSError,ValueError) as error:raise Fault('broker_unavailable','MQTT unavailable; HTTPS polling remains available',503) from error
        finally:client.close()
    def session(self,principal,credential):
        if principal.token_id:raise Fault('scope_denied','Device owner credential required for broker access',403)
        # Password and exact topics are reset together; no wildcard grants.
        with self.lock:
            if not self.initialized:
                self.command({'command':'setDefaultACLAccess','acls':[{'acltype':kind,'allow':False} for kind in
                    ['publishClientSend','publishClientReceive','subscribe','unsubscribe']]})
                role={'command':'createRole','rolename':'c-notification-publisher','acls':[
                    {'acltype':'publishClientSend','topic':'ab/v1/nodes/+/down/events','allow':True,'priority':10}]}
                try:self.command({'command':'getRole','rolename':role['rolename']})
                except Fault as error:
                    if error.code!='broker_control':raise
                    self.command(role)
                publisher=self.config['publisher'];fields={'username':publisher['username'],'password':publisher['password'],
                    'roles':[{'rolename':role['rolename'],'priority':10}]}
                if publisher['username']==self.config['admin']['username']:raise Fault('broker_config','Separate admin/publisher identities required')
                try:self.command({'command':'getClient','username':publisher['username']})
                except Fault as error:
                    if error.code!='broker_control':raise
                    self.command({'command':'createClient',**fields})
                else:self.command({'command':'modifyClient',**fields})
                self.initialized=True
            try:self.command({'command':'getRole','rolename':'node-'+principal.node_id})
            except Fault as error:
                if error.code!='broker_control':raise
                self.command(role_for(principal.node_id))
            fields={'username':principal.node_id,'password':credential,'clientid':'ab-'+principal.node_id,
                    'roles':[{'rolename':'node-'+principal.node_id,'priority':10}]}
            try:self.command({'command':'getClient','username':principal.node_id})
            except Fault as error:
                if error.code!='broker_control':raise
                self.command({'command':'createClient',**fields})
            else:self.command({'command':'modifyClient',**fields})
            self.command({'command':'enableClient','username':principal.node_id})
        return {'uri':self.config.get('public_uri',self.config['uri']),'username':principal.node_id,'client_id':'ab-'+principal.node_id,'qos':1,'authority':'HTTPS'}
    def disable(self,node_id):
        with self.lock:
            try:self.command({'command':'disableClient','username':node_id})
            except Fault as error:
                if error.code!='broker_control':raise
    def loop(self):
        backoff=1
        while not self.stop.is_set():
            client=None
            try:
                with self.center.lock:rows=self.center.db.execute('SELECT * FROM outbox WHERE sent=0 LIMIT 32').fetchall()
                if not rows:self.stop.wait(.5);continue
                client=self.connection('publisher');client.connect()
                for row in rows:
                    # MQTT contains only an opaque wake-up reference, no task/input/result.
                    with self.center.lock:node=self.center.db.execute('SELECT enabled FROM devices WHERE id=?',(row['target'],)).fetchone()
                    if node and node['enabled']:
                        client.publish(f"ab/v1/nodes/{row['target']}/down/events",canonical({'schema_version':1,'event_id':row['event_id']}))
                    with self.center.transaction():self.center.db.execute('UPDATE outbox SET sent=1 WHERE event_id=?',(row['event_id'],))
                self.online=True;self.error='';backoff=1
            except (OSError,ValueError,Fault):self.online=False;self.error='broker_unavailable'
            finally:
                if client:client.close()
            self.stop.wait(backoff);backoff=min(30,backoff*2)
    def close(self):self.stop.set();self.thread.join(timeout=12)
