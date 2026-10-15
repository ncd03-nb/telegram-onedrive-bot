"""Copy an authorized source to a public Archive item using bounded multipart."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .archive import Archive,ArchiveError,public_download
from .exchange import load_job_env,write_result
from .failover import notify_result
from .progress import Progress
from .settings import Settings
from .source import Sources,SourceError,clean_name,validate_url
from .telegram import Telegram


async def wait_ready(client,file,identifier,progress,timeout=1500):
    started=time.monotonic()
    while time.monotonic()-started<timeout:
        async with client.session.get('https://archive.org/metadata/'+identifier,allow_redirects=False) as r:
            if r.status==200:
                raw=await r.content.read(1024**2+1)
                if len(raw)>1024**2:raise ArchiveError('Archive metadata too large')
                obj=json.loads(raw)
            else:obj={}
        found=next((v for v in obj.get('files',[]) if v.get('name')==file['name']),None)
        if found:
            if int(found.get('size',-1))!=file['size'] or found.get('md5')!=file['md5']:
                raise SourceError('Checksum/dung lượng file Archive không khớp.')
            url='https://archive.org/download/'+identifier+'/'+quote(file['name'])
            async with await public_download(client.session,url,{'Range':'bytes=0-31'}) as r:
                data=await r.content.read(33)
                with Path(file['path']).open('rb') as f:expected=f.read(32)
                if r.status==206 and data==expected and r.headers.get('Content-Range','').startswith('bytes 0-31/'):
                    return url
        progress.phase='Internet Archive đang xử lý file — chờ link tải'
        await asyncio.sleep(15)
    raise SourceError('Archive đã nhận upload nhưng chưa có link tải sau 25 phút; kiểm tra /status trước khi gửi lại.')


async def run():
    load_job_env();os.environ['STORAGE_BACKEND']='archive';s=Settings.load()
    jid=os.environ['JOB_ID'];owner=int(os.environ['JOB_OWNER']);chat=int(os.environ['JOB_CHAT']);message=int(os.environ['JOB_MESSAGE_ID'])
    if not re.fullmatch(r'[a-f0-9]{12}',jid) or owner not in s.users or chat!=owner or message<=0:raise ValueError('Invalid workflow identity')
    tg=Telegram(s.token);sources=Sources(s);await tg.start();await sources.start()
    progress=Progress(tg,chat,message,jid);progress.phase='Đang tải file nguồn';await progress.start()
    result={'id':jid,'owner':owner,'state':'failed','storage':'archive'}
    identifier='odrivegay-'+jid+'-'+uuid.uuid4().hex[:16]
    try:
        async with asyncio.timeout(19800):
            with tempfile.TemporaryDirectory(prefix=jid+'-',dir=s.data_dir) as directory:
                async with sources.open(validate_url(os.environ['JOB_SOURCE_URL'])) as (response,meta):
                    name=clean_name(os.getenv('JOB_REQUESTED_NAME') or meta['name']);path=Path(directory)/name
                    if meta['size'] is not None and shutil.disk_usage(directory).free<meta['size']+512*1024**2:raise SourceError('Runner không đủ ổ đĩa để giữ file nguồn.')
                    sha=hashlib.sha256();md5=hashlib.md5();count=0
                    with path.open('wb') as f:
                        async for chunk in response.content.iter_chunked(1024**2):
                            count+=len(chunk)
                            if count>s.max_bytes or meta['size'] is not None and count>meta['size']:raise SourceError('Nguồn vượt dung lượng cho phép.')
                            if shutil.disk_usage(directory).free<len(chunk)+128*1024**2:raise SourceError('Runner không đủ ổ đĩa.')
                            f.write(chunk);sha.update(chunk);md5.update(chunk)
                            await progress.update(count,meta['size'],name)
                    if not count or meta['size'] is not None and count!=meta['size']:raise SourceError('File nguồn chưa tải đủ.')
                    if meta.get('md5') and md5.hexdigest().lower()!=meta['md5'].lower():raise SourceError('Checksum file Drive không khớp.')
                file={'path':str(path),'name':name,'size':count,'md5':md5.hexdigest(),'sha256':sha.hexdigest()}
                progress.phase='Đang upload Internet Archive — 8 luồng';progress.started=time.monotonic();await progress.update(0,count,name)
                async def uploaded(done,total):await progress.update(done,total,name)
                async with Archive(os.environ['IA_ACCESS_KEY'],os.environ['IA_SECRET_KEY'],workers=8,part_mib=16) as archive:
                    info=await archive.multipart(file,identifier,uploaded,collection='opensource_media')
                    progress.phase='Internet Archive đang xử lý file — chờ link tải'
                    # Persist committed identity even if ingestion or Telegram later fails.
                    completed=time.time()
                    record={'version':1,'id':jid,'owner':owner,'chat':chat,'destination':s.destination+'/'+jid+'/'+name,
                        'size':count,'completed':completed,'expires':completed+43200,'file_id':identifier,'etag':file['md5'],'link_ctag':file['sha256']}
                    result.update(state='done',record=record,manifest=True,archive={'identifier':identifier,'name':name,'md5':file['md5'],'sha256':file['sha256'],'url':info['url']},metrics=info,links_notified=False)
                    write_result(Path(os.environ.get('RESULT_PATH','result.json')),result,s.token)
                    await wait_ready(archive,file,identifier,progress)
                await progress.close(finished=True)
    except asyncio.CancelledError:
        if result['state']!='done':result.update(state='cancelled',error='Đã hủy upload Archive.')
        await progress.close('Đã dừng '+jid+'; file đã upload sẽ giữ lịch xóa.');raise
    except Exception as error:
        result['error']=str(error) if isinstance(error,SourceError) else 'Upload Archive thất bại; kiểm tra nguồn, quyền hoặc trạng thái Archive.'
        await progress.close(('⏳ Archive đã nhận file; bot tiếp tục kiểm tra link tải.\n' if result['state']=='done' else '❌ ')+jid+' — '+result['error'])
    finally:
        write_result(Path(os.environ.get('RESULT_PATH','result.json')),result,s.token)
        await sources.close();await tg.close();await notify_result(s,jid)
    return 0 if result['state']=='done' else 1


async def main():
    task=asyncio.create_task(run())
    for sig in (signal.SIGTERM,signal.SIGINT):asyncio.get_running_loop().add_signal_handler(sig,task.cancel)
    return await task


if __name__=='__main__':
    try:raise SystemExit(asyncio.run(main()))
    except asyncio.CancelledError:raise SystemExit(130)
