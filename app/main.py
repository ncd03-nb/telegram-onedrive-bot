import asyncio
import hmac
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web
import aiohttp

from .settings import Settings
from .source import SourceError, Sources, clean_name, validate_url
from .store import Store
from .telegram import Telegram
from .transfer import Transfer
from .onedrive import OneDrive, GraphError
from .retention import Retention
from .actions import Actions
from .progress import Progress
from .downloads import Downloads, download_url

log = logging.getLogger("bot")
LOCAL_TZ = timezone(timedelta(hours=7))

HELP = """Gửi link Google Drive / Dropbox / link tải trực tiếp.
/copy <link> [tên file mới]
/status [mã tác vụ] — xem tiến độ
/cancel <mã tác vụ> — hủy
/find <từ khóa> — tìm file bot đã upload
/link <mã tác vụ> — lấy link file + link tải mới
/dl <mã tác vụ> — lấy link tải trực tiếp mới
/check — kiểm tra quota OneDrive
/whoami — xem Telegram user ID
Chỉ nhận chat riêng, người trong ALLOWED_USER_IDS.
File nằm trong TelegramUploads/<mã tác vụ>/.
File tự chuyển vào thùng rác sau 12h từ lúc upload xong khi bot online.
Drive cần chia sẻ quyền tải cho tài khoản Google đã kết nối; link công khai cũng dùng được nếu Google cho phép tải."""


def size(value):
    return "?" if value is None else f"{value / 1024**3:.2f} GiB"


def describe(j):
    state = {"queued": "Đang chờ", "running": "Đang chuyển", "done": "Hoàn tất",
             "cancelled": "Đã hủy", "failed": "Thất bại", "interrupted": "Bị ngắt", "deleted": "Đã xóa / không còn ở vị trí cũ"}[j["state"]]
    text = f"{j['id']} — {state}\nĐã đọc từ nguồn: {size(j['bytes'])} / {size(j['total'])}"
    if j["destination"]:
        text += "\nĐích: " + j["destination"].split(":", 1)[-1]
    if j["error"]:
        text += "\n" + j["error"]
    if j.get("backend") == "actions":
        text += "\nRunner: GitHub Actions — " + (j.get("phase") or "đang chờ runner")
        text += "\nTiến độ % được cập nhật trên tin nhắn tác vụ mỗi 15 giây."
    if j.get("expires"):
        when = datetime.fromtimestamp(j["expires"], LOCAL_TZ).strftime("%d/%m %H:%M GMT+7")
        text += "\nHạn lưu: " + when
    if j.get("cleanup_error"):
        text += "\n" + j["cleanup_error"]
    if j.get("links_error"):
        text += "\n" + j["links_error"]
    return text


def link_message(job, item, direct_only=False, settings=None):
    direct = item.get("@microsoft.graph.downloadUrl", "")
    web_url = item.get("webUrl", "")
    if not direct or not web_url:
        raise SourceError("OneDrive chưa trả đủ link; dùng /link để thử lại.")
    when = datetime.fromtimestamp(job["expires"], LOCAL_TZ).strftime("%d/%m %H:%M GMT+7")
    text = f"{job['id']} — {item.get('name', 'File')}\n"
    if not direct_only:
        text += "Link file:\n" + web_url + "\n\n"
    custom = download_url(settings, job) if settings else ''
    text += "Tải trực tiếp:\n" + (custom or direct)
    text += ("\n\nLink có hiệu lực đến hạn lưu file. Ai có link đều có thể tải."
             if custom else "\n\nLink tải tạm thời. Hết hạn dùng /dl " + job["id"])
    text += "\nHạn lưu file: " + when
    if job.get("cleanup_error"):
        text += "\n" + job["cleanup_error"]
    return text


def link_keyboard(settings, job, item, direct_only=False):
    custom = download_url(settings, job)
    rows = [[{'text': '⚡ Index Link' if custom else '⬇ Tải trực tiếp',
              'url': custom or item['@microsoft.graph.downloadUrl']}]]
    if not direct_only:
        rows.append([{'text': '📁 Mở file OneDrive', 'url': item['webUrl']}])
    return {'inline_keyboard': rows}


