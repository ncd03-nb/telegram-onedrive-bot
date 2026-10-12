import asyncio
import hashlib
import json
import socket
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.main import Bot
from app.settings import Settings
from app.source import ConfirmForm, PublicResolver, SourceError, Sources, clean_name, drive_id, filename, normalize, validate_url
from app.store import Store
from app.transfer import Transfer


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_api_uses_metadata_size_for_ten_gib(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "google-token.json"
            token.write_text("{}")
            sources = Sources(Settings(token="test", users={1}, google_token=token))
            sources.google_access = AsyncMock(return_value="fake-access")
            class Metadata:
                status = 200
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass
                async def json(self):
                    return {"name": "ROM.zip", "size": str(10 * 1024**3), "md5Checksum": "a" * 32, "mimeType": "application/zip", "capabilities": {"canDownload": True}}
            class API:
                def get(self, *args, **kwargs):
                    self.args, self.kwargs = args, kwargs
                    return Metadata()
            class Download:
                headers = {"Content-Type": "application/zip"}
                content_length = None
                url = "https://www.googleapis.com/drive/v3/files/abcDEF_123456789?alt=media"
                def release(self):
                    pass
            sources.api = API()
            sources.get = AsyncMock(return_value=Download())
            async with sources.open("https://drive.google.com/file/d/abcDEF_123456789/view?resourcekey=example") as (r, meta):
                self.assertEqual(meta["size"], 10 * 1024**3)
                self.assertEqual(meta["name"], "ROM.zip")
                self.assertEqual(meta["md5"], "a" * 32)
            self.assertEqual(sources.api.kwargs["headers"]["X-Goog-Drive-Resource-Keys"], "abcDEF_123456789/example")
            self.assertIn("alt=media", sources.get.await_args.args[0])
            self.assertEqual(sources.get.await_args.args[1]["Authorization"], "Bearer fake-access")

    async def test_google_api_rejects_missing_download_permission_before_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("{}")
            sources = Sources(Settings(token="test", users={1}, google_token=token))
            sources.google_access = AsyncMock(return_value="fake-access")
            class Metadata:
                status = 200
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass
                async def json(self):
                    return {"name": "ROM.zip", "size": "100", "capabilities": {"canDownload": False}}
            sources.api = type("API", (), {"get": lambda *args, **kwargs: Metadata()})()
            sources.get = AsyncMock()
            with self.assertRaisesRegex(SourceError, "tắt quyền"):
                async with sources.open("https://drive.google.com/file/d/abcDEF_123456789/view"):
                    pass
            sources.get.assert_not_awaited()

    def test_private_and_invalid_urls(self):
        for url in ["http://localhost/a", "http://127.0.0.1", "http://169.254.169.254/latest", "http://10.0.0.1", "http://[::1]/", "http://[::ffff:192.168.1.1]", "http://224.0.0.1", "http://user:pass@example.com", "http://example.com:8080", "file:///etc/passwd"]:
            with self.subTest(url=url), self.assertRaises(SourceError):
                validate_url(url)
        self.assertEqual(validate_url("https://example.com/file"), "https://example.com/file")

    async def test_mixed_dns_rejected(self):
        resolver = PublicResolver()
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=addresses)):
            with self.assertRaises(OSError):
                await resolver.resolve("example.com", 443)

    def test_drive_and_dropbox(self):
        url = "https://drive.google.com/file/d/abcDEF_123456789/view?resourcekey=resource"
        self.assertEqual(drive_id(url), "abcDEF_123456789")
        self.assertIn("resourcekey=resource", normalize(url))
        self.assertIn("dl=1", normalize("https://www.dropbox.com/s/test/a.zip?dl=0"))
        with self.assertRaises(SourceError):
            drive_id("https://drive.google.com/drive/folders/abcDEF_123456789")
        with self.assertRaises(SourceError):
            normalize("https://mega.nz/file/test")

    def test_filename_and_form(self):
        self.assertEqual(filename("attachment; filename*=UTF-8''rom%20test.zip", "https://example.com"), "rom test.zip")
        self.assertNotIn("/", clean_name("../../bad:name.zip"))
        form = ConfirmForm()
        form.feed('<form id="download-form" action="https://drive.usercontent.google.com/download"><input name="id" value="abc123"><input name="uuid" value="token"></form>')
        self.assertEqual(form.fields, {"id": "abc123", "uuid": "token"})

    async def test_redirect_private_address_rejected(self):
        sources = Sources(Settings(token="test", users={1}))
        class Redirect:
            status = 302
            headers = {"Location": "http://169.254.169.254/latest/meta-data"}
            def release(self):
                pass
        sources.session = type("Session", (), {"get": AsyncMock(return_value=Redirect())})()
        with self.assertRaises(SourceError):
            await sources.get("https://example.com/file")
        self.assertEqual(sources.session.get.await_count, 1)


