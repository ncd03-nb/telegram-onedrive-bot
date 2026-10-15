"""Bounded IAS3 multipart uploader; never forwards credentials outside Archive."""
import asyncio
import base64
import hashlib
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import aiohttp


class ArchiveError(Exception):
    pass


def safe_url(value):
    p = urlsplit(value)
    if (p.scheme != 'https' or not p.hostname or
            not (p.hostname == 'archive.org' or p.hostname.endswith('.archive.org'))
            or p.username or p.password or p.port not in (None, 443)):
        raise ArchiveError('Refused non-Archive HTTPS destination')
    return value


def xml_value(raw, name):
    root = ET.fromstring(raw)
    return next((e.text for e in root.iter() if e.tag.rsplit('}', 1)[-1] == name), None)


def completion(parts):
    root = ET.Element('CompleteMultipartUpload')
    for number, digest in sorted(parts.items()):
        part = ET.SubElement(root, 'Part')
        ET.SubElement(part, 'PartNumber').text = str(number)
        ET.SubElement(part, 'ETag').text = '"'+digest+'"'
    return ET.tostring(root, encoding='utf-8')


class Archive:
    def __init__(self, access, secret, workers=4, part_mib=32):
        if not 1 <= workers <= 16 or not 5 <= part_mib <= 64 or workers*part_mib > 256:
            raise ValueError('Invalid multipart bounds')
        if not access or not secret or any(c in access+secret for c in '\r\n:'):
            raise ValueError('Invalid Archive credentials')
        self.auth = 'LOW '+access+':'+secret
        self.workers, self.part_bytes = workers, part_mib*1024**2
        self.routes={};self.redirects=0

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240,connect=30,sock_read=180),
            connector=aiohttp.TCPConnector(limit=18),trust_env=False,
            headers={'User-Agent':'TelegramBot-IAS3Multipart/1.0'})
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def request(self, method, url, data=None, headers=None, sent=None):
        original=urlsplit(safe_url(url));route_key=urlunsplit(original._replace(query='',fragment=''))
        if route_key in self.routes:
            target=urlsplit(self.routes[route_key]);url=urlunsplit(target._replace(query=original.query))
        for _ in range(5):
            safe_url(url)
            async def chunks():
                for start in range(0,len(data),1024**2):
                    chunk=data[start:start+1024**2];yield chunk
                    await sent(start+len(chunk))
            streamed=sent is not None and isinstance(data,bytes)
            async with self.session.request(method,url,data=chunks() if streamed else data,allow_redirects=False,
                    expect100=method=='PUT' and isinstance(data,bytes),
                    headers={'Authorization':self.auth,**({'Content-Length':str(len(data))} if streamed else {}),**(headers or {})}) as r:
                if r.status in (307,308):
                    url=safe_url(urljoin(url,r.headers.get('Location','')));self.redirects+=1;continue
                raw=await r.content.read(1048577)
                if len(raw)>1048576:raise ArchiveError('Oversized IAS3 response')
                if r.status not in (200,201,204):raise ArchiveError('IAS3 HTTP '+str(r.status))
                if raw.lstrip().startswith(b'<') and xml_value(raw,'Code'):
                    raise ArchiveError('IAS3 reported an XML error')
                target=urlsplit(url)
                if original.path==target.path and original.query==target.query:
                    self.routes[route_key]=urlunsplit(target._replace(query='',fragment=''))
                return raw
        raise ArchiveError('IAS3 redirect limit')

    async def multipart(self,file,identifier,progress=None,collection='test_collection'):
        import re
        if collection not in {'test_collection','opensource_media'} or not re.fullmatch(r'(?:bot-upload-test-[a-f0-9]{32}|odrivegay-[a-f0-9]{12}-[a-f0-9]{16})',identifier):
            raise ValueError('Invalid Archive item identity')
        if collection!='test_collection' and not identifier.startswith('odrivegay-'):raise ValueError('Invalid production item')
        path=Path(file['path']);size=file['size']
        if size<=0 or path.stat().st_size!=size:raise ValueError('Invalid source size')
        url='https://s3.us.archive.org/'+identifier+'/'+quote(path.name)
        meta={'x-archive-auto-make-bucket':'1','x-archive-queue-derive':'0',
            'x-archive-meta-collection':collection,'x-archive-meta-mediatype':'data',
            'x-archive-meta-title':'uri('+quote(path.name)+')','x-archive-size-hint':str(size)}
        if size<=self.part_bytes and collection!='test_collection':
            data=await asyncio.to_thread(path.read_bytes)
            digest=hashlib.md5(data)
            if digest.hexdigest()!=file['md5']:raise ArchiveError('Source checksum changed')
            async def sent(count):
                if progress:await progress(count,size)
            await self.request('PUT',url,data,{**meta,'Content-Type':'application/octet-stream','Content-MD5':base64.b64encode(digest.digest()).decode()},sent=sent)
            if progress:await progress(size,size)
            return {'identifier':identifier,'url':'https://archive.org/download/'+identifier+'/'+quote(path.name),'parts':1,'part_mib':self.part_bytes//1024**2,'workers':1}
        started=time.monotonic()
        raw=await self.request('POST',url+'?uploads',headers=meta)
        initiated=time.monotonic()
        uid=xml_value(raw,'UploadId')
        if not uid or len(uid)>1024:raise ArchiveError('IAS3 did not return an upload ID')
        query='uploadId='+quote(uid,safe='')
        count=(size+self.part_bytes-1)//self.part_bytes
        queue=iter(range(1,count+1));parts={};inflight={};done=0;committed=False
        def load(number):
            with path.open('rb') as f:
                f.seek((number-1)*self.part_bytes)
                return f.read(min(self.part_bytes,size-(number-1)*self.part_bytes))
        async def worker():
            nonlocal done
            for number in queue:
                data=await asyncio.to_thread(load,number)
                digest=hashlib.md5(data)
                async def sent(count):
                    inflight[number]=max(inflight.get(number,0),count)
                    if progress:await progress(sum(inflight.values()),size)
                for attempt in range(3):
                    try:
                        await self.request('PUT',url+'?partNumber='+str(number)+'&'+query,data,
                            {'Content-Type':'application/octet-stream','Content-MD5':base64.b64encode(digest.digest()).decode()},sent=sent)
                        break
                    except (ArchiveError,aiohttp.ClientError,TimeoutError):
                        if attempt==2:raise
                        await asyncio.sleep(2**(attempt+1))
                parts[number]=digest.hexdigest();done+=len(data)
                inflight[number]=len(data)
                if progress:await progress(sum(inflight.values()),size)
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(self.workers,count)):group.create_task(worker())
            if len(parts)!=count:raise ArchiveError('Missing multipart results')
            parts_finished=time.monotonic()
            await self.request('POST',url+'?'+query,completion(parts),
                {'Content-Type':'application/xml','x-archive-queue-derive':'0'})
            committed=True
            return {'identifier':identifier,'url':'https://archive.org/download/'+identifier+'/'+quote(path.name),
                    'parts':count,'part_mib':self.part_bytes//1024**2,'workers':self.workers,
                    'init_seconds':round(initiated-started,3),'parts_seconds':round(parts_finished-initiated,3),
                    'parts_mib_s':round(size/1024**2/max(.001,parts_finished-initiated),2),
                    'commit_seconds':round(time.monotonic()-parts_finished,3),'redirects':self.redirects}
        finally:
            if not committed:
                try:
                    async with asyncio.timeout(20):await self.request('DELETE',url+'?'+query)
                except Exception:pass

    async def cleanup(self,file,identifier):
        # Caller only supplies an identifier freshly created by multipart().
        import re
        if not re.fullmatch(r'bot-upload-test-[a-f0-9]{32}',identifier):raise ValueError('Invalid sandbox ID')
        url='https://s3.us.archive.org/'+identifier+'/'+quote(Path(file['path']).name)
        async with asyncio.timeout(25):
            try:
                await self.request('DELETE',url,headers={'x-archive-keep-old-version':'1','x-archive-queue-derive':'0'})
            except ArchiveError as error:
                if str(error)!='IAS3 HTTP 404':raise


