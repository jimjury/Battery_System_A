import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from desktop_a.service import AService
from control_center.api import CenterAPI
from server_b.service import BService
from sdk import ABClient,APIError
from test_platform_v3 import Running,wait_for


class DesktopATests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.c=Running(CenterAPI(self.root/'C',admin_token='test-center-admin'))
        self.b=Running(BService(self.root/'B',admin_token='test-b-admin'))
        self.b.service.remote.configure(self.c.url,development_http=True)
        self.b.service.remote.register(self.c.service.center.issue_enrollment('B'),'actual-local-B')
        self.a=Running(AService(self.root/'A',admin_token='test-a-admin'))
        self.client=ABClient(self.a.url,'test-a-admin',target='A',allow_local_http=True)
        self.client.request('PUT','/api/v1/center/config',{'url':self.c.url,'development_http':True,'alias':'actual-PC-A'})
        self.client.request('POST','/api/v1/center/register',{'code':self.c.service.center.issue_enrollment('A')})
        wait_for(lambda:self.b.service.remote.online)
        code=self.b.service.remote.get_client().create_pairing_code()
        self.client.pair(self.b.service.engine.node_id,code['code'])
    def tearDown(self):self.a.close();self.b.close();self.c.close();self.temp.cleanup()
    def test_actual_pc_a_to_c_to_b_and_private_hash_export(self):
        file=self.root/'actual.csv';file.write_bytes(b'new_feature,other\n2,5\n3,8\n')
        uploaded=self.client.upload_file(file)
        target={'node_id':self.b.service.engine.node_id,'deployment_id':'table_scale_two','deployment_version':'1.0.0','parameters':{'columns':['new_feature','other']}}
        group=self.client.submit_group({'dataset':uploaded['file_id']},[target]);task_id=group['tasks'][0]['task_id']
        wait_for(lambda:self.client.get_task(task_id)['state']=='completed',30)
        result=self.client.get_result(task_id)['result'];self.assertEqual([r['prediction'] for r in result['preview']],[15,23])
        artifact=result['artifacts'][0];download=self.client.download_file(artifact['id'],self.root/'export.csv')
        self.assertEqual(download['sha256'],artifact['sha256']);self.assertEqual(hashlib.sha256((self.root/'export.csv').read_bytes()).hexdigest(),artifact['sha256'])
        self.assertNotIn('credential',json.dumps(self.client.request('GET','/api/v1/center/status')))
    def test_local_scope_revocation_and_arbitrary_proxy_denied(self):
        grant=self.client.request('POST','/api/v1/api-keys',{'scopes':['catalog:read']})
        restricted=ABClient(self.a.url,grant['token'],target='A',allow_local_http=True)
        self.assertTrue(restricted.list_nodes()['nodes'])
        with self.assertRaises(APIError):restricted.submit_group({},[])
        with self.assertRaises(APIError):self.client.request('POST','/api/v1/catalog',{})
        self.client.request('POST','/api/v1/api-keys',{'revoke_id':grant['id']})
        with self.assertRaises(APIError):restricted.list_nodes()
    def test_pc_a_identity_persists_and_c_credentials_stay_private(self):
        identity=self.a.service.config['node_id'];self.a.close()
        self.a=Running(AService(self.root/'A',admin_token='test-a-admin'))
        self.assertEqual(self.a.service.config['node_id'],identity)
        self.assertTrue(self.a.service.config['credential'])
        with self.assertRaises(APIError):ABClient(self.a.url,target='A',allow_local_http=True).list_nodes()
    def test_browser_origin_and_csrf_are_required(self):
        import urllib.request,urllib.error
        def send(path,body,headers):
            request=urllib.request.Request(self.a.url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**headers},method='POST')
            return urllib.request.urlopen(request)
        with self.assertRaises(urllib.error.HTTPError) as error:send('/api/session',{'password':'test-a-admin'},{'Origin':'https://untrusted.example'})
        self.assertEqual(error.exception.code,403)
        with send('/api/session',{'password':'test-a-admin'},{}) as reply:cookie=reply.headers['Set-Cookie'].split(';')[0]
        with self.assertRaises(urllib.error.HTTPError) as error:send('/api/v1/api-keys',{'scopes':['catalog:read']},{'Cookie':cookie})
        self.assertEqual(error.exception.code,403)


if __name__=='__main__':unittest.main()
