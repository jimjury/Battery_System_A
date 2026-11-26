"""Measure real local B workers; this is not physical A/cloud endurance acceptance."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from server_b.engine import Engine


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--seconds',type=int,default=180);args=parser.parse_args()
    try:
        import psutil
        process=psutil.Process();memory=lambda:process.memory_info().rss
    except ImportError:memory=lambda:None
    started=time.monotonic();durations=[];samples=[]
    with tempfile.TemporaryDirectory() as temporary:
        engine=Engine(Path(temporary)/'runtime')
        try:
            file=Path(temporary)/'text.txt';file.write_text('actual resource check input\n')
            uploaded=engine.import_file('local:resource-check',file)
            while time.monotonic()-started<args.seconds:
                begin=time.monotonic();task=engine.submit('local:resource-check','file_statistics','1.0.0',{'document':uploaded['file_id']},{})
                while True:
                    result=engine.task('local:resource-check',task['task_id'])
                    if result['state'] in {'completed','failed','cancelled'}:break
                    if time.monotonic()-begin>20:raise RuntimeError('Task exceeded resource-check deadline')
                    time.sleep(.01)
                if result['state']!='completed' or result['result']['metrics']['words']!=4:raise RuntimeError('Actual inference failed')
                durations.append((time.monotonic()-begin)*1000)
                if len(durations)%10==0:samples.append({'tasks':len(durations),'rss_bytes':memory(),'elapsed_s':time.monotonic()-started})
            ordered=sorted(durations);report={'service':'local B','elapsed_s':time.monotonic()-started,'actual_tasks':len(durations),
                'p50_ms':statistics.median(durations),'p95_ms':ordered[min(len(ordered)-1,int(len(ordered)*.95))],
                'rss_samples':samples,'esp32_or_cloud_acceptance':False,'long_duration_acceptance':False}
            output=ROOT/'.local/resource-check.json';output.write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps({k:v for k,v in report.items() if k!='rss_samples'}))
        finally:engine.close()


if __name__=='__main__':main()
