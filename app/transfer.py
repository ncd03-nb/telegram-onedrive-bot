import asyncio
import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

from .source import SourceError, clean_name


def rclone_error(lines):
    text = " ".join(lines).lower()
    if any(x in text for x in ("invalid_grant", "aadsts", "token expired", "unauthorized", "401")):
        return "OneDrive OAuth cần đăng nhập lại: rclone config reconnect onedrive:."
    if any(x in text for x in ("quota", "insufficientstorage", "507")):
        return "OneDrive không còn đủ quota để lưu file."
    if "403" in text or "accessdenied" in text:
        return "OneDrive từ chối quyền truy cập. Kiểm tra quyền Microsoft 365 của trường."
    return "Upload OneDrive thất bại. Kiểm tra kết nối / OAuth / quota; dùng /check để kiểm tra tài khoản."


async def stop_process(proc):
    if proc and proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


class Transfer:
    def __init__(self, settings, sources):
        self.s = settings
        self.sources = sources
        self.verified = {}

    def flags(self):
        return ["--config", str(self.s.config), "--onedrive-chunk-size", "10Mi",
                "--buffer-size", "1Mi", "--transfers", "1", "--checkers", "1",
                "--retries", "1", "--low-level-retries", "10", "--contimeout", "30s",
                "--timeout", "5m", "--stats", "0", "--log-level", "ERROR"]

    async def run_command(self, args, timeout=60):
        proc = await asyncio.create_subprocess_exec(self.s.rclone, *args,
            "--config", str(self.s.config), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            if proc.returncode:
                raise SourceError(rclone_error([err.decode("utf-8", "replace")]))
            return out
        finally:
            await stop_process(proc)

    async def check(self):
        obj = json.loads(await self.run_command(["about", self.s.destination.split(":", 1)[0] + ":", "--json"]))
        return obj

    async def copy(self, job, progress):
        async with asyncio.timeout(self.s.transfer_timeout):
            async with self.sources.open(job["url"]) as (response, meta):
                name = clean_name(job["requested_name"] or meta["name"])
                dest = self.s.destination.rstrip("/") + "/" + job["id"] + "/" + name
                await progress(0, meta["size"], dest)
                if meta["size"] is None:
                    await self.spool(response, meta, dest, progress)
                else:
                    await self.stream(response, meta, dest, progress)
                # A subprocess exit alone is not proof of a complete destination file.
                obj = json.loads(await self.run_command(["lsjson", dest, "--stat"]))
                if obj.get("IsDir") or obj.get("Size") != meta["size"]:
                    raise SourceError("Dung lượng file trên OneDrive không khớp; kiểm tra thư mục tác vụ trước khi thử lại.")
                self.verified[dest] = obj
                return dest

    async def read_errors(self, proc, lines):
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            lines.append(raw.decode("utf-8", "replace")[:2000])
            del lines[:-8]

    async def stream(self, response, meta, dest, progress):
        proc = await asyncio.create_subprocess_exec(self.s.rclone, "rcat", dest, "--size", str(meta["size"]),
            *self.flags(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        lines = []
        reader = asyncio.create_task(self.read_errors(proc, lines))
        count, last = 0, 0
        digest = hashlib.md5(usedforsecurity=False)
        try:
            async for block in response.content.iter_chunked(1024 * 1024):
                count += len(block)
                if count > meta["size"] or count > self.s.max_bytes:
                    raise SourceError("Nguồn gửi nhiều byte hơn dung lượng khai báo.")
                digest.update(block)
                proc.stdin.write(block)
                await proc.stdin.drain()
                if time.monotonic() - last > 3:
                    await progress(count, meta["size"], dest)
                    last = time.monotonic()
            if count != meta["size"]:
                raise SourceError("Nguồn bị ngắt; file tải chưa đủ dung lượng.")
            if meta["md5"] and digest.hexdigest().lower() != meta["md5"].lower():
                raise SourceError("MD5 không khớp metadata Google Drive; không xác nhận tác vụ hoàn tất.")
            proc.stdin.close()
            await proc.wait()
            await reader
            if proc.returncode:
                raise SourceError(rclone_error(lines))
            await progress(count, meta["size"], dest)
        except (BrokenPipeError, ConnectionResetError):
            await stop_process(proc)
            await reader
            raise SourceError(rclone_error(lines)) from None
        finally:
            await stop_process(proc)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def spool(self, response, meta, dest, progress):
        # Only for servers without a known length. Never spool a 10 GB Drive file.
        limit = min(self.s.max_bytes, self.s.spool_bytes)
        if not limit:
            raise SourceError("Nguồn không khai báo Content-Length. Host này đã tắt lưu tạm.")
        with tempfile.TemporaryDirectory(prefix="spool-", dir=self.s.data_dir) as directory:
            path = Path(directory) / "input"
            count = 0
            digest = hashlib.md5(usedforsecurity=False)
            with path.open("wb") as f:
                async for block in response.content.iter_chunked(1024 * 1024):
                    count += len(block)
                    if count > limit or shutil.disk_usage(directory).free < len(block) + 64 * 1024**2:
                        raise SourceError("Nguồn không khai báo dung lượng và vượt giới hạn lưu tạm / ổ đĩa.")
                    f.write(block)
                    digest.update(block)
                    await progress(count, None, dest)
            meta["size"] = count
            if meta["md5"] and digest.hexdigest().lower() != meta["md5"].lower():
                raise SourceError("MD5 file nguồn không khớp.")
            await progress(count, count, dest)
            proc = await asyncio.create_subprocess_exec(self.s.rclone, "copyto", str(path), dest, *self.flags(),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            lines = []
            reader = asyncio.create_task(self.read_errors(proc, lines))
            try:
                await proc.wait()
                await reader
                if proc.returncode:
                    raise SourceError(rclone_error(lines))
            finally:
                await stop_process(proc)
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
