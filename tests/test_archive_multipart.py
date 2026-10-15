import asyncio
import hashlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from app.archive import Archive,ArchiveError,completion,safe_url
from unittest.mock import AsyncMock


class MultipartTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_production_file_uses_one_checksum_verified_put(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'boot.img';path.write_bytes(b'fixture')
            client=Archive('access','secret');client.request=AsyncMock(return_value=b'')
            result=await client.multipart({'path':str(path),'size':7,'md5':hashlib.md5(b'fixture').hexdigest()},'odrivegay-'+'a'*12+'-'+'b'*16,collection='opensource_media')
            client.request.assert_awaited_once();call=client.request.await_args
            self.assertEqual(call.args[0],'PUT');self.assertEqual(call.args[3]['x-archive-meta-collection'],'opensource_media')
            self.assertEqual(result['parts'],1)

    async def test_cleanup_is_idempotent_when_sample_is_already_gone(self):
        client=Archive('access','secret')
        client.request=AsyncMock(side_effect=ArchiveError('IAS3 HTTP 404'))
        await client.cleanup({'path':'sample.bin'},'bot-upload-test-'+'c'*32)
        client.request.assert_awaited_once()

    async def test_reuses_validated_archive_redirect_and_expect_continue(self):
        class Response:
            def __init__(self,status,location=None):
                self.status=status;self.headers={'Location':location} if location else {}
                self.content=type('Content',(),{'read':AsyncMock(return_value=b'')})()
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        calls=[];responses=iter([Response(307,'https://ia800001.us.archive.org/item/file?uploads'),Response(200),Response(200)])
        class Session:
            def request(self,method,url,**kwargs):calls.append((method,url,kwargs));return next(responses)
        client=Archive('access','secret');client.session=Session()
        await client.request('POST','https://s3.us.archive.org/item/file?uploads')
        await client.request('PUT','https://s3.us.archive.org/item/file?partNumber=1&uploadId=fixture',b'a'*(5*1024**2))
        self.assertEqual(calls[-1][1],'https://ia800001.us.archive.org/item/file?partNumber=1&uploadId=fixture')
        self.assertTrue(calls[-1][2]['expect100'])
        self.assertEqual(client.redirects,1)

    async def test_missing_part_etags_use_quoted_md5_in_sorted_completion(self):
        seen=[]
        class Fake(Archive):
            async def request(self,method,url,data=None,headers=None,**kwargs):
                seen.append((method,url,data,headers))
                if url.endswith('?uploads'):return b'<InitiateMultipartUploadResult><UploadId>fixture</UploadId></InitiateMultipartUploadResult>'
                await asyncio.sleep(0)
                return b''
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.bin';a=b'a'*(5*1024**2);b=b'b'*123;path.write_bytes(a+b)
            client=Fake('access','secret',workers=2,part_mib=5)
            progress=[]
            async def update(done,total):progress.append((done,total))
            result=await client.multipart({'path':str(path),'size':len(a+b)},'bot-upload-test-'+'a'*32,update)
            self.assertEqual(result['parts'],2)
            completed=next(c for c in seen if c[0]=='POST' and '?uploadId=' in c[1])
            root=ET.fromstring(completed[2])
            self.assertEqual([(p.findtext('PartNumber'),p.findtext('ETag')) for p in root],
                [('1','"'+hashlib.md5(a).hexdigest()+'"'),('2','"'+hashlib.md5(b).hexdigest()+'"')])
            self.assertEqual(progress[-1],(len(a+b),len(a+b)))
            self.assertFalse(any(c[0]=='DELETE' for c in seen))

    async def test_failed_completion_aborts_only_its_multipart_upload(self):
        seen=[]
        class Fake(Archive):
            async def request(self,method,url,data=None,headers=None,**kwargs):
                seen.append((method,url))
                if url.endswith('?uploads'):return b'<Result><UploadId>test/id</UploadId></Result>'
                if method=='POST':raise ArchiveError('Test completion failure')
                return b''
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.bin';path.write_bytes(b'fixture')
            with self.assertRaises(ArchiveError):
                await Fake('access','secret').multipart({'path':str(path),'size':7},'bot-upload-test-'+'b'*32)
        deletes=[url for method,url in seen if method=='DELETE']
        self.assertEqual(len(deletes),1)
        self.assertTrue(deletes[0].endswith('?uploadId=test%2Fid'))

    async def test_redirect_hosts_and_memory_bounds_are_enforced(self):
        for url in ['http://archive.org/a','https://archive.org.evil.invalid/a','https://user@archive.org/a']:
            with self.assertRaises(ArchiveError):safe_url(url)
        for workers,part in [(17,16),(8,64),(1,4)]:
            with self.assertRaises(ValueError):Archive('access','secret',workers,part)
        with self.assertRaises(ValueError):
            await Archive('access','secret').cleanup({'path':'file.bin'},'an-existing-user-item')
