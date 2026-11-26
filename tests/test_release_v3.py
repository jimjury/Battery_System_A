import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import release
from sdk.mcp_server import MCP


class SignedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.context=patch.object(release,'ROOT',self.root);self.context.start();release.generate_keys()
    def tearDown(self):self.context.stop();self.temp.cleanup()
    def test_signature_body_and_frame_integrity(self):
        data=b'actual artifact bytes';envelope=release.usb_envelope('ui',data)
        self.assertEqual(release.verify(envelope)['ui']['sha256'],hashlib.sha256(data).hexdigest())
        self.assertTrue(release.frame(envelope,data,'ui').endswith(data))
        with self.assertRaises(ValueError):release.frame(envelope,data+b'x','ui')
        raw=bytearray(base64.b64decode(envelope['signature']));raw[0]^=1
        with self.assertRaises(ValueError):release.verify(dict(envelope,signature=base64.b64encode(raw).decode()))
    def test_sequences_and_untrusted_sources(self):
        self.assertGreater(release.allocate_sequence('ui',2000000000),2000000000)
        self.assertGreater(release.allocate_sequence('ui'),2000000001)
        self.assertFalse(release.trusted_url('https://github.com/attacker/project/releases/download/v1/ui.bin'))
        self.assertFalse(release.trusted_url('http://raw.githubusercontent.com/a'))
        self.assertFalse(release.trusted_url('https://github.com@evil.example/a'))
    def test_strict_json_rejects_duplicate_and_nonfinite(self):
        for data in [b'{"a":1,"a":2}',b'{"a":NaN}']:
            with self.assertRaises(ValueError):release.strict_json(data)


class MCPPolicyTests(unittest.TestCase):
    def ready(self,server):
        result=server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        self.assertIn('tools',result['result']['capabilities'])
        self.assertIsNone(server.handle({'jsonrpc':'2.0','method':'notifications/initialized'}))
    def test_policy_and_malformed_requests(self):
        server=MCP(object());self.ready(server)
        result=server.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'cancel_task','arguments':{'task_id':'example'}}})
        self.assertTrue(result['result']['isError']);self.assertIn('operator_read_only',result['result']['content'][0]['text'])
        result=server.handle({'jsonrpc':'2.0','id':3,'method':'tools/list','params':[]})
        self.assertEqual(result['error']['code'],-32602)
    def test_unknown_file_cannot_read_arbitrary_path(self):
        server=MCP(object(),allow_submit=True);self.ready(server)
        result=server.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'upload_authorized_file','arguments':{'file_ref':'../../secret'}}})
        self.assertTrue(result['result']['isError']);self.assertIn('file_not_authorized',result['result']['content'][0]['text'])


if __name__=='__main__':unittest.main()
