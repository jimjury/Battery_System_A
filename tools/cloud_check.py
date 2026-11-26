"""Real PC A -> authorized public HTTPS C -> local B worker verification."""
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_platform_v3 import Running,wait_for
from desktop_a.service import AService
from server_b.service import BService
from sdk import ABClient,APIError
from deploy_center import connect


def main():
    profile=json.loads((ROOT/'.local/cloud-login.json').read_text())
    ssh=connect(profile,ROOT/'.local/known_hosts')
    a=b=None
    try:
        _,stdout,stderr=ssh.exec_command('cat /var/lib/ab-center/admin_token.txt')
        admin=stdout.read().decode().strip()
        if stdout.channel.recv_exit_status() or not admin:raise RuntimeError('Authorized C administrator credential could not be read')
        origin='https://'+profile['host'];ca=str(ROOT/'.local/pki/ca.pem')
        operator=ABClient(origin,admin,ca_file=ca)
        root=ROOT/'.local/cloud-validation';root.mkdir(exist_ok=True)
        b=Running(BService(root/'B',admin_token='cloud-test-local-B-only'))
        b.service.remote.configure(origin,ca_file=ca)
        if not b.service.remote.config.get('credential'):
            code=operator.request('POST','/api/v1/admin/enrollments',{'role':'B'})['code']
            b.service.remote.register(code,'PC-cloud-validation-B')
        a=Running(AService(root/'A',admin_token='cloud-test-local-A-only'))
        local=ABClient(a.url,'cloud-test-local-A-only',target='A',allow_local_http=True)
        local.request('PUT','/api/v1/center/config',{'url':origin,'ca_file':ca,'alias':'PC-cloud-validation-A'})
        if not a.service.config.get('credential'):
            code=operator.request('POST','/api/v1/admin/enrollments',{'role':'A'})['code']
            local.request('POST','/api/v1/center/register',{'code':code})
        wait_for(lambda:b.service.remote.online,80)
        pairing=b.service.remote.get_client().create_pairing_code(max_active=4)
        pair=local.pair(b.service.engine.node_id,pairing['code'])
        file=root/'measured-input.csv';file.write_bytes(b'feature_x,feature_y\n2,5\n3,8\n')
        uploaded=local.upload_file(file)
        target={'node_id':b.service.engine.node_id,'deployment_id':'table_scale_two','deployment_version':'1.0.0','parameters':{'columns':['feature_x','feature_y']}}
        before=time.monotonic();key=str(uuid.uuid4());group=local.submit_group({'dataset':uploaded['file_id']},[target],key)
        duplicate=local.submit_group({'dataset':uploaded['file_id']},[target],key)
        if group['group_id']!=duplicate['group_id']:raise AssertionError('Cloud idempotency failed')
        task_id=group['tasks'][0]['task_id'];wait_for(lambda:local.get_task(task_id)['state']=='completed',90)
        result=local.get_result(task_id)['result']
        if [item['prediction'] for item in result['preview']]!=[15,23]:raise AssertionError('Actual cloud result mismatch')
        artifact=result['artifacts'][0];receipt=local.download_file(artifact['id'],root/'actual-export.csv')
        if receipt['sha256']!=artifact['sha256']:raise AssertionError('Export digest mismatch')
        other=ABClient(origin,ca_file=ca);other.enroll(operator.request('POST','/api/v1/admin/enrollments',{'role':'A'})['code'],'A','unpaired-permission-test')
        for operation in [lambda:other.get_task(task_id),lambda:other.download_file(artifact['id'],root/'forbidden.csv')]:
            try:operation()
            except APIError as error:
                if error.status not in {403,404}:raise
            else:raise AssertionError('Unpaired A read private data')
        # Real service restart, persistent DB and running outbound clients.
        _,stdout,stderr=ssh.exec_command('systemctl restart ab-center',timeout=30)
        if stdout.channel.recv_exit_status():raise RuntimeError('C restart failed')
        wait_for(lambda:operator.request('GET','/health')['status']=='ok',30)
        if local.get_result(task_id)['result']['artifacts'][0]['sha256']!=artifact['sha256']:raise AssertionError('Cloud history lost after restart')
        local.revoke_pairing(pair['pairing_id'])
        try:local.get_result(task_id)
        except APIError as error:
            if error.status not in {403,404}:raise
        else:raise AssertionError('Revocation did not deny old task result')
        # Re-pair for subsequent operator testing; old task remains inaccessible.
        code=b.service.remote.get_client().create_pairing_code(max_active=4);local.pair(b.service.engine.node_id,code['code'])
        report={'actual_cloud_origin':origin,'tls_verified':True,'pc_a_id':a.service.config['node_id'],'b_id':b.service.engine.node_id,
            'task_id':task_id,'actual_predictions':[15,23],'input_sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
            'result_sha256':artifact['sha256'],'elapsed_s':time.monotonic()-before,'same_key_one_group':True,
            'unpaired_task_and_file_denied':True,'cloud_service_restart_history_preserved':True,'pair_revocation_denied':True,
            'demo_model':True,'mqtt_broker_verified':False,'physical_esp32_test':False}
        (ROOT/'.local/cloud-validation/report.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2))
    finally:
        if a:a.close()
        if b:b.close()
        ssh.close()


if __name__=='__main__':main()
