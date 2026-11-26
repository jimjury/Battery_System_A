"""Prepare verified signed candidates; publish/activate only with explicit approval.

Uses existing Python, local publisher key and existing credential manager.
Never installs tools, embeds tokens in firmware, or publishes on every commit.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from release import ROOT,allocate_sequence,canonical,package,verify,sign,strict_json

REPO='jimjury/Battery_System_A'


def credential():
    token=os.environ.get('GITHUB_TOKEN')
    if token:return token
    env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='Never')
    try:
        result=subprocess.run(['git','credential','fill'],input=b'protocol=https\nhost=github.com\n\n',
                              capture_output=True,env=env,timeout=8,check=False)
        fields=dict(line.split('=',1) for line in result.stdout.decode().splitlines() if '=' in line)
        if fields.get('password'):return fields['password']
    except (OSError,ValueError,subprocess.TimeoutExpired):pass
    raise RuntimeError('Publisher needs existing GitHub Credential Manager authorization or a process-local GITHUB_TOKEN. Never paste a token into source or chat.')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        url=urllib.parse.urlsplit(newurl)
        if url.scheme!='https' or url.hostname not in {'api.github.com','github.com','objects.githubusercontent.com','release-assets.githubusercontent.com'}:
            raise RuntimeError('Untrusted release download redirect')
        redirected=super().redirect_request(req,fp,code,msg,headers,newurl)
        redirected.remove_header('Authorization')
        return redirected


class GitHub:
    def __init__(self,token):
        self.token=token;self.opener=urllib.request.build_opener(SafeRedirect())
    def api(self,method,path,value=None,data=None,content_type='application/json'):
        url=path if path.startswith('https://uploads.github.com/') else 'https://api.github.com'+path
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in {'api.github.com','uploads.github.com'}:raise RuntimeError('Unexpected GitHub API origin')
        if value is not None:data=canonical(value)
        req=urllib.request.Request(url,data=data,method=method,headers={'Accept':'application/vnd.github+json',
            'Content-Type':content_type,'User-Agent':'ABPlatformPublisher','X-GitHub-Api-Version':'2022-11-28'})
        req.add_unredirected_header('Authorization','Bearer '+self.token)
        try:
            with self.opener.open(req,timeout=60) as response:return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code==404:return None
            raise RuntimeError(f'GitHub API returned HTTP {error.code}; no manifest activation performed') from None
    def read(self,url,maximum,authorized=False):
        req=urllib.request.Request(url,headers={'Accept':'application/octet-stream','User-Agent':'ABPlatformPublisher'})
        if authorized:req.add_unredirected_header('Authorization','Bearer '+self.token)
        with self.opener.open(req,timeout=60) as response:
            data=response.read(maximum+1)
        if len(data)>maximum:raise RuntimeError('Release asset exceeded its signed size')
        return data


def host_checks():
    directory=ROOT/'.deploy/publish_checks';directory.mkdir(parents=True,exist_ok=True)
    log=directory/'tests.log'
    with log.open('wb') as file:
        result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],cwd=ROOT,stdout=file,stderr=subprocess.STDOUT,check=False)
    if result.returncode:raise RuntimeError('Host regression failed; release preparation stopped. See .deploy/publish_checks/tests.log')
    return hashlib.sha256(log.read_bytes()).hexdigest()


def clean_source():
    output=subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)
    if output:raise RuntimeError('Commit and review source before public release; working tree is dirty')


def publish(directory,kind,tag,approved=False,candidate=False,activate=False):
    if not approved:raise RuntimeError('Public release needs explicit --approved after user review')
    clean_source();directory=Path(directory).resolve()
    envelope=json.loads((directory/'manifest.json').read_bytes());item=verify(envelope)[kind]
    artifact=(directory/(kind+'.bin')).read_bytes()
    if hashlib.sha256(artifact).hexdigest()!=item['sha256'] or len(artifact)!=item['size']:raise RuntimeError('Local release artifact does not match its publisher signature')
    if not candidate:
        receipt=ROOT/'.local'/('hardware-acceptance-'+kind+'.json')
        record=json.loads(receipt.read_bytes()) if receipt.exists() else {}
        if record.get('hardware_verified') is not True or record.get('artifact_sha256')!=item['sha256']:
            raise RuntimeError('No matching actual hardware acceptance receipt. A software-only candidate cannot become an installable entry.')
    if candidate and activate:raise RuntimeError('Candidate publication cannot activate the default update manifest')
    public_url=f'https://github.com/{REPO}/releases/download/{urllib.parse.quote(tag,safe="")}/{kind}.bin'
    if item['url']!=public_url:raise RuntimeError('Signed URL and immutable release location disagree')
    github=GitHub(credential());base='/repos/'+REPO
    if github.api('GET',base+'/releases/tags/'+urllib.parse.quote(tag,safe='')):raise RuntimeError('Release tag already exists; immutable assets are never overwritten')
    release=github.api('POST',base+'/releases',{'tag_name':tag,'name':tag,'draft':True,'prerelease':candidate,
        'body':'Signed candidate; no default activation.' if candidate else 'Hardware-accepted signed release.'})
    for name,data in [(kind+'.bin',artifact),('manifest.json',canonical(envelope))]:
        asset=github.api('POST',f"https://uploads.github.com/repos/{REPO}/releases/{release['id']}/assets?name={name}",data=data,content_type='application/octet-stream')
        downloaded=github.read(asset['url'],len(data),authorized=True)
        if downloaded!=data:raise RuntimeError('Draft release asset read-back failed; draft retained and manifest unchanged')
    github.api('PATCH',base+f"/releases/{release['id']}",{'draft':False})
    downloaded=github.read(public_url,item['size'])
    if hashlib.sha256(downloaded).hexdigest()!=item['sha256']:raise RuntimeError('Public release read-back failed; default manifest unchanged')
    if activate:
        current=github.api('GET',base+'/contents/updates/manifest.json')
        import base64
        old=strict_json(base64.b64decode(current['content']))
        payload=verify(envelope)
        if old.get('schema_version')==2:
            previous=verify(old)
            other='firmware' if kind=='ui' else 'ui'
            payload[other]=previous.get(other)
            if previous.get(kind) and item['sequence']<=previous[kind]['sequence']:
                raise RuntimeError('Activation would regress the release sequence')
        elif old.get('schema_version')!=1 or old.get('ui') is not None or old.get('firmware') is not None:
            raise RuntimeError('Refusing to replace an unverified update entry')
        activation=sign(payload)
        github.api('PUT',base+'/contents/updates/manifest.json',{'message':'release: activate verified signed '+tag,
            'content':base64.b64encode(canonical(activation)).decode(),'sha':current['sha'],'branch':'main'})
    return {'release_url':release['html_url'],'candidate':candidate,'default_manifest_activated':activate,'sha256':item['sha256']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','publish']);parser.add_argument('--kind',choices=['ui','firmware'],default='ui')
    parser.add_argument('--version');parser.add_argument('--tag');parser.add_argument('--directory',type=Path)
    parser.add_argument('--approved',action='store_true');parser.add_argument('--candidate',action='store_true');parser.add_argument('--activate',action='store_true')
    args=parser.parse_args()
    if args.action=='prepare':
        if not args.tag or not args.version:parser.error('prepare requires --version and --tag')
        test_hash=host_checks();url=f'https://github.com/{REPO}/releases/download/{urllib.parse.quote(args.tag,safe="")}/{args.kind}.bin'
        directory=package(args.kind,args.version,allocate_sequence(args.kind),url)
        (directory/'host-evidence.json').write_bytes(canonical({'tests_log_sha256':test_hash,'hardware_accepted':False}))
        print(directory)
    else:
        if not args.tag or not args.directory:parser.error('publish requires --tag and --directory')
        print(json.dumps(publish(args.directory,args.kind,args.tag,args.approved,args.candidate,args.activate),indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as error:print('ERROR: '+str(error),file=sys.stderr);sys.exit(1)
