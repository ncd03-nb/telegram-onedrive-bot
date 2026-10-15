"""Dispatch and observe one Actions transfer without running another poller."""
import asyncio
import io
import json
import time
import zipfile
from datetime import datetime, timezone

import aiohttp

from .source import SourceError


class Actions:
    def __init__(self, settings):
        self.s = settings
        self.session = None
        self.before_dispatch = None
        self.base = f"https://api.github.com/repos/{settings.github_repo}"

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), trust_env=False)

    async def close(self):
        if self.session:
            await self.session.close()

    async def request(self, method, path, payload=None, params=None):
        async with self.session.request(method, self.base + path, json=payload, params=params,
            headers={"Authorization": "Bearer " + self.s.github_token,
                "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}) as r:
            if r.status not in {200, 201, 202, 204}:
                raise SourceError(f"GitHub Actions HTTP {r.status}; kiểm tra token, quyền Actions và quota.")
            return await r.json() if r.content_length != 0 and r.status != 204 else {}

    async def dispatch(self, job):
        if self.before_dispatch:
            await self.before_dispatch()
        # Inputs go through JSON, never interpolation into shell commands.
        return await self.request("POST", "/actions/workflows/copy.yml/dispatches", {
            "ref": self.s.github_ref, "inputs": {
                "job_id": job["id"], "source_url": job["url"],
                "requested_name": job["requested_name"], "owner": str(job["owner"]),
                "chat": str(job["chat"]), "message_id": str(job["message_id"]),
                "max_file_gib": str(self.s.max_bytes // 1024**3),
                "retention_hours": str(self.s.retention_hours)}})

    async def find_run(self, job):
        since = datetime.fromtimestamp((job["dispatch_at"] or job["created"]) - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = await self.request("GET", "/actions/workflows/copy.yml/runs",
            params={"event": "workflow_dispatch", "created": ">=" + since, "per_page": 100})
        matches = [r for r in result["workflow_runs"] if r.get("display_title") == "copy-" + job["id"]]
        if len(matches) > 1:
            raise SourceError("Phát hiện nhiều workflow cùng mã; kiểm tra Actions trước khi chạy lại.")
        return matches[0] if matches else None

    async def cancel(self, run_id):
        return await self.request("POST", f"/actions/runs/{int(run_id)}/cancel")

    async def result(self, run_id, job_id):
        obj = await self.request("GET", f"/actions/runs/{int(run_id)}/artifacts", params={"per_page": 100})
        artifact = next((a for a in obj["artifacts"] if a["name"] == "result-" + job_id and not a["expired"]), None)
        if not artifact or artifact["size_in_bytes"] > 131072:
            raise SourceError("Workflow chưa có kết quả hợp lệ; kiểm tra thư mục tác vụ trên OneDrive.")
        async with self.session.get(self.base + f"/actions/artifacts/{artifact['id']}/zip",
            headers={"Authorization": "Bearer " + self.s.github_token}, allow_redirects=False) as r:
            if r.status != 302 or not r.headers.get("Location", "").startswith("https://"):
                raise SourceError("Không lấy được artifact kết quả Actions.")
            location = r.headers["Location"]
        # No GitHub credential is forwarded to signed artifact storage.
        async with self.session.get(location) as r:
            raw = await read_limited(r.content, 131072)
            if r.status != 200 or len(raw) > 131072:
                raise SourceError("Artifact kết quả không hợp lệ.")
        return decode_result(raw, job_id)

    async def observe(self, job, store):
        while True:
            current = store.get(job["id"], job["owner"])
            if current["state"] != "running":
                return None
            try:
                run = (await self.request("GET", f"/actions/runs/{current['run_id']}")) if current["run_id"] else await self.find_run(current)
                if run:
                    store.change(job["id"], run_id=run["id"], phase=run["status"])
                    if run["status"] == "completed":
                        # Artifact step runs even when transfer failed.
                        try:
                            return await self.result(run["id"], job["id"])
                        except SourceError:
                            if run.get("conclusion") == "success":
                                raise
                            return {"state": "cancelled" if run.get("conclusion") == "cancelled" else "failed",
                                "error": "Workflow " + str(run.get("conclusion")) + "; kiểm tra Actions và thư mục đích."}
                elif time.time() - current["dispatch_at"] > 600:
                    raise SourceError("Chưa tìm thấy runner sau 10 phút; bot không tự dispatch lại để tránh bản trùng. Kiểm tra Actions.")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # Reconcile after transient connection failures, including controller restart.
                pass
            await asyncio.sleep(15)


async def read_limited(stream, limit):
    data = bytearray()
    while True:
        block = await stream.read(min(65536, limit + 1 - len(data)))
        if not block: return bytes(data)
        data.extend(block)
        if len(data) > limit: raise SourceError('Artifact kết quả vượt kích thước cho phép.')


def decode_result(raw, job_id):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entry = archive.getinfo("result.json")
        if entry.file_size > 65536:
            raise SourceError("Kết quả Actions vượt kích thước cho phép.")
        obj = json.loads(archive.read(entry))
    if not isinstance(obj, dict) or obj.get("id") != job_id or obj.get("state") not in {"done", "failed", "cancelled"}:
        raise SourceError("Kết quả Actions không khớp tác vụ.")
    return obj