class StoreTests(unittest.TestCase):
    def test_dedup_ownership_limit_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = Store(path)
            update = {"update_id": 22, "message": {"text": "secret-link"}}
            store.ingest(update)
            store.ingest(update)
            self.assertEqual(store.next_update(), update)
            self.assertIsNone(store.next_update())
            j = store.add(22, 1, 1, "https://example.com/file")
            self.assertEqual(j["id"], store.add(22, 1, 1, "https://example.com/file")["id"])
            self.assertIsNone(store.get(j["id"], 2))
            store.add(23, 1, 1, "https://example.com/file")
            store.add(24, 1, 1, "https://example.com/file")
            with self.assertRaises(ValueError):
                store.add(25, 1, 1, "https://example.com/file")
            self.assertEqual(store.claim()["state"], "running")
            store.close()
            store = Store(path)
            self.assertEqual(store.get(j["id"], 1)["state"], "interrupted")
            self.assertEqual(store.next_update(), update)
            store.finish_update(22)
            self.assertEqual(store.db.execute("SELECT payload FROM updates WHERE id=22").fetchone()[0], "")
            store.ingest(update)
            self.assertIsNone(store.next_update())
            store.close()


class Content:
    def __init__(self, data, slow=False):
        self.data = data
        self.slow = slow
    async def iter_chunked(self, chunk):
        for index in range(0, len(self.data), 4096):
            yield self.data[index:index + 4096]
            if self.slow:
                await asyncio.sleep(10)


