"""One workflow job: stream cloud -> OneDrive, notify Telegram, persist TTL."""
import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path

from .main import link_message, link_keyboard
from .onedrive import OneDrive
from .progress import Progress
from .retention import Retention
from .settings import Settings
from .source import SourceError, Sources, validate_url
from .store import Store
from .telegram import Telegram
from .transfer import Transfer
from .failover import notify_result
from .exchange import load_job_env, write_result


async def run():
    load_job_env()
    s = Settings.load()
    job_id = os.environ['JOB_ID']
    owner, chat = int(os.environ['JOB_OWNER']), int(os.environ['JOB_CHAT'])
    message_id = int(os.environ['JOB_MESSAGE_ID'])
    if not re.fullmatch(r'[a-f0-9]{12}', job_id) or owner not in s.users or chat != owner or message_id <= 0:
        raise ValueError('Invalid workflow identity')
    store = Store(s.data_dir / 'runner.sqlite3')
    job = store.add(1, owner, chat, validate_url(os.environ['JOB_SOURCE_URL']), os.environ.get('JOB_REQUESTED_NAME', ''))
    with store.db:
        store.db.execute('UPDATE jobs SET id=? WHERE id=?', (job_id, job['id']))
    job = store.get(job_id, owner)
    sources, telegram = Sources(s), Telegram(s.token)
    transfer = Transfer(s, sources)
    drive = OneDrive(s, transfer)
    retention = Retention(s, store, drive, transfer)
    progress = Progress(telegram, chat, message_id, job_id)
    result = {'id': job_id, 'state': 'failed', 'error': 'Runner không hoàn tất; kiểm tra thư mục đích.'}
    await sources.start()
    await telegram.start()
    await drive.start()
    await progress.start()
    try:
        async def update(count, total, dest):
            store.change(job_id, bytes=count, total=total, destination=dest)
            await progress.update(count, total, dest)
        dest = await transfer.copy(job, update)
        now = time.time()
        file_id = transfer.verified.pop(dest, {}).get('ID', '').rsplit('#', 1)[-1]
        store.change(job_id, state='done', url='', completed=now, expires=now + s.retention_hours * 3600,
            destination=dest, file_id=file_id, retention='pending')
        item = None
        try:
            item = await retention.arm(store.get(job_id, owner))
        except Exception:
            store.change(job_id, cleanup_error='Lịch xóa đang chờ host bot xác minh.', links_error='Dùng /link để lấy link lại.')
        current = store.get(job_id, owner)
        await progress.close(finished=True)
        notified = False
        if item:
            try:
                await telegram.edit(chat, message_id, link_message(current, item, settings=s),
                    reply_markup=link_keyboard(s, current, item))
                notified = True
            except Exception:
                pass
        result = {'id': job_id, 'state': 'done', 'record': retention.record(current),
            'manifest': current['manifest'], 'retention': current['retention'],
            'links_notified': notified, 'cleanup_error': current['cleanup_error']}
    except asyncio.CancelledError:
        result = {'id': job_id, 'state': 'cancelled', 'error': 'Workflow đã hủy; kiểm tra thư mục đích nếu upload đã commit.'}
        await progress.close(f"{job_id} — Đã hủy")
        raise
    except Exception as e:
        error = str(e) if isinstance(e, SourceError) else ('Nguồn hoặc upload quá thời gian; kiểm tra mạng / quota rồi thử lại.' if isinstance(e, TimeoutError) else 'Lỗi nguồn / upload; kiểm tra quyền và quota.')
        result = {'id': job_id, 'state': 'failed', 'error': error}
        await progress.close(f"{job_id} — Thất bại\n{error}")
    finally:
        output = Path(os.environ.get('RESULT_PATH', 'result.json'))
        output.parent.mkdir(parents=True, exist_ok=True)
        write_result(output,result,s.token)
        await sources.close()
        await telegram.close()
        await drive.close()
        store.close()
        await notify_result(s, job_id)
    return 0 if result['state'] == 'done' else 1


async def main():
    task = asyncio.create_task(run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    return await task


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except asyncio.CancelledError:
        raise SystemExit(130)
