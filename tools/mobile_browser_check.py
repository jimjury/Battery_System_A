"""Edge UI-only fixtures: never represents a physical phone/ESP32 acceptance test."""
import base64
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
import websocket

ROOT=Path(__file__).resolve().parents[1]

def run():
    output=ROOT/'.local/mobile-browser';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        process=subprocess.Popen(['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
            '--headless','--disable-gpu',f'--remote-debugging-port={port}','--no-first-run',
            '--no-default-browser-check','--user-data-dir='+temporary,'about:blank'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        connection=None;errors=[];calls=[];counter=0
        try:
            deadline=time.monotonic()+25
            while True:
                try:
                    req=urllib.request.Request(f'http://127.0.0.1:{port}/json/new?about:blank',method='PUT')
                    with urllib.request.urlopen(req,timeout=2) as response:page=json.load(response)
                    break
                except OSError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.2)
            connection=websocket.create_connection(page['webSocketDebuggerUrl'],timeout=20,suppress_origin=True)
            def send(method,params=None):
                nonlocal counter
                counter+=1;connection.send(json.dumps({'id':counter,'method':method,'params':params or {}}));return counter
            def intercepted(params):
                request=params['request'];path=urlsplit(request['url']).path;calls.append((request['method'],path))
                status=200;mime='application/json';data={}
                if path in ['/','/platform.js','/style.css','/sha256.js']:
                    name='index.html' if path=='/' else path[1:];body=(ROOT/'web'/name).read_bytes()
                    mime={'html':'text/html','js':'application/javascript','css':'text/css'}[name.rsplit('.',1)[1]]
                else:
                    if path=='/api/session':data={'authenticated':True,'csrf':'fixture-only','read_only':True}
                    elif path=='/api/v1/device/status':data={'device_id':'UI-FIXTURE-NOT-HARDWARE','center':{},'uptime_s':60,'free_heap':100000,'minimum_heap':80000,'firmware':'fixture','ui_version':'fixture'}
                    elif path=='/api/v1/tasks':data={'tasks':[{'task_id':'ui-fixture-task','b_id':'ui-fixture-B','state':'running'}]}
                    elif path=='/api/v1/nodes':data={'nodes':[]}
                    elif path=='/api/v1/pairings':data={'pairings':[]}
                    elif path=='/api/v1/mobile/status':data={'enabled':False,'ssid':'fixture-only','read_only':True}
                    elif path=='/favicon.ico':status=404
                    else:raise AssertionError('Unexpected fixture request '+path)
                    body=json.dumps(data).encode()
                send('Fetch.fulfillRequest',{'requestId':params['requestId'],'responseCode':status,
                    'responseHeaders':[{'name':'Content-Type','value':mime}], 'body':base64.b64encode(body).decode()})
            def command(method,params=None):
                identity=send(method,params)
                while True:
                    response=json.loads(connection.recv())
                    if response.get('method')=='Fetch.requestPaused':intercepted(response['params'])
                    if response.get('method')=='Runtime.exceptionThrown':errors.append(response)
                    if response.get('id')==identity:
                        if 'error' in response:raise RuntimeError(response['error'])
                        return response.get('result',{})
            def evaluate(source):
                result=command('Runtime.evaluate',{'expression':source,'awaitPromise':True,'returnByValue':True})
                if result.get('exceptionDetails'):raise AssertionError(result['exceptionDetails'])
                return result.get('result',{}).get('value')
            command('Runtime.enable');command('Page.enable')
            command('Fetch.enable',{'patterns':[{'urlPattern':'http://192.168.*/*'}]})
            command('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
            command('Page.navigate',{'url':'http://192.168.8.1/'})
            deadline=time.monotonic()+15
            while not evaluate("typeof phoneView!=='undefined'&&document.readyState==='complete'&&S.csrf.length>0"):
                if time.monotonic()>deadline:raise AssertionError('phone UI boot timed out')
                time.sleep(.1)
            assert evaluate('phoneView===true')
            assert evaluate("['network','center','submit','updates','api'].every(p=>document.querySelector('[data-page='+p+']').hidden)")
            assert evaluate("['pair-form','build-task','submit-task'].every(id=>document.getElementById(id).hidden)")
            evaluate("section('updates')")
            assert evaluate("!document.getElementById('page-overview').hidden")
            evaluate("(async()=>{section('tasks');await refreshTasks()})()")
            assert evaluate("document.querySelectorAll('#tasks-table button').length===1")
            evaluate("renderPairs([{a_id:'fixtureA',b_id:'fixtureB',policy:{scopes:[]},revoked:false}])")
            assert evaluate("document.querySelectorAll('#pairings-table button').length===0")
            evaluate("section('overview')")
            assert evaluate('document.documentElement.scrollWidth<=innerWidth'), 'phone page overflows viewport'
            shot=command('Page.captureScreenshot',{'format':'png'})
            (output/'phone-fixture.png').write_bytes(base64.b64decode(shot['data']))
            assert not errors,errors
            assert all(method=='GET' for method,path in calls),calls
            phone_calls=list(calls);calls.clear()
            command('Page.navigate',{'url':'http://192.168.7.1/'})
            deadline=time.monotonic()+15
            while not evaluate("typeof phoneView!=='undefined'&&document.readyState==='complete'&&S.csrf.length>0&&!phoneView"):
                if time.monotonic()>deadline:raise AssertionError('USB UI boot timed out')
                time.sleep(.1)
            assert evaluate("typeof document.getElementById('mobile-enable').onclick==='function'")
            assert evaluate("!document.querySelector('[data-page=updates]').hidden")
            assert not errors,errors
            report={'test_type':'Edge with intercepted UI fixtures only','physical_phone_test':False,
                'physical_esp32_test':False,'viewport':[390,844],'phone_read_only_controls':True,
                'phone_task_cancel_hidden':True,'phone_pair_revoke_hidden':True,'phone_navigation_guard':True,
                'phone_no_horizontal_overflow':True,'phone_requests':phone_calls,'usb_management_controls':True,
                'javascript_exceptions':len(errors)}
            (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))
        finally:
            if connection:connection.close()
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()

if __name__=='__main__':run()
