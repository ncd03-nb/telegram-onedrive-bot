import asyncio
import json
import logging
import math
import re
import time

from .onedrive import GraphError
from .source import SourceError

log = logging.getLogger("bot")


class Retention:
    def __init__(self, settings, store, drive, transfer):
        self.s, self.store, self.drive, self.transfer = settings, store, drive, transfer
        self.lock = asyncio.Lock()

    def record(self, job):
        record = {"version": 1, "id": job["id"], "owner": job["owner"], "chat": job["chat"],
            "destination": job["destination"], "size": job["total"], "completed": job["completed"],
            "expires": job["expires"], "file_id": job["file_id"], "etag": job["etag"]}
        if job.get('link_ctag'):
            record['link_ctag'] = job['link_ctag']
        return record

    def validate_record(self, obj, job_id):
        if not isinstance(obj, dict) or obj.get("version") != 1 or obj.get("id") != job_id or not re.fullmatch(r"[a-f0-9]{12}", job_id):
            raise ValueError("Invalid manifest identity")
        prefix = self.s.destination.rstrip("/") + "/" + job_id + "/"
        name = obj.get("destination", "").removeprefix(prefix)
        if not obj.get("destination", "").startswith(prefix) or not name or name.startswith(".") or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("Manifest outside bot job directory")
        if not isinstance(obj.get("size"), int) or obj["size"] < 0 or not obj.get("file_id") or not obj.get("etag"):
            raise ValueError("Invalid file identity")
        if 'link_ctag' in obj and (not isinstance(obj['link_ctag'],str) or not 0 < len(obj['link_ctag']) <= 4096):
            raise ValueError('Invalid content identity')
        if any(not isinstance(obj.get(k), int) for k in ("owner", "chat")):
            raise ValueError("Invalid owner")
        if any(not isinstance(obj.get(k), (int, float)) or not math.isfinite(obj[k]) for k in ("completed", "expires")) or obj["expires"] <= obj["completed"]:
            raise ValueError("Invalid expiration")
        return obj

    async def arm(self, job):
        async with self.lock:
            return await self._arm(job)

    async def _arm(self, job):
        obj = await self.drive.item(job["destination"])
        if "file" not in obj or obj.get("size") != job["total"]:
            raise SourceError("Không xác minh được file để bật lịch xóa.")
        # Existing identity must never be reset to a file that replaced the original.
        if (job["file_id"] and obj.get("id") != job["file_id"]) or (job["etag"] and obj.get("eTag") != job["etag"]):
            self.store.change(job["id"], retention="conflict", cleanup_error="File đã được thay thế / chỉnh sửa; không tự xóa.")
            return None
        self.store.change(job["id"], file_id=obj["id"], etag=obj["eTag"], file_url=obj.get("webUrl", ""),
            link_ctag=obj.get('cTag',''), retention="armed", cleanup_error="")
        job = self.store.get(job["id"], job["owner"])
        try:
            await self.drive.write_manifest(job["destination"], self.record(job))
        except Exception:
            self.store.change(job["id"], manifest=0, cleanup_error="Lịch xóa có trong DB host; chưa lưu bản phục hồi trên OneDrive.")
        else:
            self.store.change(job["id"], manifest=1, cleanup_error="")
        return obj

    async def refresh_link_record(self, job, item):
        """Owner-requested legacy link upgrade; never reset retention identity/TTL."""
        async with self.lock:
            job = self.store.get(job['id'],job['owner'])
            if not job or job['state'] != 'done' or not job['expires'] or job['expires'] <= time.time():
                raise SourceError('File đã hết hạn hoặc không còn khả dụng.')
            if (not job['file_id'] or item.get('id') != job['file_id'] or 'file' not in item
                    or item.get('size') != job['total']):
                raise SourceError('File đã bị thay thế hoặc đổi dung lượng; không trả link file khác.')
            ctag = item.get('cTag','')
            if job.get('link_ctag') and job['link_ctag'] != ctag:
                raise SourceError('Nội dung file đã bị thay đổi; không tạo link mới cho file đã sửa.')
            if not ctag and item.get('eTag') != job['etag']:
                raise SourceError('Chưa xác minh được nội dung file; OneDrive không trả cTag.')
            updated = {**job,'link_ctag':ctag}
            record = self.validate_record(self.record(updated),job['id'])
            await self.drive.write_manifest(job['destination'],record)
            self.store.change(job['id'],link_ctag=ctag,manifest=1,file_url=item.get('webUrl',''),links_error='')
            return self.store.get(job['id'],job['owner'])

    async def restore(self):
        # Cross-host recovery: only manifests within a valid, single bot job directory.
        files = json.loads(await self.transfer.run_command(["lsjson", self.s.destination, "-R", "--files-only", "--include", "*/.bot-retention.json"], timeout=120))
        count = 0
        for entry in files[:2000]:
            path = entry.get("Path", "")
            match = re.fullmatch(r"([a-f0-9]{12})/\.bot-retention\.json", path)
            if not match or entry.get("Size", 0) > 65536:
                continue
            job_id = match.group(1)
            if self.store.db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                continue
            try:
                raw = await self.transfer.run_command(["cat", self.s.destination.rstrip("/") + "/" + path, "--head", "65536"])
                obj = self.validate_record(json.loads(raw), job_id)
            except (SourceError, ValueError, TypeError, KeyError):
                continue
            self.store.import_manifest(obj)
            count += 1
        return count

    async def sweep(self):
        async with self.lock:
            await self._sweep()

    async def _sweep(self):
        for job in self.store.managed():
            try:
                if job["retention"] == "pending":
                    await self._arm(job)
                    job = self.store.get(job["id"], job["owner"])
                if job["retention"] != "armed":
                    continue
                if job["expires"] > time.time():
                    if not job["manifest"]:
                        await self.drive.write_manifest(job["destination"], self.record(job))
                        self.store.change(job["id"], manifest=1, cleanup_error="")
                    continue
                result = await self.drive.recycle(job["destination"], job["file_id"], job["etag"])
                if result == "conflict":
                    self.store.change(job["id"], retention="conflict", cleanup_error="File đã bị đổi / thay thế; bỏ qua tự xóa để bảo vệ file mới.")
                    continue
                self.store.change(job["id"], state="deleted", retention=result, file_url="", cleanup_error="", links_error="")
                # Only remove our manifest after the main file has been recycled or is absent.
                try:
                    await self.drive.delete_manifest(job["destination"])
                except Exception:
                    pass
            except Exception as e:
                self.store.change(job["id"], cleanup_error="Chưa xóa được theo lịch; bot sẽ thử lại. Kiểm tra OneDrive OAuth / kết nối.")
                log.warning("Retention của %s chưa hoàn thành (%s).", job["id"], type(e).__name__)
