"""Deploy the verified C bundle over pinned SSH to an explicitly chosen server.

SSH configuration is a private JSON file, not command-line secrets. Local tests
must pass before invoking install. Existing remote Python/systemd are reused.
"""
import argparse
import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import shlex
import sys

ROOT=Path(__file__).resolve().parents[1]


def make_tls(host,directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    directory.mkdir(parents=True,exist_ok=True);ca_file=directory/'ca.pem';ca_key_file=directory/'ca.key'
    now=datetime.datetime.now(datetime.timezone.utc)
    if ca_file.exists() and ca_key_file.exists():
        ca=x509.load_pem_x509_certificate(ca_file.read_bytes());ca_key=serialization.load_pem_private_key(ca_key_file.read_bytes(),password=None)
    else:
        ca_key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'AB Platform private test CA')])
        ca=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(minutes=5)).not_valid_after(now+datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True,key_encipherment=False,content_commitment=False,data_encipherment=False,key_agreement=False,key_cert_sign=True,crl_sign=True,encipher_only=None,decipher_only=None),critical=True).sign(ca_key,hashes.SHA256()))
        ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        ca_key_file.write_bytes(ca_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    try:san=x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:san=x509.DNSName(host)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,host)])
    certificate=(x509.CertificateBuilder().subject_name(subject).issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-datetime.timedelta(minutes=5)).not_valid_after(now+datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
        .add_extension(x509.SubjectAlternativeName([san]),critical=False)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).sign(ca_key,hashes.SHA256()))
    (directory/'server.pem').write_bytes(certificate.public_bytes(serialization.Encoding.PEM)+ca_file.read_bytes())
    (directory/'server.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    return directory


def connect(configuration,known_hosts):
    import paramiko
    client=paramiko.SSHClient();client.load_host_keys(str(known_hosts));client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(configuration['host'],port=configuration.get('port',22),username=configuration['username'],password=configuration.get('password'),
        key_filename=configuration.get('key_file'),look_for_keys=False,allow_agent=False,timeout=15,auth_timeout=15,banner_timeout=15)
    return client


def install(configuration,known_hosts,version):
    from package_center import package
    config=json.loads(Path(configuration).read_text());bundle=package(version)
    tls=make_tls(config['host'],ROOT/'.local/pki')
    client=connect(config,known_hosts)
    try:
        staging='/root/ab-center-deploy-'+version
        _,stdout,stderr=client.exec_command('umask 077; mkdir -p '+shlex.quote(staging))
        if stdout.channel.recv_exit_status()!=0:raise RuntimeError('Could not create private staging directory')
        with client.open_sftp() as sftp:
            for local,name in [(Path(bundle['path']),'bundle.zip'),(ROOT/'deploy/install_center.py','install_center.py'),(tls/'server.pem','server.pem'),(tls/'server.key','server.key'),(tls/'ca.pem','ca.pem')]:
                target=staging+'/'+name;sftp.put(str(local),target);sftp.chmod(target,0o600)
        command=['python3',staging+'/install_center.py','--bundle',staging+'/bundle.zip','--sha256',bundle['sha256'],
                 '--cert',staging+'/server.pem','--key',staging+'/server.key','--ca',staging+'/ca.pem','--origin','https://'+config['host']]
        _,stdout,stderr=client.exec_command(shlex.join(command),timeout=90)
        result=stdout.read().decode();errors=stderr.read().decode();exit_code=stdout.channel.recv_exit_status()
        (ROOT/'.local/cloud-install.log').write_text(result+errors,encoding='utf-8')
        if exit_code:raise RuntimeError('Remote install failed; review private .local/cloud-install.log')
        receipt=json.loads(result.strip().splitlines()[-1]);(ROOT/'.local/cloud-deployment.json').write_text(json.dumps(receipt,indent=2))
        public=ROOT/'client_config';public.mkdir(exist_ok=True)
        (public/'cloud-ca.pem').write_bytes((tls/'ca.pem').read_bytes())
        (public/'center.json').write_text(json.dumps({'url':'https://'+config['host'],'ca_file':'cloud-ca.pem'},indent=2))
        print(json.dumps(receipt,indent=2))
    finally:client.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--known-hosts',type=Path,required=True);parser.add_argument('--version',required=True)
    args=parser.parse_args();install(args.credentials,args.known_hosts,args.version)