class MemorySources:
    def __init__(self, data, total, md5=None, slow=False):
        self.data, self.total, self.md5, self.slow = data, total, md5, slow
    @asynccontextmanager
    async def open(self, url):
        yield type("Response", (), {"content": Content(self.data, self.slow)})(), {"name": "rom.zip", "size": self.total, "md5": self.md5}


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = self.root / "config"
        self.config.write_text("ok")
        self.s = Settings(token="test", users={1}, config=self.config, data_dir=self.root)
        self.job = {"url": "https://example.com/rom.zip", "id": "123abc", "requested_name": ""}
        self.processes = []
        real_exec = asyncio.create_subprocess_exec
        async def fake_exec(*args, **kwargs):
            proc = await real_exec(sys.executable, str(Path(__file__).with_name("fake_rclone.py")), *args[1:], **kwargs)
            self.processes.append(proc)
            return proc
        self.patch = patch("app.transfer.asyncio.create_subprocess_exec", fake_exec)
        self.patch.start()
        self.progress = AsyncMock()

    async def asyncTearDown(self):
        self.patch.stop()
        for proc in self.processes:
            self.assertIsNotNone(proc.returncode, "Subprocess leaked")
        self.directory.cleanup()

    async def test_stream_bytes_md5_and_destination_size(self):
        data = bytes(range(256)) * 8192
        transfer = Transfer(self.s, MemorySources(data, len(data), hashlib.md5(data).hexdigest()))
        destination = await transfer.copy(self.job, self.progress)
        self.assertEqual(destination, "onedrive:TelegramUploads/123abc/rom.zip")
        self.assertEqual((self.root / "output/TelegramUploads/123abc/rom.zip").read_bytes(), data)
        self.assertEqual(self.progress.await_args.args[:2], (len(data), len(data)))

    async def test_truncated_source_rejected(self):
        with self.assertRaisesRegex(SourceError, "chưa đủ"):
            await Transfer(self.s, MemorySources(b"short", 10000)).copy(self.job, self.progress)

    async def test_md5_mismatch_rejected(self):
        with self.assertRaisesRegex(SourceError, "MD5"):
            await Transfer(self.s, MemorySources(b"abc", 3, "0" * 32)).copy(self.job, self.progress)

    async def test_wrong_destination_size_rejected(self):
        self.config.write_text("wrongsize")
        with self.assertRaisesRegex(SourceError, "Dung lượng"):
            await Transfer(self.s, MemorySources(b"abc", 3)).copy(self.job, self.progress)

    async def test_upload_error_is_sanitized(self):
        self.config.write_text("upload-fail")
        with self.assertRaisesRegex(SourceError, "OAuth"):
            await Transfer(self.s, MemorySources(b"abc", 3)).copy(self.job, self.progress)

    async def test_unknown_size_spool_and_cleanup(self):
        data = b"hello" * 1000
        await Transfer(self.s, MemorySources(data, None)).copy(self.job, self.progress)
        self.assertEqual(list(self.root.glob("spool-*")), [])

    async def test_unknown_size_exceed_limit(self):
        self.s.spool_bytes = 2
        with self.assertRaisesRegex(SourceError, "lưu tạm"):
            await Transfer(self.s, MemorySources(b"abc", None)).copy(self.job, self.progress)
        self.assertEqual(list(self.root.glob("spool-*")), [])

    async def test_cancel_terminates_rclone(self):
        task = asyncio.create_task(Transfer(self.s, MemorySources(b"abc", 3, slow=True)).copy(self.job, self.progress))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.bot = Bot(Settings(token="test", users={1}, data_dir=Path(self.directory.name), mode="webhook", webhook_secret="secret"))
        self.bot.telegram.send = AsyncMock()
    async def asyncTearDown(self):
        self.bot.store.close()
        self.directory.cleanup()

    def test_unapproved_users_and_groups_ignored(self):
        for user, kind in [(2, "private"), (1, "group")]:
            self.bot.ingest({"update_id": user, "message": {"from": {"id": user}, "chat": {"id": user, "type": kind}}})
        self.assertIsNone(self.bot.store.next_update())

    async def test_cancel_is_scoped_to_owner(self):
        job = self.bot.store.add(100, 2, 2, "https://example.com/file")
        update = {"update_id": 200, "message": {"text": "/cancel " + job["id"], "from": {"id": 1}, "chat": {"id": 1, "type": "private"}}}
        await self.bot.handle(update)
        self.assertEqual(self.bot.store.get(job["id"], 2)["state"], "queued")

    async def test_cancel_during_start_notification_does_not_upload(self):
        job = self.bot.store.add(100, 1, 1, "https://example.com/file")
        self.bot.transfer.copy = AsyncMock()
        async def cancel_on_notify(chat, text):
            self.bot.store.change(job["id"], state="cancelled")
        self.bot.notify = cancel_on_notify
        worker = asyncio.create_task(self.bot.worker())
        await asyncio.sleep(0.05)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.bot.transfer.copy.assert_not_awaited()

    async def test_webhook_auth_and_dedup(self):
        app = web.Application()
        app.router.add_post("/telegram", self.bot.webhook)
        async with TestClient(TestServer(app)) as client:
            update = {"update_id": 88, "message": {"text": "https://example.com/file", "from": {"id": 1}, "chat": {"id": 1, "type": "private"}}}
            self.assertEqual((await client.post("/telegram", json=update)).status, 403)
            for _ in range(2):
                self.assertEqual((await client.post("/telegram", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "secret"})).status, 200)
            self.assertEqual(self.bot.store.next_update()["update_id"], 88)
            self.assertIsNone(self.bot.store.next_update())


if __name__ == "__main__":
    unittest.main()
