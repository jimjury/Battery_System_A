"""Read-only DNS/TLS readiness check. Never changes DNS or disables verification."""
import argparse
import json
from pathlib import Path
import socket
import urllib.request


def check(host,expected_ip):
    report={'hostname':host,'expected_ipv4':expected_ip,'dns_ready':False,'public_tls_ready':False}
    try:
        addresses=sorted({entry[4][0] for entry in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)})
        report['addresses']=addresses;report['dns_ready']=addresses==[expected_ip]
    except OSError as error:report['dns_error']=str(error)
    if report['dns_ready']:
        try:
            request=urllib.request.Request('https://'+host+'/health',headers={'User-Agent':'ABPlatformDomainReadiness/1'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=10) as response:
                health=json.loads(response.read(65536));report['health']=health
                report['public_tls_ready']=health.get('service')=='center' and health.get('status')=='ok'
        except Exception as error:report['tls_error']=str(error)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',default='c.jueyjim.loc.cc');parser.add_argument('--ip',default='60.204.129.7')
    parser.add_argument('--report',type=Path);args=parser.parse_args();result=check(args.host,args.ip)
    if args.report:args.report.write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
