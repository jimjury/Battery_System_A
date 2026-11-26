"""Exercise the real portable B UI using an existing Edge installation and CDP."""
import base64
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_platform_v3 import Running,wait_for
from server_b.service import BService
from desktop_a.service import AService
from control_center.api import CenterAPI
import websocket


def run():
    edge=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():raise RuntimeError('Existing Edge is required; no browser will be installed')
    output=ROOT/'.local/browser-check-pc';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        center=Running(CenterAPI(Path(temporary)/'center',admin_token='center-test-password'))
        worker=Running(BService(Path(temporary)/'worker',admin_token='worker-test-password'))
        worker.service.remote.configure(center.url,development_http=True)
        worker.service.remote.register(center.service.center.issue_enrollment('B'),'browser-worker')
        service=Running(AService(Path(temporary)/'runtime',admin_token='browser-test-only-password'))
        service.service.configure({'url':center.url,'alias':'browser-PC-A','development_http':True})
        service.service.register({'code':center.service.center.issue_enrollment('A')})
        wait_for(lambda:worker.service.remote.online)
        code=worker.service.remote.get_client().create_pairing_code()
        service.service.get_client().pair(worker.service.engine.node_id,code['code'])
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        process=subprocess.Popen([str(edge),'--headless','--disable-gpu',f'--remote-debugging-port={port}',
            '--no-first-run','--no-default-browser-check','--user-data-dir='+str(Path(temporary)/'edge'),'about:blank'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        connection=None;events=[];counter=0
        try:
            def open_page():
                try:
                    req=urllib.request.Request(f'http://127.0.0.1:{port}/json/new?{service.url}',method='PUT')
                    with urllib.request.urlopen(req,timeout=2) as response:return json.load(response)
                except OSError:return None
            page=wait_for(open_page,25)
            connection=websocket.create_connection(page['webSocketDebuggerUrl'],timeout=30,suppress_origin=True)
            def command(method,params=None):
                nonlocal counter
                counter+=1;identity=counter;connection.send(json.dumps({'id':identity,'method':method,'params':params or {}}))
                while True:
                    response=json.loads(connection.recv())
                    if response.get('id')==identity:
                        if 'error' in response:raise RuntimeError(response['error'])
                        return response.get('result',{})
                    if response.get('method')=='Runtime.exceptionThrown':events.append(response)
            def evaluate(source):
                result=command('Runtime.evaluate',{'expression':source,'awaitPromise':True,'returnByValue':True})
                if result.get('exceptionDetails'):raise RuntimeError(result['exceptionDetails'])
                return result.get('result',{}).get('value')
            command('Runtime.enable');command('Page.enable')
            wait_for(lambda:evaluate("typeof boot==='function' && document.readyState==='complete'"))
            evaluate("document.getElementById('login-open').click();document.getElementById('admin-password').value='browser-test-only-password';document.getElementById('login-form').requestSubmit()")
            wait_for(lambda:evaluate("S.csrf.length>0"))
            evaluate("section('catalog')")
            wait_for(lambda:evaluate("document.querySelectorAll('#catalog-table tbody tr').length===3"))
            evaluate("(()=>{const index=S.deployments.findIndex(d=>d.deployment_id==='file_statistics');const box=document.querySelectorAll('#catalog-table input')[index];box.click();document.getElementById('build-task').click();const file=new File(['actual browser words\\n'],'browser.txt',{type:'text/plain'});const transfer=new DataTransfer();transfer.items.add(file);const input=document.querySelector('#input-form input[type=file]');input.files=transfer.files;input.dispatchEvent(new Event('change'));})()")
            wait_for(lambda:evaluate("Object.values(S.files).flat().length===1"))
            # Drop the first task response after the actual server persisted it.
            evaluate("globalThis.testOriginalFetch=fetch;globalThis.testDropTaskReply=true;globalThis.fetch=async(...args)=>{const response=await testOriginalFetch(...args);if(testDropTaskReply&&args[0]==='/api/v1/task-groups'&&args[1]?.method==='POST'){testDropTaskReply=false;throw Error('test-only lost durable task reply');}return response;};document.getElementById('submit-task').click()")
            wait_for(lambda:evaluate("testDropTaskReply===false && S.busy===false"))
            evaluate("document.getElementById('submit-task').click()")
            def completed():
                tasks=worker.service.engine.list_tasks('',True)
                return tasks[0] if tasks and tasks[0]['state']=='completed' else None
            try:task=wait_for(completed,25)
            except AssertionError:
                (output/'failure.json').write_text(json.dumps({'browser':evaluate('({logs:S.logs,busy:S.busy,selected:S.selected})'),
                    'tasks':worker.service.engine.list_tasks('',True),'exceptions':events},indent=2),encoding='utf-8')
                raise
            if task['result']['metrics']['words']!=3:raise AssertionError('Browser input did not reach actual worker')
            wait_for(lambda:evaluate("S.busy===false"))
            if len(worker.service.engine.list_tasks('',True))!=1:raise AssertionError('Lost task reply retry created duplicate inference')
            evaluate('showTask('+json.dumps(task['task_id'])+')')
            if not evaluate("document.getElementById('page-results').hidden===false"):raise AssertionError('Results view failed')
            screenshot=command('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
            (output/'pc-a-results.png').write_bytes(base64.b64decode(screenshot['data']))
            if events:raise AssertionError('Browser raised JavaScript exceptions')
            receipt={'actual_browser':'existing Microsoft Edge','actual_service':'PC A HTTP to C HTTP to B subprocess',
                'steps':['login','dynamic catalog','select deployment','File upload','real subprocess inference','results view'],
                'task_id':task['task_id'],'words':3,'lost_task_reply_retry_single_task':True,'javascript_exceptions':len(events),'physical_esp32_test':False}
            (output/'report.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8');print(json.dumps(receipt))
        finally:
            if connection:connection.close()
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            service.close();worker.close();center.close()


if __name__=='__main__':run()
