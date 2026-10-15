import asyncio
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.actions import Actions, decode_result
from app.main import Bot
from app.progress import Progress
from app.settings import Settings
from app.source import SourceError
from app.store import Store


class ActionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = Bot(Settings(token='test',users={1},data_dir=Path(self.temp.name),transfer_backend='actions'))
        self.bot.telegram.send = AsyncMock(return_value={'message_id': 77})
        self.bot.telegram.edit = AsyncMock()

    async def asyncTearDown(self):
        self.bot.store.close()
        self.temp.cleanup()

    def job(self):
        job = self.bot.store.add(10,1,1,'https://example.com/file',backend='actions')
        self.bot.store.change(job['id'],message_id=77)
        return self.bot.store.claim()

    async def test_dispatch_inputs_are_data_not_shell(self):
        actions = Actions(self.bot.s)
        actions.request = AsyncMock(return_value={})
        job = self.job()
        job['requested_name'] = '$(echo unsafe); ROM.zip'
        await actions.dispatch(job)
        inputs = actions.request.await_args.args[2]['inputs']
        from app.exchange import unseal
        decoded=unseal(self.bot.s.token,inputs['job_payload'],job['id'])
        self.assertEqual(decoded['requested_name'],job['requested_name'])
        self.assertEqual(decoded['message_id'],77)
        self.assertNotIn(job['url'],json.dumps(inputs))

    async def test_existing_run_is_reconciled_without_dispatch_after_restart(self):
        job = self.job()
        self.bot.store.change(job['id'],dispatch_at=time.time(),run_id=999)
        self.bot.store.recover()
        job = self.bot.store.claim()
        self.assertEqual(job['state'],'running')
        self.bot.actions.dispatch = AsyncMock()
        self.bot.actions.observe = AsyncMock(return_value={'state':'failed','error':'expected test failure'})
        await self.bot.actions_job(job)
        self.bot.actions.dispatch.assert_not_awaited()
        self.assertEqual(self.bot.store.get(job['id'],1)['state'],'failed')

    async def test_ambiguous_dispatch_does_not_retry_post(self):
        job = self.job()
        self.bot.actions.dispatch = AsyncMock(side_effect=asyncio.TimeoutError())
        self.bot.actions.observe = AsyncMock(return_value=None)
        await self.bot.actions_job(job)
        await self.bot.actions_job(self.bot.store.get(job['id'],1))
        self.bot.actions.dispatch.assert_awaited_once()
        self.assertTrue(self.bot.store.get(job['id'],1)['dispatch_at'])
        self.assertEqual(self.bot.store.get(job['id'],1)['url'],'')

    async def test_completion_recovers_original_expiration_and_does_not_duplicate_link(self):
        job = self.job()
        now = time.time() - 30
        record = {'version':1,'id':job['id'],'owner':1,'chat':1,'destination':f"onedrive:TelegramUploads/{job['id']}/rom.zip",
            'size':10,'completed':now,'expires':now+43200,'file_id':'original','etag':'etag'}
        self.bot.actions.dispatch = AsyncMock(return_value={})
        self.bot.actions.observe = AsyncMock(return_value={'state':'done','record':record,'manifest':1,'links_notified':True})
        await self.bot.actions_job(job)
        current = self.bot.store.get(job['id'],1)
        self.assertEqual(current['expires'],record['expires'])
        self.assertEqual(current['state'],'done')
        self.bot.telegram.send.assert_not_awaited()

    async def test_wrong_owner_result_is_not_imported(self):
        job = self.job()
        now = time.time()
        record = {'version':1,'id':job['id'],'owner':2,'chat':2,'destination':f"onedrive:TelegramUploads/{job['id']}/rom.zip",
            'size':10,'completed':now,'expires':now+43200,'file_id':'original','etag':'etag'}
        self.bot.actions.dispatch = AsyncMock(return_value={})
        self.bot.actions.observe = AsyncMock(return_value={'state':'done','record':record})
        await self.bot.actions_job(job)
        self.assertEqual(self.bot.store.get(job['id'],1)['state'],'failed')

    def test_result_archive_identity_and_decompression_limit(self):
        def archive(obj):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
                z.writestr('result.json',json.dumps(obj))
            return stream.getvalue()
        with self.assertRaises(SourceError):
            decode_result(archive({'id':'other','state':'done'}),'abc')
        with self.assertRaises(SourceError):
            decode_result(archive({'id':'abc','state':'done','extra':'x'*70000}),'abc')

    async def test_progress_periodic_even_without_new_bytes_and_never_100_before_commit(self):
        p = Progress(self.bot.telegram,1,77,'abc',interval=0.02)
        await p.update(100,100,'dest')
        self.assertIn('99.0%',p.text())
        await p.start()
        await asyncio.sleep(0.07)
        self.assertEqual(self.bot.telegram.edit.await_count,1)
        p.last_render-=11
        await asyncio.sleep(0.03)
        self.assertGreaterEqual(self.bot.telegram.edit.await_count,2)
        await p.close(finished=True)
        self.assertIn('100.0%',self.bot.telegram.edit.await_args.args[2])
        count = self.bot.telegram.edit.await_count
        await asyncio.sleep(0.04)
        self.assertEqual(count,self.bot.telegram.edit.await_count)

    def test_manifest_recovers_running_actions_job_with_original_ttl(self):
        job = self.job()
        now = time.time()
        record = {'id':job['id'],'owner':1,'chat':1,'destination':f"onedrive:TelegramUploads/{job['id']}/rom.zip",'size':10,
            'completed':now,'expires':now+43200,'file_id':'original','etag':'etag'}
        self.bot.store.import_manifest(record)
        self.assertEqual(self.bot.store.get(job['id'],1)['state'],'done')
        self.assertEqual(self.bot.store.get(job['id'],1)['expires'],record['expires'])

    async def test_queued_message_is_created_once(self):
        update={'update_id':90,'message':{'text':'/copy https://example.com/file','from':{'id':1},'chat':{'id':1,'type':'private'}}}
        await self.bot.handle(update)
        await self.bot.handle(update)
        self.bot.telegram.send.assert_awaited_once()
        job=self.bot.store.claim()
        self.assertEqual(job['backend'],'actions')
        self.assertEqual(job['message_id'],77)

    def test_chunk_sizes_respect_graph_fragment_constraints(self):
        env={'TELEGRAM_BOT_TOKEN':'123:fake','ALLOWED_USER_IDS':'1','BOT_MODE':'standby','DATA_DIR':self.temp.name}
        for value in ('0','6','60'):
            with self.subTest(value=value), patch.dict(os.environ,{**env,'ONEDRIVE_CHUNK_MIB':value},clear=True):
                with self.assertRaises(ValueError):Settings.load()
        with patch.dict(os.environ,{**env,'ONEDRIVE_CHUNK_MIB':'50'},clear=True):
            chunk=Settings.load().onedrive_chunk_mib*1024**2
            self.assertEqual(chunk%327680,0)
            self.assertLess(chunk,60*1024**2)


if __name__=='__main__':
    unittest.main()