class Bot:
    def __init__(self, settings):
        self.s = settings
        self.store = Store(settings.data_dir / "jobs.sqlite3", recover=False)
        self.telegram = Telegram(settings.token)
        self.sources = Sources(settings)
        self.transfer = Transfer(settings, self.sources)
        self.drive = OneDrive(settings, self.transfer, busy=lambda: bool(self.active_id) and self.s.transfer_backend == "local")
        self.retention = Retention(settings, self.store, self.drive, self.transfer)
        self.downloads = Downloads(settings, self.drive, self.retention)
        self.actions = Actions(settings)
        self.tasks = []
        self.active_id = None
        self.active_task = None
        self.update_event = asyncio.Event()
        self.job_event = asyncio.Event()
        self.check_lock = asyncio.Lock()
        self.last_check = 0
        self.lock = None

    def acquire_lock(self):
        self.lock = (self.s.data_dir / "instance.lock").open("a+b")
        try:
            import fcntl
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt
            self.lock.seek(0)
            if not self.lock.read(1):
                self.lock.write(b"0")
                self.lock.flush()
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)

    async def start(self, app):
        self.acquire_lock()
        self.store.recover()
        if self.s.mode == "standby":
            if self.s.download_base_url:
                await self.drive.start()
            log.info("Host dự phòng sẵn sàng; chưa nhận Telegram updates.")
            return
        if not shutil.which(self.s.rclone):
            raise ValueError("Không tìm thấy rclone. Chạy bằng Docker hoặc cài rclone.")
        await self.telegram.start()
        await self.sources.start()
        await self.drive.start()
        await self.actions.start()
        if self.s.mode == "polling":
            await self.telegram.call("deleteWebhook", drop_pending_updates=False)
        else:
            await self.telegram.call("setWebhook", url=self.s.webhook_url + "/telegram",
                secret_token=self.s.webhook_secret, allowed_updates=["message"],
                max_connections=1, drop_pending_updates=False)
        self.tasks = [asyncio.create_task(self.updates()), asyncio.create_task(self.worker()), asyncio.create_task(self.collector())]
        if self.s.mode == "polling":
            self.tasks.append(asyncio.create_task(self.poll()))
        log.info("Bot đang chạy: %s; chỉ cho phép %d user.", self.s.mode, len(self.s.users))

    async def close(self, app):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.active_task:
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)
        if self.s.mode != "standby":
            await self.sources.close()
            await self.telegram.close()
            await self.actions.close()
        await self.drive.close()
        self.store.close()
        if self.lock:
            self.lock.close()

    def accept(self, update):
        message = update.get("message", {})
        return message.get("from", {}).get("id") in self.s.users and message.get("chat", {}).get("type") == "private"

    def ingest(self, update):
        if isinstance(update.get("update_id"), int) and self.accept(update):
            self.store.ingest(update)
            self.update_event.set()

    async def poll(self):
        # Telegram will replay pending updates; SQLite deduplicates them after a restart.
        offset = 0
        while True:
            try:
                items = await self.telegram.call("getUpdates", offset=offset, timeout=30,
                    allowed_updates=["message"])
                for item in items:
                    self.ingest(item)
                    offset = item["update_id"] + 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Telegram polling thất bại (%s); thử lại sau 5 giây.", type(e).__name__)
                await asyncio.sleep(5)

    async def notify(self, chat, text):
        try:
            return await self.telegram.send(chat, text)
        except Exception as e:
            log.warning("Không gửi được thông báo Telegram (%s); /status vẫn lưu kết quả.", type(e).__name__)

    async def notify_links(self, chat, job, item, direct_only=False, prefix=''):
        return await self.telegram.send(chat, prefix + link_message(job, item, direct_only, self.s),
            reply_markup=link_keyboard(self.s, job, item, direct_only))

    async def updates(self):
        while True:
            update = self.store.next_update()
            if not update:
                self.update_event.clear()
                try:
                    await asyncio.wait_for(self.update_event.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self.handle(update)
            except asyncio.CancelledError:
                raise
            except (SourceError, ValueError) as e:
                await self.notify(update["message"]["chat"]["id"], str(e))
            except Exception as e:
                log.warning("Xử lý lệnh thất bại (%s).", type(e).__name__)
                await self.notify(update["message"]["chat"]["id"], "Lệnh thất bại; thử /status hoặc gửi lại lệnh.")
            self.store.finish_update(update["update_id"])

    async def handle(self, update):
        m = update["message"]
        owner, chat = m["from"]["id"], m["chat"]["id"]
        parts = m.get("text", "").strip().split(maxsplit=1)
        if not parts:
            await self.notify(chat, "Hãy gửi link tới file.")
            return
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if command in {"/start", "/help"}:
            await self.notify(chat, HELP)
        elif command == "/whoami":
            await self.notify(chat, f"Telegram user ID: {owner}")
        elif command == "/status":
            jobs = [self.store.get(args.strip(), owner)] if args else self.store.recent(owner)
            await self.notify(chat, "\n\n".join(describe(j) for j in jobs if j) or "Không có tác vụ.")
        elif command in {"/find", "/search"}:
            if not args.strip():
                await self.notify(chat, "Dùng /find <từ khóa trong tên file>.")
                return
            jobs = self.store.find(owner, args.strip())
            await self.notify(chat, "\n\n".join(describe(j) + "\nLấy link: /link " + j["id"] for j in jobs) or "Không tìm thấy file còn hạn do bạn upload qua bot.")
        elif command in {"/link", "/dl", "/download"}:
            job = self.store.get(args.strip(), owner)
            if not job or job["state"] != "done" or not job["expires"] or job["expires"] <= time.time():
                await self.notify(chat, "File không tồn tại, chưa hoàn tất hoặc đã hết hạn lưu. Dùng /find để tìm mã tác vụ.")
                return
            try:
                item = await self.drive.links(job["destination"])
            except GraphError as e:
                if e.status == 404:
                    await self.notify(chat, "File đã bị xóa hoặc chuyển khỏi vị trí bot quản lý.")
                    return
                raise
            if job["file_id"] and item["id"] != job["file_id"]:
                await self.notify(chat, "File tại vị trí này đã được thay thế; không trả link file khác.")
                return
            self.store.change(job["id"], file_url=item["webUrl"], links_error="")
            await self.notify_links(chat, job, item, command in {"/dl", "/download"})
        elif command == "/cancel":
            job = self.store.get(args.strip(), owner)
            if not job or job["state"] not in {"queued", "running"}:
                await self.notify(chat, "Không tìm thấy tác vụ đang chờ / chạy thuộc tài khoản bạn.")
                return
            if job["backend"] == "actions" and job["dispatch_at"]:
                run = await self.actions.find_run(job) if not job["run_id"] else {"id": job["run_id"]}
                if not run:
                    await self.notify(chat, "GitHub đang nhận workflow; đợi runner xuất hiện rồi /cancel lại để hủy cả upload.")
                    return
                await self.actions.cancel(run["id"])
            self.store.change(job["id"], state="cancelled", url="")
            if job["id"] == self.active_id and self.active_task:
                self.active_task.cancel()
            await self.notify(chat, "Đã hủy " + job["id"] + ". Nếu file đã được commit trước khi hủy, kiểm tra thư mục đích.")
        elif command == "/check":
            if self.active_id:
                await self.notify(chat, "Bot đang chuyển file. Chạy /check khi tác vụ kết thúc để tránh đọc / ghi OAuth cùng lúc.")
                return
            if self.check_lock.locked() or time.monotonic() - self.last_check < 30:
                await self.notify(chat, "Chờ 30 giây giữa các lần kiểm tra.")
                return
            async with self.check_lock:
                self.last_check = time.monotonic()
                obj = await self.transfer.check()
                await self.notify(chat, "OneDrive kết nối được.\nTổng: " + size(obj.get("total")) + "\nCòn trống: " + size(obj.get("free")))
        else:
            text = args if command == "/copy" else m.get("text", "").strip()
            fields = text.split(maxsplit=1)
            if not fields or not fields[0].startswith(("https://", "http://")):
                await self.notify(chat, HELP)
                return
            url = validate_url(fields[0])
            # Reject Drive folder links before adding a job.
            from .source import drive_id
            drive_id(url)
            name = clean_name(fields[1]) if len(fields) > 1 else ""
            job = self.store.add(update["update_id"], owner, chat, url, name, backend=self.s.transfer_backend)
            if not job["message_id"]:
                self.store.change(job['id'],phase='creating_message')
                sent = await self.notify(chat, job["id"] + " — Đang chờ runner\nTiến độ: 0%\nTin nhắn này sẽ cập nhật mỗi 15 giây khi bắt đầu chuyển.")
                if not sent or not isinstance(sent.get("message_id"), int):
                    self.store.change(job['id'],phase='queued')
                    raise SourceError("Chưa tạo được tin nhắn tiến độ; tác vụ đang chờ kết nối Telegram.")
                self.store.change(job["id"], message_id=sent["message_id"])
                self.store.change(job['id'],phase='queued')
            self.job_event.set()

    async def actions_job(self, job):
        try:
            if not job["message_id"]:
                sent = await self.telegram.send(job["chat"], job["id"] + " — Đang chờ GitHub Actions\nTiến độ: 0%")
                self.store.change(job["id"], message_id=sent["message_id"])
                job = self.store.get(job["id"], job["owner"])
            if not job["dispatch_at"]:
                # Persist BEFORE the request. Restart or ambiguous HTTP response must not duplicate uploads.
                self.store.change(job["id"], dispatch_at=time.time(), phase="dispatching")
                try:
                    response = await self.actions.dispatch(job)
                    if response.get("workflow_run_id"):
                        self.store.change(job["id"], run_id=response["workflow_run_id"])
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    pass  # Reconcile by unique run-name; never repeat POST automatically.
                self.store.change(job["id"], url="", phase="queued")
            result = await self.actions.observe(self.store.get(job["id"], job["owner"]), self.store)
            if not result:
                return
            if result['state'] == 'done':
                record = result['record']
                self.retention.validate_record({**record, 'etag': record.get('etag') or 'pending'}, job['id'])
                if record['owner'] != job['owner'] or record['chat'] != job['chat']:
                    raise SourceError('Kết quả Actions không khớp owner.')
                self.store.change(job['id'], state='done',url='',bytes=record['size'],total=record['size'],
                    destination=record['destination'],completed=record['completed'],expires=record['expires'],
                    file_id=record['file_id'],etag=record['etag'],retention='armed' if record['etag'] else 'pending',
                    manifest=int(bool(result.get('manifest'))),cleanup_error=result.get('cleanup_error',''),phase='completed')
                if not result.get('links_notified'):
                    try:
                        item = await self.retention.arm(self.store.get(job['id'], job['owner']))
                        if item:
                            await self.notify_links(job['chat'], self.store.get(job['id'],job['owner']), item, prefix='✅ Copy hoàn tất!\n')
                    except Exception:
                        await self.notify(job['chat'], '✅ ' + job['id'] + ' — Đã upload. Dùng /link ' + job['id'] + ' để lấy link.')
            else:
                self.store.change(job['id'],state=result['state'],url='',error=result.get('error','Workflow thất bại.'),phase='completed')
                await self.telegram.edit(job['chat'],job['message_id'],describe(self.store.get(job['id'],job['owner'])))
        except asyncio.CancelledError:
            raise  # The runner keeps going after a controller restart.
        except Exception as e:
            error = str(e) if isinstance(e, SourceError) else 'Không đọc được kết quả Actions; kiểm tra workflow và thư mục đích.'
            self.store.change(job['id'],state='failed',url='',error=error)
            await self.notify(job['chat'],job['id'] + ' — ' + error)

    async def worker(self):
        while True:
            if self.check_lock.locked():
                await asyncio.sleep(0.25)
                continue
            self.store.clean()
            job = self.store.claim()
            if not job:
                self.job_event.clear()
                try:
                    await asyncio.wait_for(self.job_event.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            self.active_id = job["id"]
            if job["backend"] == "actions":
                try:
                    await self.actions_job(job)
                finally:
                    self.active_id = None
                continue
            reporter = Progress(self.telegram,job['chat'],job['message_id'],job['id']) if job['message_id'] else None
            if reporter:
                await reporter.start()
            else:
                await self.notify(job['chat'],job['id'] + ' — Đang chuyển\nTiến độ: 0%')
            async def progress(count, total, dest):
                self.store.change(job["id"], bytes=count, total=total, destination=dest)
                if reporter:
                    await reporter.update(count,total,dest)
            if self.store.get(job["id"], job["owner"])["state"] == "cancelled":
                if reporter:
                    await reporter.close(job['id'] + ' — Đã hủy')
                self.active_id = None
                continue
            self.active_task = asyncio.create_task(self.transfer.copy(job, progress))
            item = None
            try:
                dest = await self.active_task
                completed = time.time()
                verified = self.transfer.verified.pop(dest, {})
                file_id = verified.get("ID", "").rsplit("#", 1)[-1]
                self.store.change(job["id"], state="done", destination=dest, url="", completed=completed,
                    expires=completed + self.s.retention_hours * 3600, file_id=file_id, retention="pending")
                # Upload remains successful if obtaining links or writing recovery metadata fails.
                try:
                    item = await self.retention.arm(self.store.get(job["id"], job["owner"]))
                except Exception as e:
                    log.warning("Đã upload %s; chưa chuẩn bị xong link/lịch xóa (%s).", job["id"], type(e).__name__)
                    self.store.change(job["id"], links_error="Đã upload; chưa lấy được link. Dùng /link khi OneDrive kết nối lại.",
                        cleanup_error="Lịch xóa đang chờ xác minh OneDrive; bot sẽ thử lại.")
            except asyncio.CancelledError:
                # A user cancellation cancels the transfer task, not the worker.
                if asyncio.current_task().cancelling():
                    if self.store.get(job["id"], job["owner"])["state"] != "cancelled":
                        self.store.change(job["id"], state="interrupted", error="Host đã dừng; kiểm tra thư mục đích trước khi gửi lại.", url="")
                    raise
            except SourceError as e:
                self.store.change(job["id"], state="failed", error=str(e), url="")
            except TimeoutError:
                self.store.change(job["id"], state="failed", error="Tác vụ vượt thời gian cho phép; kiểm tra thư mục đích.", url="")
            except Exception as e:
                log.warning("Tác vụ %s thất bại (%s).", job["id"], type(e).__name__)
                self.store.change(job["id"], state="failed", error="Lỗi kết nối hoặc nguồn bị ngắt; kiểm tra quyền / quota và thử lại.", url="")
            finally:
                self.active_task = None
                self.active_id = None
                if reporter:
                    current = self.store.get(job['id'],job['owner'])
                    await reporter.close(finished=True) if current['state']=='done' else await reporter.close(describe(current))
            current = self.store.get(job["id"], job["owner"])
            await self.notify(job["chat"], describe(current))
            if item:
                try:
                    await self.notify_links(job["chat"], current, item)
                except Exception:
                    self.store.change(job["id"], links_error="Chưa lấy được link tải; dùng /link để lấy lại.")

    async def collector(self):
        last_restore = 0
        while True:
            if not self.active_id and not self.check_lock.locked():
                async with self.check_lock:
                    if time.monotonic() - last_restore >= 300:
                        try:
                            await self.retention.restore()
                        except Exception as e:
                            log.warning("Chưa đọc được lịch xóa từ OneDrive (%s).", type(e).__name__)
                        last_restore = time.monotonic()
                    await self.retention.sweep()
            elif self.active_id:
                # Graph deletion/metadata uses the existing token and doesn't mutate rclone.conf.
                # Do not postpone an armed file's expiration behind a multi-hour upload.
                await self.retention.sweep()
            await asyncio.sleep(self.s.cleanup_interval)

    async def health(self, request):
        healthy = all(not task.done() for task in self.tasks)
        return web.json_response({"status": "ok" if healthy else "degraded", "mode": self.s.mode,
            "transfer_backend": self.s.transfer_backend,
            "custom_downloads": bool(self.s.download_base_url),
            "actions_configured": bool(self.s.github_token)}, status=200 if healthy else 503)

    async def webhook(self, request):
        if self.s.mode != "webhook":
            raise web.HTTPServiceUnavailable()
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(secret, self.s.webhook_secret):
            raise web.HTTPForbidden()
        try:
            obj = await request.json()
            if not isinstance(obj, dict):
                raise ValueError()
            self.ingest(obj)
        except (ValueError, TypeError, AttributeError):
            raise web.HTTPBadRequest() from None
        # Persist before responding so Telegram retries cannot enqueue duplicate jobs.
        return web.json_response({"ok": True})


def create_app(settings):
    bot = Bot(settings)
    app = web.Application(client_max_size=256 * 1024)
    app.router.add_get("/health", bot.health)
    app.router.add_post("/telegram", bot.webhook)
    app.router.add_get("/0:/{capability}/{name}", bot.downloads.handle)
    app.on_startup.append(bot.start)
    app.on_cleanup.append(bot.close)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load()
    web.run_app(create_app(settings), host="0.0.0.0", port=settings.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
