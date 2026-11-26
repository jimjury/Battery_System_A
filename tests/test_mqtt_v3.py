import json
import unittest
import time
from ab_platform.mqtt import MQTT,packet,string
from control_center.mqtt_bridge import role_for


class Wire:
    def __init__(self,data):self.data=bytearray(data);self.sent=bytearray()
    def recv(self,count):
        result=bytes(self.data[:min(count,3)]);del self.data[:len(result)];return result
    def sendall(self,data):self.sent.extend(data)
    def close(self):pass


class MQTTBoundaryTests(unittest.TestCase):
    def client(self,data):
        client=MQTT('mqtts://localhost:8883','node','test-only','ab-node');client.socket=Wire(data);return client
    def test_qos_one_duplicate_is_acknowledged_without_execution(self):
        body=string('ab/v1/nodes/node/down/events')+b'\x00\x09'+json.dumps({'schema_version':1,'event_id':'opaque'}).encode()
        client=self.client(packet(0x32,body)+packet(0x3a,body))
        client.receive();client.receive()
        self.assertEqual(client.socket.sent,packet(0x40,b'\x00\x09')*2)
        self.assertEqual(client.messages[0],client.messages[1])
        self.assertEqual(len(client.messages),2)
    def test_oversize_and_malformed_packets_are_rejected(self):
        for wire in [b'\x30\xff\xff\xff\xff',b'\x30\x03\x00\x20x']:
            with self.assertRaises(ValueError):self.client(wire).receive()
        with self.assertRaises(ValueError):packet(0x30,b'x'*8193)
    def test_tls_origin_and_exact_acl_scope(self):
        for uri in ['mqtt://example.test','mqtts://user:secret@example.test','mqtts://example.test/path']:
            with self.assertRaises(ValueError):MQTT(uri,'node','test','client')
        first=role_for('00000000-0000-4000-8000-000000000001')
        second=role_for('00000000-0000-4000-8000-000000000002')
        self.assertNotEqual(first['acls'][0]['topic'],second['acls'][0]['topic'])
        self.assertTrue(all(acl['topic'].endswith('/down/events') and '+' not in acl['topic'] and '#' not in acl['topic'] for acl in first['acls']))
        self.assertFalse(any(acl['acltype']=='publishClientSend' for acl in first['acls']))
    def test_missing_keepalive_ack_disconnects_instead_of_false_online(self):
        client=self.client(b'');client.ping_at=time.monotonic()-9
        with self.assertRaises(TimeoutError):client.poll()
        client=self.client(packet(0xd0));client.ping_at=time.monotonic()
        client.receive();self.assertEqual(client.ping_at,0)
    def test_bad_or_missing_puback_never_acknowledges_publication(self):
        client=self.client(packet(0x40,b'\x00\x01extra'))
        with self.assertRaises(ValueError):client.publish('exact/topic',b'notification')
        client=self.client(b'')
        with self.assertRaises(OSError):client.publish('exact/topic',b'notification')


if __name__=='__main__':unittest.main()
