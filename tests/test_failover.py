import asyncio
import base64
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.actions import Actions
from app.failover import Failover, LeaseLost, export_state, import_state, host_key, webhook_key, validate_hub
from app.store import Store


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.a = Store(Path(self.directory.name)/'a.sqlite3')
        self.b = Store(Path(self.directory.name)/'b.sqlite3')

    def tearDown(self):
        self.a.close(); self.b.close(); self.directory.cleanup()

    def test_restart_preserves_dispatched_job_and_deduplicates_update(self):
        update={'update_id':42,'message':{'text':'/copy https://example.org/file'}}
        self.a.ingest(update)
        self.a.next_update()
        job=self.a.add(42,123,123,'https://example.org/file',backend='actions')
        self.a.change(job['id'],state='running',dispatch_at=123456,run_id=456,message_id=99)
        encoded,digest=export_state(self.a.db)
        self.assertEqual(export_state(self.a.db),(encoded,digest))
        import_state(self.b.db,encoded);self.b.recover();self.b.ingest(update)
        restored=self.b.claim()
        self.assertEqual((restored['id'],restored['dispatch_at'],restored['run_id']), (job['id'],123456,456))
        replay=self.b.next_update()
        self.assertEqual(self.b.add(replay['update_id'],123,123,'ignored',backend='actions')['id'],job['id'])
        self.assertEqual(self.b.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_expiration_and_content_identity_survive_host_change(self):
        job=self.a.add(1,123,123,'https://example.org/file',backend='actions')
        self.a.change(job['id'],state='done',expires=123456789,etag='original',file_id='id',retention='armed')
        import_state(self.b.db,export_state(self.a.db)[0])
        restored=self.b.get(job['id'],123)
        self.assertEqual((restored['expires'],restored['etag'],restored['file_id']), (123456789,'original','id'))

    def test_missing_table_and_injected_column_leave_database_untouched(self):
        self.b.add(1,1,1,'keep')
        original=export_state(self.b.db)
        obj=json.loads(gzip.decompress(base64.b64decode(export_state(self.a.db)[0])))
        for mutate in (lambda o:o['tables'].pop('updates'),lambda o:o['tables']['jobs']['columns'].append('evil"')):
            value=json.loads(json.dumps(obj));mutate(value)
            encoded=base64.b64encode(gzip.compress(json.dumps(value).encode())).decode()
            with self.assertRaises(ValueError):import_state(self.b.db,encoded)
            self.assertEqual(export_state(self.b.db),original)

    def test_callbacks_use_separate_keys_and_validate_destination(self):
        self.assertNotEqual(host_key('test'),webhook_key('test'))
        settings=SimpleNamespace(hub_url='https://hub.workers.dev',hub_role='render',hub_bot='payload')
        validate_hub(settings)
        for url in ('http://hub.workers.dev','https://hub.workers.dev/path','https://example.org','https://x.workers.dev@evil.org'):
            settings.hub_url=url
            with self.assertRaises(ValueError):validate_hub(settings)


class Dispatch(unittest.IsolatedAsyncioTestCase):
    async def test_external_post_only_after_durable_intent(self):
        actions=Actions(SimpleNamespace(github_repo='a/b',github_ref='main',max_bytes=1024**3,retention_hours=12))
        events=[]
        async def guard():events.append('checkpoint')
        async def request(*args,**kwargs):events.append('dispatch');return {}
        actions.before_dispatch=guard;actions.request=request
        job=dict(id='0123456789ab',url='https://example.org',requested_name='boot',owner=1,chat=1,message_id=1)
        await actions.dispatch(job)
        self.assertEqual(events,['checkpoint','dispatch'])

    async def test_lease_failure_prevents_post_and_preserves_reconciliation(self):
        bot=SimpleNamespace(s=SimpleNamespace(hub_role='docker'))
        hub=Failover(bot);hub.checkpoint=AsyncMock(side_effect=LeaseLost())
        actions=Actions(SimpleNamespace(github_repo='a/b'));actions.before_dispatch=hub.dispatch_guard
        actions.request=AsyncMock()
        with self.assertRaises(asyncio.CancelledError):await actions.dispatch({})
        actions.request.assert_not_awaited()
