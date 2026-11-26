"""Bounded HTTP(S) hosting shared by local B and independent C.

Public listeners require TLS and an explicit origin. Browser sessions and API
credentials are separate; no wildcard CORS or arbitrary URL proxy exists.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import http.server
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import secrets
import socketserver
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .contracts import Fault, canonical, identifier, new_id, strict_json


@dataclass
class Response:
    body: object
    status: int = 200
    headers: dict = field(default_factory=dict)
    binary: bool = False


class Sessions:
    def __init__(self, secret):
        self.hash = hashlib.sha256(secret.encode()).hexdigest()
        self.values = {}; self.failures = {}; self.lock = threading.RLock()

    def login(self, password, peer):
        if not isinstance(password, str) or len(password) > 256: raise Fault('login_denied','Invalid administrator credential',401)
        now=time.monotonic()
        with self.lock:
            if self.failures.get(peer,0)>now: raise Fault('login_rate_limited','Retry in three seconds',429)
            self.failures[peer]=now+3
            if not hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(),self.hash):
                raise Fault('login_denied','Invalid administrator credential',401)
            self.values={k:v for k,v in self.values.items() if v['expires']>now}
            if len(self.values)>=16:self.values.pop(next(iter(self.values)))
            token=secrets.token_urlsafe(32);csrf=secrets.token_hex(32)
            self.values[token]={'csrf':csrf,'expires':now+3600}
            return token,csrf

    def authenticate(self, headers, write=False):
        token=None
        for item in headers.get('Cookie','').split(';'):
            if item.strip().startswith('ab_session='):token=item.strip().split('=',1)[1]
        with self.lock:
            session=self.values.get(token)
            if not session or session['expires']<time.monotonic():raise Fault('login_required','Administrator session required',401)
            if write and not hmac.compare_digest(headers.get('X-CSRF-Token',''),session['csrf']):
                raise Fault('csrf_denied','CSRF token is missing or invalid',403)
            return session


class Server(socketserver.ThreadingMixIn,http.server.HTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,service,origin=None):
        self.service=service;self.slots=threading.BoundedSemaphore(12);self.tls=False
        self.origin=origin;self.hosts={'127.0.0.1','localhost','::1'}
        if origin:self.hosts.add(urlsplit(origin).hostname)
        super().__init__(address,Handler)
    def process_request(self,request,client_address):
        request.settimeout(30)
        if not self.slots.acquire(False):self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,*args):
        try:super().process_request_thread(*args)
        finally:self.slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version='ABPlatform/3.0'
    def log_message(self,*args):pass
    def do_GET(self):self.handle_request()
    def do_POST(self):self.handle_request()
    def do_PUT(self):self.handle_request()
    def do_DELETE(self):self.handle_request()
    def handle_request(self):
        request_id=self.headers.get('X-Request-ID','')
        if not identifier(request_id):request_id=new_id()
        try:
            if len(self.path)>2048:raise Fault('url_limit','Request path too long',414)
            host=urlsplit('http://'+self.headers.get('Host','')).hostname
            if host not in self.server.hosts:raise Fault('host_denied','Invalid management Host',403)
            origin=self.headers.get('Origin')
            if origin and origin.rstrip('/')!=f"{'https' if self.server.tls else 'http'}://{self.headers.get('Host')}":
                raise Fault('origin_denied','Cross-origin browser requests are forbidden',403)
            if origin and self.headers.get('Authorization'):raise Fault('browser_bearer_denied','Browsers use the local session, not device bearer credentials',403)
            parsed=urlsplit(self.path);query=parse_qs(parsed.query)
            length=0
            if self.command in {'POST','PUT','DELETE'}:
                if self.headers.get('Transfer-Encoding'):raise Fault('length_required','Content-Length required',411)
                try:length=int(self.headers.get('Content-Length','0'))
                except ValueError:raise Fault('content_length','Invalid content length',400)
                if not 0<=length<=256*1024:raise Fault('body_limit','Request body exceeds host limit',413)
            raw=self.rfile.read(length)
            if len(raw)!=length:raise Fault('truncated_body','Request body incomplete',400)
            response=self.server.service.dispatch(self.command,parsed.path,query,self.headers,raw,self.client_address[0])
            if not isinstance(response,Response):response=Response(response)
            self.respond(response,request_id)
        except Fault as fault:
            self.respond(Response({'code':fault.code,'message':fault.message},fault.status),request_id)
        except (BrokenPipeError,ConnectionResetError,TimeoutError):self.close_connection=True
        except Exception as error:
            logging.getLogger('ab_http').error('request=%s failure_type=%s',request_id,type(error).__name__)
            self.respond(Response({'code':'internal_error','message':'Request failed; no successful ACK was issued'},500),request_id)
    def respond(self,response,request_id):
        headers=dict(response.headers)
        if not response.binary:
            body=response.body
            if isinstance(body,dict):body={'request_id':request_id,'code':'ok' if response.status<400 else 'error','message':'OK' if response.status<400 else 'Request failed',**body}
            data=canonical(body);headers.setdefault('Content-Type','application/json; charset=utf-8');headers['Content-Length']=str(len(data))
        elif isinstance(response.body,bytes):data=response.body;headers['Content-Length']=str(len(data))
        else:data=None
        self.send_response(response.status)
        headers.setdefault('Cache-Control','no-store');headers.setdefault('X-Content-Type-Options','nosniff');headers['X-Request-ID']=request_id
        if self.server.tls and 'Set-Cookie' in headers:headers['Set-Cookie']+='; Secure'
        for key,value in headers.items():self.send_header(key,str(value))
        if 'Content-Length' not in headers:self.send_header('Connection','close');self.close_connection=True
        self.end_headers()
        if data is not None:self.wfile.write(data)
        else:
            try:
                for chunk in response.body:self.wfile.write(chunk)
            except Fault:self.close_connection=True


def json_body(headers,raw,limit=16384):
    if len(raw)>limit:raise Fault('json_limit','JSON body exceeds endpoint limit',413)
    if raw and headers.get('Content-Type','').split(';')[0]!='application/json':raise Fault('content_type','Use application/json',415)
    value=strict_json(raw) if raw else {}
    if not isinstance(value,dict):raise Fault('json_object','JSON object required',400)
    return value


def serve(service,host,port,*,cert=None,key=None,origin=None):
    if host not in {'127.0.0.1','localhost','::1'} and not (cert and key and origin and origin.startswith('https://')):
        raise Fault('tls_required','Public listeners require TLS and an explicit HTTPS origin')
    server=Server((host,port),service,origin)
    if cert or key:
        if not cert or not key:raise Fault('tls_config','Certificate and key must both be supplied')
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert,key);server.socket=context.wrap_socket(server.socket,server_side=True);server.tls=True
    return server


def configure_log(root):
    path=Path(root)/'logs';path.mkdir(parents=True,exist_ok=True)
    logger=logging.getLogger('ab_http');logger.setLevel(logging.INFO)
    if not logger.handlers:logger.addHandler(RotatingFileHandler(path/'service.log',maxBytes=1024*1024,backupCount=3,encoding='utf-8'))
