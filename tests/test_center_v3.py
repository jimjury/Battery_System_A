import hashlib
import tempfile
import unittest
from pathlib import Path

from ab_platform.contracts import Fault, new_id
from control_center.core import Center


def deployment(name='demo_table', variant='scale1'):
    return {'schema_version':1,'deployment_id':name,'deployment_version':'1.0','model_id':'table_transform','model_version':'1.0',
            'variant_id':variant,'variant_version':'1.0','task_type':'table_regression','title':'Deterministic demo',
            'inputs':[{'id':'dataset','kind':'file','formats':['csv'],'required':True}],
            'parameters_schema':{'type':'object','properties':{},'additionalProperties':False},
            'outputs':[{'id':'predictions','kind':'table','formats':['csv','json']}],
            'capabilities':{'batch':True,'cancel':True},'availability':'ready','mode':'demo'}


class CenterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.now=1000000
        self.c=Center(self.temp.name,clock=lambda:self.now)
        self.nodes={}; self.creds={}; self.instances={}; self.epochs={}
        for alias,role in [('A1','A'),('A2','A'),('B1','B'),('B2','B')]:
            record=self.c.enroll(self.c.issue_enrollment(role),role,new_id(),alias)
            self.creds[alias]=record['credential']; self.nodes[alias]=self.c.authenticate(record['credential'])
            self.instances[alias]=new_id(); h=self.c.heartbeat(self.nodes[alias],self.instances[alias]); self.epochs[alias]=h['session_epoch']
        for b in ['B1','B2']: self.c.publish_catalog(self.nodes[b],[deployment()])
        self.pairings={}
        for a,b in [('A1','B1'),('A1','B2'),('A2','B1')]: self.pairings[a,b]=self.pair(a,b)

    def tearDown(self): self.c.close(); self.temp.cleanup()

    def pair(self,a,b,**kwargs):
        code=self.c.pair_code(self.nodes[b],**kwargs)
        return self.c.pair(self.nodes[a],self.nodes[b].node_id,code['code'])

    def upload(self,a='A1',data=b'x,y\n1,2\n3,4\n'):
        p=self.nodes[a]; sha=hashlib.sha256(data).hexdigest(); row=self.c.create_upload(p,'sample.csv',len(data),sha)
        if data:self.c.put_part(p,row['upload_id'],0,data,sha)
        self.c.finalize(p,row['upload_id']); return row['file_id']

    def group(self,a='A1',bs=('B1',),key=None,file=None):
        file=file or self.upload(a)
        return self.c.create_group(self.nodes[a],key or new_id(),{'dataset':file},
            [{'node_id':self.nodes[b].node_id,'deployment_id':'demo_table','deployment_version':'1.0','parameters':{}} for b in bs])

    def claim(self,task,b='B1'):
        return self.c.claim(self.nodes[b],task['task_id'],self.instances[b],self.epochs[b])

    def transition(self,task,state,result=None,b='B1'):
        return self.c.transition(self.nodes[b],task['task_id'],state,self.instances[b],self.epochs[b],task['attempt_id'],task['lease_epoch'],result)

    def test_registration_role_replay_and_forged_identity(self):
        code=self.c.issue_enrollment('A')
        with self.assertRaises(Fault): self.c.enroll(code,'B',new_id(),'rogue')
        self.c.enroll(code,'A',new_id(),'legitimate')
        with self.assertRaises(Fault): self.c.enroll(code,'A',new_id(),'replay')
        with self.assertRaises(Fault): self.c.authenticate('unknown-credential-123456789')

    def test_multi_to_multi_discovery_and_catalog(self):
        a=self.c.nodes(self.nodes['A1'],'B'); b=self.c.nodes(self.nodes['B1'],'A')
        self.assertEqual(sum(n['paired'] for n in a),2); self.assertEqual(len(b),2)
        self.assertEqual(len(self.c.catalog(self.nodes['A1'],self.nodes['B2'].node_id)['deployments']),1)
        with self.assertRaises(Fault): self.c.catalog(self.nodes['A2'],self.nodes['B2'].node_id)

    def test_pairing_code_bound_expiring_single_use(self):
        code=self.c.pair_code(self.nodes['B2'])
        with self.assertRaises(Fault): self.c.pair(self.nodes['A2'],self.nodes['B1'].node_id,code['code'])
        self.c.pair(self.nodes['A2'],self.nodes['B2'].node_id,code['code'])
        with self.assertRaises(Fault): self.c.pair(self.nodes['A2'],self.nodes['B2'].node_id,code['code'])
        self.now+=301
        expired=self.c.issue_enrollment('A',lifetime=1); self.now+=2
        with self.assertRaises(Fault): self.c.enroll(expired,'A',new_id(),'expired')

    def test_revoke_only_one_pair_and_old_task_access(self):
        task=self.group()['tasks'][0]
        self.c.revoke(self.nodes['B1'],self.pairings['A1','B1']['pairing_id'])
        with self.assertRaises(Fault): self.c.get_task(self.nodes['A1'],task['task_id'])
        self.assertTrue(self.c.catalog(self.nodes['A1'],self.nodes['B2'].node_id))
        self.assertTrue(self.c.catalog(self.nodes['A2'],self.nodes['B1'].node_id))

    def test_repair_does_not_resurrect_old_authorization(self):
        task=self.group()['tasks'][0]
        self.c.revoke(self.nodes['A1'],self.pairings['A1','B1']['pairing_id']); self.pair('A1','B1')
        with self.assertRaisesRegex(Fault,'resurrect'): self.c.get_task(self.nodes['A1'],task['task_id'])

    def test_one_upload_multiple_b_children(self):
        file=self.upload(); group=self.group(bs=('B1','B2'),file=file)
        self.assertEqual(len(group['tasks']),2)
        self.assertEqual({t['state'] for t in group['tasks']},{'queued'})
        for b in ('B1','B2'):
            task=next(t for t in group['tasks'] if t['b_id']==self.nodes[b].node_id); self.claim(task,b)
            _,data=self.c.read_file(self.nodes[b],file); self.assertEqual(b''.join(data),b'x,y\n1,2\n3,4\n')

    def test_group_retry_idempotent_and_conflict(self):
        file=self.upload(); key=new_id(); first=self.group(key=key,file=file); second=self.group(key=key,file=file)
        self.assertEqual(first,second)
        with self.assertRaises(Fault): self.group(key=key,file=file,bs=('B2',))

    def test_partial_target_rejection_is_independent(self):
        group=self.group(a='A2',bs=('B1','B2'))
        self.assertEqual(sorted(t['state'] for t in group['tasks']),['queued','rejected'])

    def test_private_files_and_task_object_authorization(self):
        file=self.upload(); task=self.group(file=file)['tasks'][0]
        for p in [self.nodes['A2'],self.nodes['B2'],self.nodes['B1']]:
            with self.assertRaises(Fault): self.c.read_file(p,file)
        with self.assertRaises(Fault): self.c.get_task(self.nodes['A2'],task['task_id'])
        with self.assertRaises(Fault): self.claim(task,'B2')
        with self.assertRaises(Fault): self.c.create_upload(self.nodes['A1'],'../escape.csv',1,'a'*64)

    def test_part_retry_wrong_hash_offset_and_finalize(self):
        data=b'abcdef'; row=self.c.create_upload(self.nodes['A1'],'sample.csv',6,hashlib.sha256(data).hexdigest()); uid=row['upload_id']
        self.c.put_part(self.nodes['A1'],uid,0,b'abc',hashlib.sha256(b'abc').hexdigest())
        retry=self.c.put_part(self.nodes['A1'],uid,0,b'abc',hashlib.sha256(b'abc').hexdigest()); self.assertTrue(retry['duplicate'])
        with self.assertRaises(Fault): self.c.put_part(self.nodes['A1'],uid,0,b'xxx',hashlib.sha256(b'xxx').hexdigest())
        with self.assertRaises(Fault): self.c.put_part(self.nodes['A1'],uid,4,b'ef',hashlib.sha256(b'ef').hexdigest())
        with self.assertRaises(Fault): self.c.finalize(self.nodes['A1'],uid)
        self.c.put_part(self.nodes['A1'],uid,3,b'def',hashlib.sha256(b'def').hexdigest()); self.c.finalize(self.nodes['A1'],uid)
        metadata,chunks=self.c.read_file(self.nodes['A1'],uid,2,3)
        self.assertEqual(metadata['length'],3); self.assertEqual(b''.join(chunks),b'cde')

    def test_fencing_duplicate_claim_and_old_instance(self):
        task=self.claim(self.group()['tasks'][0]); same=self.claim(task)
        self.assertEqual(task['attempt_id'],same['attempt_id'])
        with self.assertRaises(Fault): self.c.heartbeat(self.nodes['B1'],new_id())
        self.now+=61; new_instance=new_id(); self.c.heartbeat(self.nodes['B1'],new_instance)
        with self.assertRaises(Fault): self.transition(task,'running')

    def test_completed_results_immutable_and_cancel_late(self):
        task=self.claim(self.group()['tasks'][0]); self.transition(task,'running'); self.transition(task,'result_staging')
        result={'task_id':task['task_id'],'attempt_id':task['attempt_id'],'mode':'demo','artifacts':[]}
        final=self.transition(task,'completed',result); self.assertEqual(final['state'],'completed')
        self.assertEqual(self.transition(task,'completed',result)['state'],'completed')
        with self.assertRaises(Fault): self.transition(task,'completed',{**result,'mode':'research'})
        self.assertEqual(self.c.cancel(self.nodes['A1'],task['task_id'])['state'],'completed')

    def test_cancellation_requires_worker_confirmation(self):
        task=self.claim(self.group()['tasks'][0]); self.transition(task,'running')
        self.assertEqual(self.c.cancel(self.nodes['A1'],task['task_id'])['state'],'cancel_requested')
        with self.assertRaises(Fault): self.transition(task,'completed',{})
        self.assertEqual(self.transition(task,'cancelled')['state'],'cancelled')

    def test_result_upload_attempt_binding_and_revocation(self):
        task=self.claim(self.group()['tasks'][0]); self.transition(task,'running')
        data=b'result'; sha=hashlib.sha256(data).hexdigest()
        row=self.c.create_upload(self.nodes['B1'],'output.json',len(data),sha,task['task_id'],task['attempt_id'],task['lease_epoch'],self.instances['B1'],self.epochs['B1'])
        self.c.revoke(self.nodes['A1'],self.pairings['A1','B1']['pairing_id'])
        with self.assertRaises(Fault): self.c.put_part(self.nodes['B1'],row['upload_id'],0,data,sha)

    def test_catalog_disable_invalidates_queued_claim(self):
        task=self.group()['tasks'][0]; item=deployment(); item['availability']='disabled'
        self.c.publish_catalog(self.nodes['B1'],[item])
        with self.assertRaises(Fault): self.claim(task)

    def test_scoped_api_keys_and_credential_rotation(self):
        key=self.c.api_key(self.nodes['A1'],['nodes:read']); principal=self.c.authenticate(key['token'])
        self.assertTrue(self.c.nodes(principal,'B'))
        with self.assertRaises(Fault): self.c.catalog(principal,self.nodes['B1'].node_id)
        self.c.revoke_key(self.nodes['A1'],key['id'])
        with self.assertRaises(Fault): self.c.authenticate(key['token'])
        changed=self.c.rotate(self.nodes['A1'])
        with self.assertRaises(Fault): self.c.authenticate(self.creds['A1'])
        self.assertEqual(self.c.authenticate(changed['credential']).node_id,self.nodes['A1'].node_id)

    def test_events_are_filtered_by_node_and_current_pair(self):
        task=self.group()['tasks'][0]
        events=self.c.events(self.nodes['A1']); self.assertTrue(events['events'])
        self.assertFalse(self.c.events(self.nodes['A2'])['events'])
        self.c.revoke(self.nodes['A1'],self.pairings['A1','B1']['pairing_id'])
        self.assertFalse(self.c.events(self.nodes['A1'])['events'])

    def test_backup_and_restart_preserve_identity_and_parts(self):
        data=b'abcdefgh'; upload=self.c.create_upload(self.nodes['A1'],'resume.csv',8,hashlib.sha256(data).hexdigest())
        self.c.put_part(self.nodes['A1'],upload['upload_id'],0,data[:4],hashlib.sha256(data[:4]).hexdigest())
        backup=Path(self.temp.name)/'backup'; self.c.backup(backup)
        restored=Center(backup,clock=lambda:self.now)
        try:
            p=restored.authenticate(self.creds['A1']); self.assertEqual(restored.upload_status(p,upload['upload_id'])['offset'],4)
            restored.put_part(p,upload['upload_id'],4,data[4:],hashlib.sha256(data[4:]).hexdigest()); restored.finalize(p,upload['upload_id'])
        finally: restored.close()


if __name__=='__main__': unittest.main()
