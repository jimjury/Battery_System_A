"""Bounded MQTT 3.1.1 notifications over verified TLS, with HTTPS authority.

QoS 1 duplicates intentionally carry no execution authority. No insecure mode,
proxy, weight transfer, arbitrary subscription API or persisted credentials.
"""
import json
import select
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlsplit


def string(value):
    raw=value.encode('utf-8')
    if not raw or len(raw)>2048 or '\x00' in value:raise ValueError('Bounded MQTT UTF-8 string required')
    return struct.pack('!H',len(raw))+raw


def packet(header,body=b''):
    if len(body)>8192:raise ValueError('MQTT notification/control packet exceeds limit')
    length=len(body);encoded=bytearray()
    while True:
        digit=length%128;length//=128;encoded.append(digit|(128 if length else 0))
        if not length:break
    return bytes([header])+encoded+body


class MQTT:
    def __init__(self,uri,username,password,client_id,*,ca_file=None):
        url=urlsplit(uri)
        if url.scheme!='mqtts' or not url.hostname or url.username or url.password or url.path not in {'','/'} or url.query or url.fragment:
            raise ValueError('Credential-free mqtts:// origin required')
        self.host=url.hostname;self.port=url.port or 8883;self.context=ssl.create_default_context()
        if ca_file:self.context.load_verify_locations(cafile=ca_file)
        self.username=username;self.password=password;self.client_id=client_id;self.socket=None;self.sequence=0;self.messages=[]
        self.last_io=0;self.ping_at=0
    def connect(self):
        self.close();raw=socket.create_connection((self.host,self.port),timeout=8)
        try:self.socket=self.context.wrap_socket(raw,server_hostname=self.host)
        except BaseException:raw.close();raise
        self.socket.settimeout(8)
        # Clean session; durable replay/cursors are provided by HTTPS/outbox.
        self.send(0x10,b'\x00\x04MQTT\x04\xc2\x00\x1e'+string(self.client_id)+string(self.username)+string(self.password))
        header,body=self.receive()
        if header!=0x20 or body!=b'\x00\x00':self.close();raise OSError('MQTT authentication/connection rejected')
        return self
    def close(self):
        if self.socket:
            try:self.socket.close()
            except OSError:pass
        self.socket=None;self.messages=[];self.ping_at=0
    def send(self,header,body=b''):
        if not self.socket:raise OSError('MQTT is disconnected')
        self.socket.sendall(packet(header,body));self.last_io=time.monotonic()
    def read(self,size):
        connection=self.socket
        if connection is None:raise OSError('MQTT disconnected')
        data=bytearray()
        while len(data)<size:
            block=connection.recv(size-len(data))
            if not block:raise OSError('MQTT disconnected')
            data.extend(block)
        return bytes(data)
    def receive(self):
        header=self.read(1)[0];length=0;multiplier=1
        for index in range(4):
            digit=self.read(1)[0];length+=(digit&127)*multiplier
            if length>8192:raise ValueError('MQTT packet limit exceeded')
            if not digit&128:break
            multiplier*=128
        else:raise ValueError('Malformed MQTT remaining length')
        body=self.read(length);self.last_io=time.monotonic()
        if header==0xd0:
            if body:raise ValueError('Malformed MQTT PINGRESP')
            self.ping_at=0
        if header>>4==3:
            qos=(header>>1)&3
            if qos not in {0,1} or len(body)<2:raise ValueError('Unsupported notification QoS')
            size=int.from_bytes(body[:2],'big');end=2+size
            if not size or end+(2 if qos else 0)>len(body):raise ValueError('Malformed MQTT topic')
            topic=body[2:end].decode('utf-8')
            if qos:
                identity=body[end:end+2]
                if identity==b'\x00\x00':raise ValueError('Invalid MQTT packet identity')
                end+=2;self.send(0x40,identity)
            if len(self.messages)>=16:self.messages.pop(0)
            self.messages.append((topic,body[end:]))
        return header,body
    def next_id(self):
        self.sequence=self.sequence%65535+1;return struct.pack('!H',self.sequence)
    def wait_ack(self,kind,identity):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            header,body=self.receive()
            if header==kind and body[:2]==identity:
                if kind==0x90 and (len(body)!=3 or body[2] not in {0,1}):raise OSError('MQTT subscription denied')
                if kind==0x40 and len(body)!=2:raise ValueError('Malformed MQTT PUBACK')
                return
        raise TimeoutError('MQTT acknowledgment missing')
    def subscribe(self,topic):
        identity=self.next_id();self.send(0x82,identity+string(topic)+b'\x01');self.wait_ack(0x90,identity)
    def publish(self,topic,payload):
        if '+' in topic or '#' in topic:raise ValueError('Publishing wildcard topics is forbidden')
        identity=self.next_id();self.send(0x32,string(topic)+identity+payload);self.wait_ack(0x40,identity)
    def poll(self,timeout=1):
        if self.messages:return self.messages.pop(0)
        now=time.monotonic()
        if self.ping_at and now-self.ping_at>8:raise TimeoutError('MQTT keepalive acknowledgment missing')
        if not self.ping_at and now-self.last_io>15:self.send(0xc0);self.ping_at=now
        connection=self.socket
        if connection is None:raise OSError('MQTT disconnected')
        if connection.pending() or select.select([connection],[],[],timeout)[0]:self.receive()
        return self.messages.pop(0) if self.messages else None


class NodeNotifications:
    """Wake the HTTPS poller; never execute tasks based on MQTT payloads."""
    def __init__(self,configuration,wake):
        self.configuration=configuration;self.wake=wake;self.stop=threading.Event();self.online=False;self.error='not_configured';self.client=None
        self.thread=threading.Thread(target=self.loop,name='mqtt-notifications',daemon=True);self.thread.start()
    def loop(self):
        backoff=1
        while not self.stop.is_set():
            try:
                options=self.configuration()
                if not options:self.stop.wait(1);continue
                self.client=MQTT(**options);self.client.connect();topic=f"ab/v1/nodes/{options['username']}/down/events"
                self.client.subscribe(topic);self.online=True;self.error='';backoff=1
                while not self.stop.is_set():
                    message=self.client.poll()
                    if message and message[0]==topic and len(message[1])<=1024:
                        value=json.loads(message[1])
                        if isinstance(value,dict) and value.get('schema_version')==1:self.wake.set()
            except (OSError,ValueError,TypeError):self.online=False;self.error='mqtt_unavailable'
            finally:
                if self.client:self.client.close()
                self.online=False
            self.stop.wait(backoff);backoff=min(30,backoff*2)
    def close(self):
        self.stop.set()
        if self.client:self.client.close()
        self.thread.join(timeout=10)
