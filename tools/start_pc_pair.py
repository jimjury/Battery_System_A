"""Start the local PC A and B with an existing private C admin bootstrap file.

One-time enrollment/pairing is performed only when needed. Subsequent ordinary
Start_A/Start_B launches reuse their own identities without C admin credentials.
"""
import json
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from ab_platform.http_service import serve,configure_log
from desktop_a.service import AService
from server_b.service import BService
from sdk import ABClient


def main():
    profile=json.loads((ROOT/'client_config/center.json').read_text());ca=str((ROOT/'client_config'/profile['ca_file']).resolve())
    private=ROOT/'.local/cloud-admin-token.txt'
    operator=ABClient(profile['url'],private.read_text().strip(),ca_file=ca) if private.exists() else None
    a=AService(ROOT/'desktop_a/.runtime');b=BService(ROOT/'server_b/.runtime');servers=[]
    try:
        if not b.remote.config.get('credential'):
            if operator is None:raise RuntimeError('One-time C admin bootstrap is missing; use the separate A/B enrollment forms')
            b.remote.configure(profile['url'],ca_file=ca)
            b.remote.register(operator.request('POST','/api/v1/admin/enrollments',{'role':'B'})['code'],'Local PC B')
        if not a.config.get('credential'):
            if operator is None:raise RuntimeError('One-time C admin bootstrap is missing')
            a.configure({'url':profile['url'],'ca_file':ca,'alias':'Local PC A'})
            a.register({'code':operator.request('POST','/api/v1/admin/enrollments',{'role':'A'})['code']})
        deadline=time.monotonic()+80
        while not b.remote.online and time.monotonic()<deadline:time.sleep(.25)
        if not b.remote.online:raise RuntimeError('B has not obtained a live C session; inspect its private runtime logs')
        with a.lock:
            pairs=a.get_client().request('GET','/api/v1/pairings')['pairings']
            if not any(p['b_id']==b.engine.node_id and not p['revoked'] for p in pairs):
                code=b.remote.get_client().create_pairing_code(max_active=4)
                a.get_client().pair(b.engine.node_id,code['code'])
        for service,port in [(a,8766),(b,8765)]:
            configure_log(service.root);server=serve(service,'127.0.0.1',port);servers.append(server)
            threading.Thread(target=server.serve_forever,daemon=True).start()
        receipt={'A':'http://127.0.0.1:8766','B':'http://127.0.0.1:8765','C':profile['url'],
            'a_node_id':a.config['node_id'],'b_node_id':b.engine.node_id,'paired':True,'center_tls_verified':True}
        (ROOT/'.local/running-pc-pair.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt),flush=True)
        while True:time.sleep(1)
    except KeyboardInterrupt:pass
    finally:
        for server in servers:server.shutdown();server.server_close()
        a.close();b.close()


if __name__=='__main__':main()
