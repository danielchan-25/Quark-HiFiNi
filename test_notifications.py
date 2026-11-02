import json
import subprocess
import unittest
from unittest.mock import Mock

import notifications as delivery


class DeliveryTests(unittest.TestCase):
    def send(self, code, stdout='', stderr=''):
        execute = Mock(return_value=subprocess.CompletedProcess([],code,stdout,stderr))
        return delivery.send_text('https://example.invalid','secret','test',execute=execute,curl='curl',sanitize=lambda x:x)

    def test_connection_failures_are_proven_unsent_but_read_timeouts_are_unknown(self):
        for code,error in [(6,'Could not resolve host'),(7,'Failed to connect'),(28,'Resolving timed out')]:
            result=self.send(code,stderr=error)
            self.assertEqual(result['status'],'NOT_SENT')
            self.assertEqual(result['curl_exit'],code)
        self.assertEqual(self.send(28,stderr='Operation timed out')['status'],'UNKNOWN')

    def test_remote_rejection_and_invalid_response_are_distinct(self):
        result=self.send(0,json.dumps({'code':19021,'msg':'signature rejected'}))
        self.assertEqual(result['status'],'REJECTED')
        self.assertEqual(result['response_code'],19021)
        self.assertEqual(self.send(0,'<html>bad gateway</html>')['status'],'UNKNOWN')
        self.assertEqual(self.send(0,'{"code":0}')['status'],'SENT')

    def test_legacy_retries_require_a_single_preconnect_attempt(self):
        row={'status':'FAILED','attempts':1,'last_error':'Failed to connect'}
        self.assertTrue(delivery.retryable(row))
        self.assertFalse(delivery.retryable({**row,'attempts':3}))
        self.assertFalse(delivery.retryable({**row,'status':'UNKNOWN'}))
        self.assertTrue(delivery.retryable({'status':'NOT_CONFIGURED','attempts':0}))

    def test_new_delivery_evidence_is_conservative(self):
        for state in ('UNKNOWN','REJECTED','SENDING'):
            self.assertFalse(delivery.retryable({'status':'FAILED','attempts':1,'last_error':json.dumps({'delivery_version':1,'status':state})}))
