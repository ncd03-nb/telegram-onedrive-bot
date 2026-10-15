import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.downloads import Downloads, download_url
from app.main import Bot, link_message, link_keyboard
from app.onedrive import GraphError
from app.retention import Retention
from app.settings import Settings


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = Settings(token='test-secret', users={1}, download_base_url='https://example.onrender.com')
        now = time.time()
        self.record = dict(version=1, id='abcdef012345', owner=1, chat=1, size=10,
            destination='onedrive:TelegramUploads/abcdef012345/file có dấu #1.zip',
            completed=now, expires=now + 43200, file_id='original-id', etag='v1')
        self.job = {**self.record, 'manifest': 1}
        self.item = {'id': 'original-id', 'eTag': 'v1', 'file': {}, 'size': 10,
            'name': 'file có dấu #1.zip', 'webUrl': 'https://example.sharepoint.com/file',
            '@microsoft.graph.downloadUrl': 'https://example.sharepoint.com/new-download?secret=temporary'}
        self.drive = type('Drive', (), {})()
        self.drive.read_manifest = AsyncMock(return_value=self.record)
        self.drive.links = AsyncMock(return_value=self.item)
        handler = Downloads(self.s, self.drive, Retention(self.s, None, self.drive, None))
        app = web.Application()
        app.router.add_get('/0:/{capability}/{name}', handler.handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def fetch(self, job=None, path=None):
        path = path or urlsplit(download_url(self.s, job or self.job)).path
        return await self.client.get(path, allow_redirects=False)

    async def test_redirect_fresh_url_without_database_or_file_proxy(self):
        response = await self.fetch()
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], self.item['@microsoft.graph.downloadUrl'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(await response.read(), b'')
        self.item['@microsoft.graph.downloadUrl'] = 'https://example.sharepoint.com/fresh'
        response = await self.fetch()
        self.assertTrue(response.headers['Location'].endswith('/fresh'))
        self.assertEqual(self.drive.links.await_count, 2)

    async def test_tampered_filename_signature_and_expiry_never_call_drive(self):
        path = urlsplit(download_url(self.s, self.job)).path
        for changed in (path.replace('/file', '/other'), path.replace('abcdef012345', 'abcdef012346'),
                        path.replace(str(int(self.job['expires'])), str(int(self.job['expires']) + 1))):
            self.assertEqual((await self.fetch(path=changed)).status, 404)
        self.drive.read_manifest.assert_not_awaited()

    async def test_expired_link_rejected_even_when_collector_offline(self):
        self.assertEqual((await self.fetch(job={**self.job, 'expires': time.time() - 10})).status, 410)
        self.drive.read_manifest.assert_not_awaited()

    async def test_replaced_or_edited_file_rejected(self):
        for key, value in [('id', 'replacement'), ('eTag', 'v2'), ('size', 20)]:
            original = self.item[key]
            self.item[key] = value
            self.assertEqual((await self.fetch()).status, 410)
            self.item[key] = original

    async def test_content_link_survives_metadata_etag_change_but_rejects_edits(self):
        self.record['link_ctag']='content-v1'
        self.job['link_ctag']='content-v1'
        self.item['cTag']='content-v1'
        self.item['eTag']='metadata-v2'
        self.assertEqual((await self.fetch()).status,302)
        self.item['cTag']='content-v2'
        self.assertEqual((await self.fetch()).status,410)
        self.item.pop('cTag')
        self.assertEqual((await self.fetch()).status,410)

    async def test_new_content_capability_cannot_select_another_snapshot(self):
        self.job['link_ctag']='old-content'
        self.record['link_ctag']='new-content'
        self.item['cTag']='new-content'
        self.assertEqual((await self.fetch()).status,410)

    async def test_content_link_still_checks_id_and_size(self):
        self.job['link_ctag']=self.record['link_ctag']=self.item['cTag']='content-v1'
        for key,value in (('id','replacement'),('size',20)):
            old=self.item[key];self.item[key]=value
            self.assertEqual((await self.fetch()).status,410)
            self.item[key]=old

    async def test_manifest_cannot_extend_deadline_or_select_other_file(self):
        self.record['expires'] += 1
        self.assertEqual((await self.fetch()).status, 404)
        self.drive.links.assert_not_awaited()
        self.record['expires'] -= 1
        self.record['destination'] = self.record['destination'].rsplit('/', 1)[0] + '/other.zip'
        self.assertEqual((await self.fetch()).status, 404)

    async def test_missing_file_and_temporary_failure_are_sanitized(self):
        self.drive.read_manifest.side_effect = GraphError(404)
        self.assertEqual((await self.fetch()).status, 410)
        self.drive.read_manifest.side_effect = TimeoutError('secret-url')
        response = await self.fetch()
        self.assertEqual(response.status, 503)
        self.assertNotIn('secret', await response.text())

    async def test_telegram_custom_link_and_fallback_when_manifest_missing(self):
        text = link_message(self.job, self.item, settings=self.s)
        self.assertEqual(text, '✅ Hoàn thành\n📄 ' + self.item['name'])
        self.assertNotIn('secret=temporary', text)
        row=link_keyboard(self.s,self.job,self.item)['inline_keyboard'][0]
        self.assertEqual([button['text'] for button in row],['⚡ Index Link','⬇ Tải trực tiếp'])
        self.assertEqual(row[0]['url'],download_url(self.s,self.job))
        self.assertEqual(row[1]['url'],self.item['@microsoft.graph.downloadUrl'])
        fallback=link_keyboard(self.s,{**self.job,'manifest':0},self.item)['inline_keyboard'][0]
        self.assertEqual(len(fallback),1)
        self.assertEqual(fallback[0]['text'],'⬇ Tải trực tiếp')
        self.assertEqual(link_keyboard(self.s,self.job,self.item,direct_only=True)['inline_keyboard'],[fallback])

    async def test_key_rotation_invalidates_previous_links(self):
        old_path = urlsplit(download_url(self.s, self.job)).path
        self.s.token = 'new-secret'
        self.assertEqual((await self.fetch(path=old_path)).status, 404)

    async def test_standby_opens_only_graph_session(self):
        with tempfile.TemporaryDirectory() as directory:
            s = Settings(token='test', users={1}, mode='standby', data_dir=Path(directory), download_base_url=self.s.download_base_url)
            bot = Bot(s)
            bot.telegram.start = AsyncMock()
            await bot.start(None)
            try:
                self.assertIsNotNone(bot.drive.session)
                self.assertEqual(bot.tasks, [])
                bot.telegram.start.assert_not_awaited()
            finally:
                await bot.close(None)
            self.assertTrue(bot.drive.session.closed)
