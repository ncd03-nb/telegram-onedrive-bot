import json
import unittest
from unittest.mock import AsyncMock, patch

from app.settings import Settings
from app.source import SourceError, Sources, oplus_error
from app.transfer import Transfer


class Content:
    def __init__(self, data):
        self.data = data

    async def read(self, size):
        data, self.data = self.data[:size], self.data[size:]
        return data

    async def iter_chunked(self, size):
        while self.data:
            yield await self.read(size)


class Reply:
    def __init__(self, url, status=200, headers=None, data=b''):
        self.url, self.status, self.headers = url, status, headers or {}
        self.content, self.content_length, self.released = Content(data), len(data), False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.release()

    def __await__(self):
        async def value():
            return self
        return value().__await__()

    def release(self):
        self.released = True


class Session:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.replies.pop(0)


class OplusSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cn_and_global_checks_get_updater_headers_then_stream_original_rom(self):
        for host in ('component-ota-cn.allawntech.com', 'component-ota-in.allawnos.com',
                     'ota.coloros.com', 'ota.oplusota.com'):
            original = f'https://{host}/downloadCheck?c=raw%2Bsignature&p=a%2Fb&mode=1'
            target = 'https://cdn.example.com/path/CPH2841_17.0.0.100_full.zip?token=private'
            rom = b'PK\x03\x04' + bytes(range(256)) * 4000
            redirect = Reply(original,302,{'Location':target})
            final = Reply(target,headers={'Content-Type':'application/zip','Content-Encoding':'UTF-8'},data=rom)
            source = Sources(Settings(token='test',users={1}))
            source.session = Session([redirect,final])
            async with source.open(original) as (response, meta):
                self.assertEqual(meta['name'],'CPH2841_17.0.0.100_full.zip')
                self.assertEqual(meta['size'],len(rom))
                self.assertEqual(b''.join([b async for b in response.content.iter_chunked(1024)]),rom)
            calls = source.session.calls
            self.assertEqual(calls[0][0],original)
            self.assertEqual(calls[0][1]['headers']['User-Agent'],'okhttp/3.12.12')
            self.assertEqual(calls[0][1]['headers']['userId'],'oplus-ota|16002018')
            self.assertNotIn('userId',calls[1][1]['headers'])
            self.assertNotIn('Authorization',calls[1][1]['headers'])
            self.assertTrue(redirect.released and final.released)

    async def test_check_json_error_never_starts_uploader(self):
        url = 'https://component-ota-cn.allawntech.com/downloadCheck?c=expired'
        response = Reply(url,headers={'Content-Type':'application/json'},data=b'{"body":null,"responseCode":2306,"errMsg":"2306"}')
        source = Sources(Settings(token='test',users={1}))
        source.session = Session([response])
        with patch('app.transfer.asyncio.create_subprocess_exec',new_callable=AsyncMock) as start:
            with self.assertRaisesRegex(SourceError,'2306'):
                await Transfer(source.settings,source).copy({'url':url,'id':'abcdef012345','requested_name':''},AsyncMock())
            start.assert_not_awaited()
        self.assertTrue(response.released)

    async def test_untrusted_hosts_and_http_cannot_receive_updater_headers(self):
        for url in ('https://component-ota-cn.allawntech.com.evil.org/downloadCheck',
                    'https://fakeallawntech.com/downloadCheck','https://example.com/downloadCheck',
                    'http://component-ota-cn.allawntech.com/downloadCheck'):
            source=Sources(Settings(token='test',users={1}));source.session=Session([])
            with self.assertRaises(SourceError):await source.oplus_download(url)
            self.assertEqual(source.session.calls,[])

    async def test_private_and_http_cdn_redirects_rejected(self):
        url='https://component-ota-cn.allawntech.com/downloadCheck'
        for target in ('http://127.0.0.1/file.zip','https://10.0.0.1/file.zip','http://cdn.example.com/file.zip'):
            source=Sources(Settings(token='test',users={1}));source.session=Session([Reply(url,302,{'Location':target})])
            with self.assertRaises(SourceError):await source.oplus_download(url)
            self.assertEqual(len(source.session.calls),1)

    async def test_cdn_private_redirect_is_checked_too(self):
        url='https://component-ota-in.allawnos.com/downloadCheck'
        cdn='https://cdn.example.com/file.zip'
        source=Sources(Settings(token='test',users={1}))
        source.session=Session([Reply(url,302,{'Location':cdn}),Reply(cdn,302,{'Location':'http://169.254.169.254/latest'})])
        with self.assertRaises(SourceError):
            async with source.open(url):pass
        self.assertEqual(len(source.session.calls),2)

    async def test_cdn_json_html_xml_errors_are_not_copied(self):
        url='https://component-ota-cn.allawntech.com/downloadCheck'
        cdn='https://cdn.example.com/ROM.zip'
        for kind in ('application/json','application/problem+json','text/html','application/xml'):
            source=Sources(Settings(token='test',users={1}))
            final=Reply(cdn,headers={'Content-Type':kind},data=b'{"responseCode":2306}')
            source.session=Session([Reply(url,302,{'Location':cdn}),final])
            with self.assertRaises(SourceError):
                async with source.open(url):pass
            self.assertTrue(final.released)

    async def test_real_http_compression_remains_rejected(self):
        source=Sources(Settings(token='test',users={1}))
        for encoding in ('gzip','br','deflate'):
            source.session=Session([Reply('https://cdn.example.com/ROM.zip',headers={'Content-Encoding':encoding})])
            with self.assertRaises(SourceError):await source.get('https://cdn.example.com/ROM.zip',oplus=True)

    async def test_normal_links_are_not_resolved_and_json_file_copy_still_allowed(self):
        source=Sources(Settings(token='test',users={1}))
        url='https://example.com/data.json'
        final=Reply(url,headers={'Content-Type':'application/json'},data=b'{"test":true}')
        source.session=Session([final])
        async with source.open(url) as (r,meta):
            self.assertEqual(meta['name'],'data.json')
        self.assertEqual(len(source.session.calls),1)

    def test_provider_error_text_does_not_echo_signed_url_or_untrusted_message(self):
        raw=json.dumps({'responseCode':2306,'errMsg':'https://private.example/?token=secret'}).encode()
        text=oplus_error(raw,200)
        self.assertIn('2306',text)
        self.assertNotIn('secret',text)
        self.assertNotIn('private.example',text)
        self.assertNotIn('secret',oplus_error(b'x'*4097,200))
