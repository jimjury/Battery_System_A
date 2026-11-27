import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest

from ab_platform.http_service import serve
from ab_platform.contracts import Fault


class Health:
    def dispatch(self,*args):return {'service':'center','status':'ok'}


class NamedTLS(unittest.TestCase):
    def setUp(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes,serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        issuer=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'test-only domain migration CA')])
        now=datetime.datetime.now(datetime.timezone.utc)
        def builder():return x509.CertificateBuilder().issuer_name(name).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1))
        ca=builder().subject_name(name).public_key(issuer.public_key()).add_extension(x509.BasicConstraints(ca=True,path_length=0),True).sign(issuer,hashes.SHA256())
        self.ca=self.root/'ca.pem';self.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        self.certificates={}
        for label,san in [('ip',x509.IPAddress(ipaddress.ip_address('127.0.0.1'))),('domain',x509.DNSName('center.unit.test'))]:
            key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'leaf-'+label+'.unit.test')])
            cert=builder().subject_name(subject).public_key(key.public_key()).add_extension(x509.BasicConstraints(ca=False,path_length=None),True).add_extension(x509.SubjectAlternativeName([san]),False).sign(issuer,hashes.SHA256())
            path=self.root/(label+'.pem');private=self.root/(label+'.key')
            path.write_bytes(cert.public_bytes(serialization.Encoding.PEM));private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            self.certificates[label]=(str(path),str(private),hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest())
    def tearDown(self):self.temp.cleanup()
    def options(self):
        cert,key,_=self.certificates['ip'];named,named_key,_=self.certificates['domain']
        return dict(cert=cert,key=key,tls_names=[{'hostname':'center.unit.test','cert':named,'key':named_key}])
    def test_actual_sni_selects_domain_leaf_and_preserves_ip_certificate(self):
        server=serve(Health(),'127.0.0.1',0,**self.options());thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            context=ssl.create_default_context(cafile=str(self.ca))
            for hostname,label in [('127.0.0.1','ip'),('center.unit.test','domain')]:
                with socket.create_connection(('127.0.0.1',server.server_port)) as raw:
                    with context.wrap_socket(raw,server_hostname=hostname) as connection:
                        self.assertEqual(hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest(),self.certificates[label][2])
                        connection.sendall(('GET /health HTTP/1.0\r\nHost: '+hostname+'\r\n\r\n').encode())
                        data=b''
                        while True:
                            block=connection.recv(4096)
                            if not block:break
                            data+=block
                        self.assertIn(b'200 OK',data);self.assertEqual(json.loads(data.split(b'\r\n\r\n',1)[1])['service'],'center')
            self.assertIn('center.unit.test',server.hosts)
        finally:server.shutdown();server.server_close();thread.join()
    def test_unsafe_or_duplicate_host_configuration_is_rejected(self):
        valid=self.options()['tls_names'][0]
        for names in [{},[dict(valid,hostname='*.unit.test')],[dict(valid,hostname='https://center.unit.test')],[valid,valid]]:
            options=self.options();options['tls_names']=names
            with self.assertRaises(Fault):serve(Health(),'127.0.0.1',0,**options)


if __name__=='__main__':unittest.main()
