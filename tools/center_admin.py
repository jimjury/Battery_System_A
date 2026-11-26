"""Issue enrollment codes through C HTTPS with an operator-provided credential."""
import argparse
import getpass
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from sdk import ABClient


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['status','enrollment'])
    parser.add_argument('--role',choices=['A','B']);parser.add_argument('--profile',type=Path,default=ROOT/'client_config/center.json')
    args=parser.parse_args();profile=json.loads(args.profile.read_text())
    token=getpass.getpass('C administrator credential (not saved): ')
    client=ABClient(profile['url'],token,ca_file=str((args.profile.parent/profile['ca_file']).resolve()))
    result=client.request('GET','/api/v1/admin/status') if args.action=='status' else client.request('POST','/api/v1/admin/enrollments',{'role':args.role or 'A'})
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
