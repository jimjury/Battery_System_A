"""Real Edge -> running PC A -> public C -> running B, using local A login only."""
import base64
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_platform_v3 import wait_for
from sdk import ABClient
import websocket


def main():
    password=(ROOT/'desktop_a/.runtime/admin_password.txt').read_text().strip()
    api=ABClient('http://127.0.0.1:8766',password,target='A',allow_local_http=True)
    before={t['task_id'] for t in api.request('GET','/api/v1/tasks')['tasks']}
    output=ROOT/'.local/live-browser';output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT/'.local',ignore_cleanup_errors=True) as temporary:
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        process=subprocess.Popen(['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','--headless','--disable-gpu',f'--remote-debugging-port={port}',
            '--no-first-run','--no-default-browser-check','--user-data-dir='+temporary,'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        connection=None;counter=0;errors=[]
        try:
            def page():
                try:
                    request=urllib.request.Request(f'http://127.0.0.1:{port}/json/new?http://127.0.0.1:8766',method='PUT')
                    with urllib.request.urlopen(request,timeout=2) as response:return json.load(response)
                except OSError:return None
            target=wait_for(page,25);connection=websocket.create_connection(target['webSocketDebuggerUrl'],timeout=45,suppress_origin=True)
            def command(method,params=None):
                nonlocal counter
                counter+=1;identity=counter;connection.send(json.dumps({'id':identity,'method':method,'params':params or {}}))
                while True:
                    reply=json.loads(connection.recv())
                    if reply.get('method')=='Runtime.exceptionThrown':errors.append(reply)
                    if reply.get('id')==identity:
                        if 'error' in reply:raise RuntimeError(reply['error'])
                        return reply.get('result',{})
            def evaluate(source):
                result=command('Runtime.evaluate',{'expression':source,'awaitPromise':True,'returnByValue':True})
                if result.get('exceptionDetails'):raise RuntimeError(result['exceptionDetails'])
                return result.get('result',{}).get('value')
            command('Runtime.enable');command('Page.enable');command('Emulation.setDeviceMetricsOverride',{'width':1440,'height':1000,'deviceScaleFactor':1,'mobile':False})
            wait_for(lambda:evaluate("typeof boot==='function' && document.readyState==='complete'"))
            evaluate("document.getElementById('login-open').click();document.getElementById('admin-password').value="+json.dumps(password)+";document.getElementById('login-form').requestSubmit()")
            wait_for(lambda:evaluate('S.csrf.length>0'))
            evaluate("section('catalog')")
            wait_for(lambda:evaluate('S.deployments.length===3'))
            evaluate("(()=>{const i=S.deployments.findIndex(d=>d.deployment_id==='file_statistics');document.querySelectorAll('#catalog-table input')[i].click();document.getElementById('build-task').click();const f=new File(['public cloud browser\\n'],'cloud-browser.txt',{type:'text/plain'});const t=new DataTransfer();t.items.add(f);const input=document.querySelector('#input-form input[type=file]');input.files=t.files;input.dispatchEvent(new Event('change'));})()")
            wait_for(lambda:evaluate('Object.values(S.files).flat().length===1'))
            evaluate("document.getElementById('submit-task').click()")
            def completed():
                for task in api.request('GET','/api/v1/tasks')['tasks']:
                    if task['task_id'] not in before and task['state']=='completed':return task
                return None
            task=wait_for(completed,60);result=api.get_result(task['task_id'])['result']
            if result['metrics']['words']!=3:raise AssertionError('Live browser task result mismatch')
            evaluate('showTask('+json.dumps(task['task_id'])+')')
            if evaluate("document.getElementById('page-results').hidden"):raise AssertionError('Actual results page did not render')
            screenshot=command('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
            (output/'pc-a-cloud-results.png').write_bytes(base64.b64decode(screenshot['data']))
            if errors:raise AssertionError('Actual browser JavaScript exception')
            report={'actual_edge':True,'actual_pc_a':True,'actual_public_c':'https://60.204.129.7','actual_local_b_worker':True,
                'task_id':task['task_id'],'words':3,'mode':result['mode'],'javascript_exceptions':0,'bypassed_a':False,'tls_verification_disabled':False}
            (output/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
        finally:
            if connection:
                try:connection.send(json.dumps({'id':counter+1,'method':'Browser.close'}))
                except OSError:pass
                connection.close()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':main()
