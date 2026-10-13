"""Graph metadata/download links and guarded recycling of bot-managed files."""
import asyncio
import configparser
import json
import time
from datetime import datetime
from urllib.parse import quote, urlsplit

import aiohttp

from .source import SourceError


class GraphError(SourceError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"OneDrive API trả HTTP {status}; kiểm tra quyền / quota / OAuth.")


class OneDrive:
    def __init__(self, settings, transfer, busy=lambda: False):
        self.s, self.transfer, self.busy = settings, transfer, busy
        self.session = None
        self.refresh_lock = asyncio.Lock()

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), trust_env=False)

    async def close(self):
        if self.session:
            await self.session.close()

    def config(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(self.s.config, encoding="utf-8-sig")
        return parser[self.s.destination.split(":", 1)[0]]

    def valid_token(self):
        obj = json.loads(self.config().get("token", "{}"))
        expiry = obj.get("expiry", "")
        try:
            if datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp() > time.time() + 30:
                return obj.get("access_token")
        except (ValueError, TypeError):
            pass
        return None

    async def token(self):
        token = self.valid_token()
        if token:
            return token
        async with self.refresh_lock:
            token = self.valid_token()
            if token:
                return token
            if self.busy():
                raise SourceError("OAuth đang cần refresh. Đợi tác vụ upload hiện tại kết thúc rồi lấy link lại.")
            await self.transfer.check()  # rclone refreshes and atomically saves its OAuth config.
            token = self.valid_token()
            if not token:
                raise SourceError("OneDrive OAuth cần đăng nhập lại để lấy link và xóa theo lịch.")
            return token

    def base(self):
        config = self.config()
        if config.get("tenant_url"):
            origin = config["tenant_url"].rstrip("/")
            parsed = urlsplit(origin)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".sharepoint.com"):
                raise SourceError("tenant_url không hợp lệ cho OneDrive API.")
            return origin + "/v2.0"
        host = {"global": "graph.microsoft.com", "us": "graph.microsoft.us", "cn": "microsoftgraph.chinacloudapi.cn"}.get(config.get("region", "global"))
        if not host:
            raise SourceError("OneDrive region chưa hỗ trợ cho metadata API.")
        return "https://" + host + "/v1.0"

    def drive(self):
        value = self.config().get("drive_id", "")
        if not value:
            raise SourceError("rclone.conf chưa có drive_id.")
        return "/drives/" + quote(value, safe="")

    def relative(self, destination):
        prefix = self.s.destination.rstrip("/") + "/"
        if not destination.startswith(prefix):
            raise SourceError("Thao tác chỉ áp dụng bên trong thư mục file của bot.")
        path = destination.split(":", 1)[1].strip("/")
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise SourceError("Đường dẫn file không hợp lệ.")
        return path

    def by_path(self, destination):
        return self.drive() + "/root:/" + quote(self.relative(destination), safe="/")

    async def request(self, method, endpoint, payload=None, headers=None):
        token = await self.token()
        request_headers = {"Authorization": "Bearer " + token, **(headers or {})}
        async with self.session.request(method, self.base() + endpoint,
                headers=request_headers, json=payload, allow_redirects=False) as r:
            if r.status == 204:
                return {}
            if r.status not in {200, 201}:
                raise GraphError(r.status)
            return await r.json()

    async def item(self, destination):
        return await self.request("GET", self.by_path(destination))

    async def links(self, destination):
        obj = await self.item(destination)
        if "file" not in obj or not obj.get("webUrl") or not obj.get("@microsoft.graph.downloadUrl"):
            raise SourceError("OneDrive chưa cung cấp đủ hai link cho file này.")
        return obj

    async def recycle(self, destination, file_id, etag):
        # Re-fetch by path before deleting by immutable ID: a moved/replaced file is protected.
        try:
            obj = await self.item(destination)
        except GraphError as e:
            if e.status == 404:
                return "missing"
            raise
        if not file_id or not etag or obj.get("id") != file_id or obj.get("eTag") != etag or "file" not in obj:
            return "conflict"
        try:
            await self.request("DELETE", self.drive() + "/items/" + quote(file_id, safe=""), headers={"If-Match": etag})
        except GraphError as e:
            if e.status == 404:
                return "missing"
            if e.status == 412:
                return "conflict"
            raise
        return "deleted"

    async def write_manifest(self, destination, record):
        folder = destination.rsplit("/", 1)[0]
        return await self.request("PUT", self.by_path(folder + "/.bot-retention.json") + ":/content", payload=record,
            headers={"Content-Type": "application/json"})

    async def read_manifest(self, job_id):
        import re
        if not re.fullmatch(r"[a-f0-9]{12}", job_id):
            raise ValueError("Invalid job id")
        obj = await self.item(self.s.destination.rstrip("/") + "/" + job_id + "/.bot-retention.json")
        if "file" not in obj or not isinstance(obj.get("size"), int) or not 0 < obj["size"] <= 65536:
            raise ValueError("Invalid manifest size")
        url = obj.get("@microsoft.graph.downloadUrl", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid manifest URL")
        # Graph's preauthenticated URL needs no bearer header. Never forward OAuth.
        async with self.session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise GraphError(response.status)
            raw = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise ValueError("Manifest too large")
            return json.loads(raw)

    async def delete_manifest(self, destination):
        path = destination.rsplit("/", 1)[0] + "/.bot-retention.json"
        try:
            obj = await self.item(path)
            await self.request("DELETE", self.drive() + "/items/" + quote(obj["id"], safe=""), headers={"If-Match": obj["eTag"]})
        except GraphError as e:
            if e.status != 404:
                raise
