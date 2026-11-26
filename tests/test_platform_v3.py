import csv
import hashlib
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest

from ab_platform.contracts import Fault,new_id
from ab_platform.http_service import Server
from control_center.api import CenterAPI
from sdk import ABClient,APIError
from server_b.engine import Engine
from server_b.environments import probe,select
from server_b.service import BService


class Running:
    def __init__(self,service):
        self.service=service;self.server=Server(('127.0.0.1',0),service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join();self.service.close()


def wait_for(function,timeout=20):
    deadline=time.monotonic()+timeout;last=None
    while time.monotonic()<deadline:
        value=function();last=value
        if value:return value
        time.sleep(.05)
    raise AssertionError('Timed out waiting for actual service state: '+str(last))


class PortableEngineTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.engine=Engine(Path(self.temp.name)/'runtime')
    def tearDown(self):self.engine.close();self.temp.cleanup()
    def upload(self,data=b'alpha,beta\n1,2\n3,4\n',name='sample.csv',owner='local:test'):
        file=Path(self.temp.name)/name;file.write_bytes(data);return self.engine.import_file(owner,file)['file_id']
    def finished(self,task):
        return wait_for(lambda:self.engine.task('local:test',task['task_id']) if self.engine.task('local:test',task['task_id'])['state'] in {'completed','failed','cancelled'} else None)
    def test_two_variants_real_subprocess_distinct_expected_outputs(self):
        file=self.upload();tasks=[]
        for dep in ['table_scale_one','table_scale_two']:
            tasks.append(self.engine.submit('local:test',dep,'1.0.0',{'dataset':file},{'columns':['alpha','beta']}))
        first,second=[self.finished(task) for task in tasks]
        self.assertEqual(first['state'],'completed');self.assertEqual(second['state'],'completed')
        self.assertEqual([row['prediction'] for row in first['result']['preview']],[3,7])
        self.assertEqual([row['prediction'] for row in second['result']['preview']],[7,15])
        self.assertNotEqual(first['snapshot']['deployment']['weights_sha256'],second['snapshot']['deployment']['weights_sha256'])
        self.assertEqual(first['result']['environment']['executable'],self.engine.environments[0]['executable'])
    def test_nonbattery_text_file_actual_counts(self):
        file=self.upload('Hello world\n第二行\n'.encode(),name='words.txt')
        task=self.finished(self.engine.submit('local:test','file_statistics','1.0.0',{'document':file},{}))
        self.assertEqual(task['state'],'completed');self.assertEqual(task['result']['metrics']['words'],3)
        self.assertEqual(task['result']['metrics']['lines'],2);self.assertEqual(task['result']['mode'],'demo')
    def test_no_nan_imputation_or_missing_column_fallback(self):
        file=self.upload(b'alpha,beta\nNaN,2\n')
        task=self.finished(self.engine.submit('local:test','table_scale_one','1.0.0',{'dataset':file},{'columns':['alpha']}))
        self.assertEqual(task['state'],'failed');self.assertIsNone(task['result'])
    def test_private_local_api_ownership(self):
        file=self.upload()
        with self.assertRaises(Fault):self.engine.submit('local:other','table_scale_one','1.0.0',{'dataset':file},{'columns':['alpha']})
    def test_manual_probe_and_device_contract(self):
        import sys
        report=probe(sys.executable);self.assertEqual(report['python_version'].split('.')[0],'3')
        with self.assertRaises(Fault):select([{'available':True,**report}],{'python':'>=99.0'},supported=['cpu'])
        with self.assertRaises(Fault):select([{'available':True,**report}],{'python':'>=3.10'},device='cuda',supported=['cpu'])
    def test_explicit_trust_and_no_weight_code_upload(self):
        with self.assertRaises(Fault):self.engine.registry.import_package(Path(__file__).parents[1]/'server_b/models/demo_table')
    def test_queued_cancellation_terminal(self):
        self.engine.close();self.engine=Engine(Path(self.temp.name)/'runtime',start_scheduler=False)
        file=self.upload();task=self.engine.submit('local:test','table_scale_one','1.0.0',{'dataset':file},{'columns':['alpha']})
        result=self.engine.cancel('local:test',task['task_id']);self.assertEqual(result['state'],'cancelled')
        self.engine._execute(task['task_id']);self.assertEqual(self.engine.task('local:test',task['task_id'])['state'],'cancelled')
    def test_deployment_disable_survives_reload_and_blocks_new_tasks(self):
        file=self.upload()
        self.engine.registry.set_enabled('table_scale_one','1.0.0',False);self.engine.registry.load()
        with self.assertRaises(Fault):self.engine.submit('local:test','table_scale_one','1.0.0',{'dataset':file},{'columns':['alpha']})
        self.engine.registry.set_enabled('table_scale_one','1.0.0',True)
        task=self.finished(self.engine.submit('local:test','table_scale_one','1.0.0',{'dataset':file},{'columns':['alpha']}))
        self.assertEqual(task['state'],'completed');self.assertEqual(task['result']['preview'][0]['prediction'],1)


class RealHttpTopologyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.c=Running(CenterAPI(self.root/'C',admin_token='center-admin-test-credential'))
        self.b1=Running(BService(self.root/'B1',admin_token='b-one-test-administrator'))
        self.b2=Running(BService(self.root/'B2',admin_token='b-two-test-administrator'))
        for label,b in [('B1',self.b1),('B2',self.b2)]:
            b.service.remote.configure(self.c.url,development_http=True)
            b.service.remote.register(self.c.service.center.issue_enrollment('B'),label)
        self.a1=ABClient(self.c.url,allow_local_http=True);self.a1.enroll(self.c.service.center.issue_enrollment('A'),'A','simulated-A1');self.a1.heartbeat()
        self.a2=ABClient(self.c.url,allow_local_http=True);self.a2.enroll(self.c.service.center.issue_enrollment('A'),'A','simulated-A2');self.a2.heartbeat()
        wait_for(lambda:self.b1.service.remote.online and self.b2.service.remote.online)
        self.pairs={}
        for a,name,b in [(self.a1,'A1B1',self.b1),(self.a1,'A1B2',self.b2),(self.a2,'A2B1',self.b1)]:
            code=b.service.remote.get_client().create_pairing_code(max_active=4)
            self.pairs[name]=a.pair(b.service.engine.node_id,code['code'])
    def tearDown(self):
        self.b1.close();self.b2.close();self.c.close();self.temp.cleanup()
    def test_2a_2b_actual_http_and_workers_result_hashes(self):
        file=self.root/'arbitrary.csv';file.write_text('arbitrary_a,arbitrary_b\n1,2\n3,4\n',encoding='utf-8')
        uploaded=self.a1.upload_file(file)
        targets=[{'node_id':self.b1.service.engine.node_id,'deployment_id':'table_scale_one','deployment_version':'1.0.0','parameters':{'columns':['arbitrary_a','arbitrary_b']}},
                 {'node_id':self.b2.service.engine.node_id,'deployment_id':'table_scale_two','deployment_version':'1.0.0','parameters':{'columns':['arbitrary_a','arbitrary_b']}}]
        group=self.a1.submit_group({'dataset':uploaded['file_id']},targets)
        second_file=self.root/'other-origin.csv';second_file.write_text('other_feature\n11\n13\n',encoding='utf-8')
        second_upload=self.a2.upload_file(second_file)
        second_group=self.a2.submit_group({'dataset':second_upload['file_id']},[{'node_id':self.b1.service.engine.node_id,
            'deployment_id':'table_scale_one','deployment_version':'1.0.0','parameters':{'columns':['other_feature']}}])
        ids=[task['task_id'] for task in group['tasks']]
        def completed():
            tasks=[self.a1.get_task(task_id) for task_id in ids]
            return tasks if all(task['state']=='completed' for task in tasks) else None
        tasks=wait_for(completed,30)
        second_id=second_group['tasks'][0]['task_id']
        second=wait_for(lambda:self.a2.get_result(second_id) if self.a2.get_task(second_id)['state']=='completed' else None,30)
        self.assertEqual([row['prediction'] for row in second['result']['preview']],[11,13])
        with self.assertRaises(APIError):self.a1.get_task(second_id)
        for task in tasks:
            result=self.a1.get_result(task['task_id'])['result'];artifact=result['artifacts'][0]
            dest=self.root/(task['task_id']+'.csv');receipt=self.a1.download_file(artifact['id'],dest)
            self.assertEqual(receipt['sha256'],artifact['sha256'])
            with dest.open() as stream:values=[float(row['prediction']) for row in csv.DictReader(stream)]
            self.assertEqual(values,[3,7] if task['b_id']==self.b1.service.engine.node_id else [7,15])
            with self.assertRaises(APIError):self.a2.get_task(task['task_id'])
        self.a1.revoke_pairing(self.pairs['A1B1']['pairing_id'])
        with self.assertRaises(APIError):self.a1.list_deployments(self.b1.service.engine.node_id)
        self.assertTrue(self.a1.list_deployments(self.b2.service.engine.node_id)['deployments'])
        self.assertTrue(self.a2.list_deployments(self.b1.service.engine.node_id)['deployments'])
    def test_b_local_http_inference_without_center(self):
        self.b1.service.remote.close();self.b1.service.remote.config['credential']='';self.b1.service.remote.online=False
        client=ABClient(self.b1.url,'b-one-test-administrator',target='B',allow_local_http=True)
        file=self.root/'local.txt';file.write_text('one two three\n',encoding='utf-8')
        uploaded=client.upload_file(file);task=client.submit_local('file_statistics','1.0.0',{'document':uploaded['file_id']},{})
        result=wait_for(lambda:client.get_result(task['task_id']) if client.get_task(task['task_id'])['state']=='completed' else None)
        self.assertEqual(result['result']['metrics']['words'],3)
        self.assertTrue(client.request('GET','/health')['local_inference_independent'])


if __name__=='__main__':unittest.main()
