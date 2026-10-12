import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestServer

from app.onedrive import OneDrive
from app.settings import Settings
from scripts.onedrive_login import extract_token


class GraphHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_links_manifest_and_conditional_delete_over_http(self):
        requests = []

        async def handler(request):
            requests.append((request.method, request.path, dict(request.headers),
                await request.json() if request.method == "PUT" else None))
            if request.method == "DELETE":
                return web.Response(status=204)
            return web.json_response({"id": "file-id", "eTag": '"version1"', "file": {}, "size": 10,
                "name": "file có dấu.zip", "webUrl": "https://example.test/file",
                "@microsoft.graph.downloadUrl": "https://example.test/download?temporary=1"})

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        async with TestServer(app) as server:
            drive = OneDrive(Settings(token="test", users={1}), None)
            drive.base = lambda: str(server.make_url("/")).rstrip("/")
            drive.config = lambda: {"drive_id": "drive!1"}
            drive.token = AsyncMock(return_value="test-access")
            await drive.start()
            try:
                destination = "onedrive:TelegramUploads/abcdef012345/file có dấu.zip"
                item = await drive.links(destination)
                self.assertIn("temporary=1", item["@microsoft.graph.downloadUrl"])
                await drive.write_manifest(destination, {"version": 1, "expires": 123})
                result = await drive.recycle(destination, "file-id", '"version1"')
            finally:
                await drive.close()
        self.assertEqual(result, "deleted")
        self.assertEqual(requests[0][1], "/drives/drive!1/root:/TelegramUploads/abcdef012345/file có dấu.zip")
        self.assertEqual(requests[1][1], "/drives/drive!1/root:/TelegramUploads/abcdef012345/.bot-retention.json:/content")
        self.assertEqual(requests[1][3], {"version": 1, "expires": 123})
        self.assertEqual(requests[-1][0:2], ("DELETE", "/drives/drive!1/items/file-id"))
        self.assertEqual(requests[-1][2]["If-Match"], '"version1"')
        self.assertTrue(all(r[2]["Authorization"] == "Bearer test-access" for r in requests))


class OAuthOutputTests(unittest.TestCase):
    def test_only_token_object_is_selected(self):
        output = 'Log {not-json}\n {"other": 1}\n {"access_token":"fixture", "refresh_token":"fixture2"}\nEnd'
        self.assertEqual(extract_token(output)["refresh_token"], "fixture2")

    def test_incomplete_token_rejected(self):
        with self.assertRaises(ValueError):
            extract_token('{"access_token":"fixture"}')
