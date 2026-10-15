import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.main import Bot, link_message
from app.onedrive import GraphError, OneDrive
from app.retention import Retention
from app.settings import Settings
from app.source import SourceError
from app.store import Store


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = Settings(token="test", users={1}, data_dir=Path(self.temp.name))
        self.store = Store(self.s.data_dir / "state.db")
        self.drive = type("Drive", (), {})()
        for method in ("item", "recycle", "write_manifest", "delete_manifest"):
            setattr(self.drive, method, AsyncMock())
        self.transfer = type("Transfer", (), {"run_command": AsyncMock()})()
        self.retention = Retention(self.s, self.store, self.drive, self.transfer)
        self.job = self.store.add(22, 1, 1, "https://example.com/file")
        now = time.time()
        self.store.change(self.job["id"], state="done", url="", total=10, bytes=10, destination="onedrive:TelegramUploads/" + self.job["id"] + "/rom.zip",
            completed=now, expires=now + 43200, retention="pending", file_id="original-id")
        self.drive.item.return_value = {"id": "original-id", "eTag": '"v1"', "file": {}, "size": 10,
            "webUrl": "https://example.sharepoint.com/file", "@microsoft.graph.downloadUrl": "https://example.sharepoint.com/temp-download", "name": "rom.zip"}

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job_now(self):
        return self.store.get(self.job["id"], 1)

    async def test_arm_keeps_original_expiration_and_persists_manifest(self):
        before = self.job_now()["expires"]
        await self.retention.arm(self.job_now())
        current = self.job_now()
        self.assertEqual(current["expires"], before)
        self.assertEqual(current["retention"], "armed")
        self.assertEqual(current["manifest"], 1)
        self.assertEqual(current["etag"], '"v1"')
        self.assertNotIn("downloadUrl", json.dumps(self.drive.write_manifest.await_args.args[1]))

    async def test_manifest_failure_does_not_lose_local_schedule(self):
        self.drive.write_manifest.side_effect = SourceError("network")
        await self.retention.arm(self.job_now())
        self.assertEqual(self.job_now()["retention"], "armed")
        self.assertEqual(self.job_now()["manifest"], 0)
        self.assertTrue(self.job_now()["cleanup_error"])

    async def test_no_delete_before_deadline(self):
        await self.retention.arm(self.job_now())
        await self.retention.sweep()
        self.drive.recycle.assert_not_awaited()

    async def test_expired_file_is_recycled_and_history_retained(self):
        await self.retention.arm(self.job_now())
        self.store.change(self.job["id"], expires=time.time() - 1)
        self.drive.recycle.return_value = "deleted"
        await self.retention.sweep()
        self.drive.recycle.assert_awaited_once_with(self.job_now()["destination"], "original-id", '"v1"')
        self.assertEqual(self.job_now()["state"], "deleted")
        self.drive.delete_manifest.assert_awaited_once()

    async def test_failed_cleanup_is_retried_without_extending_ttl(self):
        await self.retention.arm(self.job_now())
        self.store.change(self.job["id"], expires=time.time() - 1)
        expiry = self.job_now()["expires"]
        self.drive.recycle.side_effect = GraphError(503)
        await self.retention.sweep()
        self.assertEqual(self.job_now()["state"], "done")
        self.assertEqual(self.job_now()["expires"], expiry)
        self.drive.recycle.side_effect = None
        self.drive.recycle.return_value = "deleted"
        await self.retention.sweep()
        self.assertEqual(self.drive.recycle.await_count, 2)
        self.assertEqual(self.job_now()["state"], "deleted")

    async def test_replaced_file_is_not_armed(self):
        self.drive.item.return_value["id"] = "replacement"
        result = await self.retention.arm(self.job_now())
        self.assertEqual(self.job_now()["retention"], "conflict")
        self.assertIsNone(result)
        self.drive.write_manifest.assert_not_awaited()
        self.drive.recycle.assert_not_awaited()

    async def test_modified_file_is_not_deleted(self):
        await self.retention.arm(self.job_now())
        self.store.change(self.job["id"], expires=time.time() - 1)
        self.drive.recycle.return_value = "conflict"
        await self.retention.sweep()
        self.assertEqual(self.job_now()["retention"], "conflict")
        self.drive.delete_manifest.assert_not_awaited()

    async def test_manifest_restores_expiry_and_owner_after_database_loss(self):
        await self.retention.arm(self.job_now())
        record = self.retention.record(self.job_now())
        self.store.close()
        self.store = Store(self.s.data_dir / "restored.db")
        self.retention.store = self.store
        self.transfer.run_command.side_effect = [json.dumps([{"Path": self.job["id"] + "/.bot-retention.json", "Size": 1000}]).encode(), json.dumps(record).encode()]
        self.assertEqual(await self.retention.restore(), 1)
        restored = self.store.get(self.job["id"], 1)
        self.assertEqual(restored["expires"], record["expires"])
        self.assertEqual(restored["file_id"], "original-id")
        self.assertIsNone(self.store.get(self.job["id"], 2))

    async def test_manifest_cannot_target_another_folder_or_parent(self):
        await self.retention.arm(self.job_now())
        record = self.retention.record(self.job_now())
        for path in ["onedrive:Documents/secret.zip", "onedrive:TelegramUploads/" + self.job["id"] + "/../secret.zip"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.retention.validate_record(record | {"destination": path}, self.job["id"])

    async def test_find_is_literal_scoped_and_excludes_expired(self):
        self.store.change(self.job["id"], destination=self.job_now()["destination"].replace("rom.zip", "rom_100%.zip"))
        self.assertEqual(len(self.store.find(1, "100%")), 1)
        self.assertEqual(self.store.find(2, "rom"), [])
        self.assertEqual(self.store.find(1, "nonexistent"), [])
        self.store.change(self.job["id"], expires=time.time() - 1)
        self.assertEqual(self.store.find(1, "rom"), [])

    async def test_getting_links_does_not_extend_expiration(self):
        await self.retention.arm(self.job_now())
        bot = Bot(self.s)
        bot.store.close()
        bot.store = self.store
        bot.retention = self.retention
        bot.telegram.send = AsyncMock()
        bot.drive.links = AsyncMock(return_value=self.drive.item.return_value)
        expiry = self.job_now()["expires"]
        await bot.handle({"update_id": 200, "message": {"text": "/dl " + self.job["id"], "from": {"id": 1}, "chat": {"id": 1, "type": "private"}}})
        self.assertEqual(self.job_now()["expires"], expiry)
        self.assertIn("temp-download", bot.telegram.send.await_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"])

    async def test_owner_refresh_upgrades_legacy_link_without_rearming_deletion(self):
        await self.retention.arm(self.job_now())
        old=self.job_now()
        item={**self.drive.item.return_value,'eTag':'metadata-v2','cTag':'content-v1'}
        refreshed=await self.retention.refresh_link_record(old,item)
        self.assertEqual(refreshed['link_ctag'],'content-v1')
        self.assertEqual(refreshed['etag'],old['etag'])
        self.assertEqual(refreshed['expires'],old['expires'])
        self.assertEqual(refreshed['completed'],old['completed'])
        self.assertEqual(self.drive.write_manifest.await_args.args[1]['link_ctag'],'content-v1')
        self.assertEqual(self.drive.write_manifest.await_args.args[1]['etag'],old['etag'])

    async def test_refresh_does_not_trust_changed_content_or_extend_expired_file(self):
        await self.retention.arm(self.job_now())
        self.store.change(self.job['id'],link_ctag='original-content')
        self.drive.write_manifest.reset_mock()
        for change in ({'cTag':'changed-content'},{'id':'replacement','cTag':'original-content'},
                       {'size':20,'cTag':'original-content'}):
            with self.assertRaises(SourceError):
                await self.retention.refresh_link_record(self.job_now(),{**self.drive.item.return_value,**change})
        self.store.change(self.job['id'],expires=time.time()-1)
        with self.assertRaises(SourceError):
            await self.retention.refresh_link_record(self.job_now(),self.drive.item.return_value)
        self.drive.write_manifest.assert_not_awaited()


class GraphDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_uses_immutable_id_and_if_match(self):
        drive = OneDrive(Settings(token="test", users={1}), None)
        drive.item = AsyncMock(return_value={"id": "id1", "eTag": '"v1"', "file": {}})
        drive.drive = lambda: "/drives/drive1"
        drive.request = AsyncMock()
        self.assertEqual(await drive.recycle("onedrive:TelegramUploads/a/file", "id1", '"v1"'), "deleted")
        drive.request.assert_awaited_once_with("DELETE", "/drives/drive1/items/id1", headers={"If-Match": '"v1"'})

    async def test_different_id_etag_and_missing_file_never_delete(self):
        for item in [{"id": "id2", "eTag": '"v1"', "file": {}}, {"id": "id1", "eTag": '"v2"', "file": {}}, {"id": "id1", "eTag": '"v1"', "folder": {}}]:
            drive = OneDrive(Settings(token="test", users={1}), None)
            drive.item = AsyncMock(return_value=item)
            drive.request = AsyncMock()
            self.assertEqual(await drive.recycle("dest", "id1", '"v1"'), "conflict")
            drive.request.assert_not_awaited()

    async def test_404_and_412_are_safe(self):
        drive = OneDrive(Settings(token="test", users={1}), None)
        drive.item = AsyncMock(side_effect=GraphError(404))
        drive.request = AsyncMock()
        self.assertEqual(await drive.recycle("dest", "id1", '"v1"'), "missing")
        drive.request.assert_not_awaited()
        drive.item.side_effect = None
        drive.item.return_value = {"id": "id1", "eTag": '"v1"', "file": {}}
        drive.drive = lambda: "/drives/drive1"
        drive.request.side_effect = GraphError(412)
        self.assertEqual(await drive.recycle("dest", "id1", '"v1"'), "conflict")
