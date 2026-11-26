import datetime
import ipaddress
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error

from ab_platform.http_service import serve
from sdk import ABClient


class Health:
    def dispatch(self,*args):return {'actual_tls':True}


class TLSVerificationTests(unittest.TestCase):
    def test_real_https_trust_chain_and_hostname_are_required(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes,serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'local-test-only')])
            now=datetime.datetime.now(datetime.timezone.utc)
            certificate=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
                .not_valid_after(now+datetime.timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
                .sign(key,hashes.SHA256()))
            cert=root/'test-ca.pem';private=root/'test.key'
            cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            server=serve(Health(),'127.0.0.1',0,cert=str(cert),key=str(private))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base=f'https://127.0.0.1:{server.server_port}'
                with self.assertRaises(urllib.error.URLError):ABClient(base).request('GET','/health')
                self.assertTrue(ABClient(base,ca_file=str(cert)).request('GET','/health')['actual_tls'])
                with self.assertRaises(urllib.error.URLError):ABClient(f'https://localhost:{server.server_port}',ca_file=str(cert)).request('GET','/health')
            finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
