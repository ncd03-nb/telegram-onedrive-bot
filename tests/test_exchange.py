import json
import tempfile
import unittest
from pathlib import Path
from app.exchange import seal,unseal,write_result


class EncryptedExchange(unittest.TestCase):
    def test_public_inputs_and_artifacts_hide_private_urls_and_chat(self):
        job={'id':'0123456789ab','url':'https://example.org/private?token=fixture','chat':-100123,'owner':123}
        first=seal('test-token',job,job['id']);second=seal('test-token',job,job['id'])
        self.assertNotEqual(first,second)
        self.assertNotIn(job['url'],first);self.assertEqual(unseal('test-token',first,job['id']),job)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.json';write_result(path,{**job,'state':'done'},'test-token')
            raw=path.read_text();self.assertNotIn(job['url'],raw);self.assertNotIn('-100123',raw)
            self.assertEqual(unseal('test-token',json.loads(raw)['sealed'],job['id'])['chat'],job['chat'])

    def test_wrong_key_identity_and_modified_ciphertext_are_rejected(self):
        value=seal('test-token',{'id':'0123456789ab'},'0123456789ab')
        for key,identity,envelope in [('other','0123456789ab',value),('test-token','222222222222',value),('test-token','0123456789ab',value[:-2]+'xx')]:
            with self.assertRaises(Exception):unseal(key,envelope,identity)