async def public_download(session,url,headers=None):
    for _ in range(6):
        safe_url(url)
        r=await session.get(url,headers=headers,allow_redirects=False)
        if r.status in (301,302,303,307,308):
            target=urljoin(url,r.headers.get('Location',''));r.release();url=safe_url(target);continue
        return r
    raise ArchiveError('Download redirect limit')


async def verify_download(session,file,url,wait=180):
    started=time.monotonic()
    while time.monotonic()-started<wait:
        async with await public_download(session,url,{'Range':'bytes=0-31','Cache-Control':'no-cache'}) as r:
            data=await r.content.read(33)
            with Path(file['path']).open('rb') as local:expected=local.read(32)
            ranged=r.status==206 and data==expected and r.headers.get('Content-Range','').startswith('bytes 0-31/')
        if ranged:
            ready=time.monotonic()-started;sha=hashlib.sha256();md5=hashlib.md5();size=0;download_started=time.monotonic()
            async with await public_download(session,url) as r:
                if r.status!=200:raise ArchiveError('Download HTTP '+str(r.status))
                async for chunk in r.content.iter_chunked(4*1024**2):
                    size+=len(chunk)
                    if size>file['size']:raise ArchiveError('Oversized download')
                    sha.update(chunk);md5.update(chunk)
            verified=size==file['size'] and sha.hexdigest()==file['sha256'] and md5.hexdigest()==file['md5']
            if not verified:raise ArchiveError('Downloaded checksum mismatch')
            return {'verified':True,'range_ok':True,'ready_seconds':round(ready,3),
                    'download_mib_s':round(size/1024**2/max(.001,time.monotonic()-download_started),2)}
        await asyncio.sleep(10)
    return {'verified':False,'range_ok':False,'ready_seconds':round(time.monotonic()-started,3)}
