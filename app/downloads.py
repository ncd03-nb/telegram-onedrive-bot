"""Stateless, expiring download capabilities shared by controller and standby."""
import asyncio
import base64
import hashlib
import hmac
import re
import time
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web

from .onedrive import GraphError
from .source import SourceError


def signature(settings, identity, name):
    key = hmac.digest(settings.token.encode(), b'onedrive-download-link-v1', 'sha256')
    message = (settings.destination + '\n' + identity + '\n' + name).encode()
    return base64.urlsafe_b64encode(hmac.digest(key, message, hashlib.sha256)[:24]).decode().rstrip('=')


def download_url(settings, job):
    if not settings.download_base_url or not job.get('manifest'):
        return ''
    name = job['destination'].rsplit('/', 1)[-1]
    identity = job['id'] + '.' + str(int(job['expires']))
    return settings.download_base_url + '/0:/' + identity + '.' + signature(settings, identity, name) + '/' + quote(name, safe='')


class Downloads:
    def __init__(self, settings, drive, retention):
        self.s, self.drive, self.retention = settings, drive, retention
        self.slots = asyncio.Semaphore(8)

    async def handle(self, request):
        headers = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Robots-Tag': 'noindex, nofollow'}
        capability, name = request.match_info['capability'], request.match_info['name']
        match = re.fullmatch(r'([a-f0-9]{12})\.([0-9]{1,12})\.([A-Za-z0-9_-]{32})', capability)
        if not self.s.download_base_url or not match or not name or name.startswith('.') or any(c in name for c in '/\\\r\n'):
            raise web.HTTPNotFound(text='Link không hợp lệ.', headers=headers)
        job_id, expiry, mac = match.groups()
        if not hmac.compare_digest(mac, signature(self.s, job_id + '.' + expiry, name)):
            raise web.HTTPNotFound(text='Link không hợp lệ.', headers=headers)
        if int(expiry) <= time.time():
            raise web.HTTPGone(text='Link đã hết hạn lưu 12 giờ.', headers=headers)
        if self.slots.locked():
            raise web.HTTPServiceUnavailable(text='Đang bận, hãy thử lại.', headers={**headers, 'Retry-After': '5'})
        try:
            async with self.slots:
                record = self.retention.validate_record(await self.drive.read_manifest(job_id), job_id)
                destination = self.s.destination.rstrip('/') + '/' + job_id + '/' + name
                if (record['destination'] != destination or int(record['expires']) != int(expiry)
                        or record['owner'] not in self.s.users):
                    raise web.HTTPNotFound(text='File không còn khả dụng.', headers=headers)
                item = await self.drive.links(destination)
                if (item.get('id') != record['file_id'] or item.get('eTag') != record['etag']
                        or item.get('size') != record['size']):
                    raise web.HTTPGone(text='File đã thay đổi hoặc bị thay thế.', headers=headers)
                if int(expiry) <= time.time():
                    raise web.HTTPGone(text='Link đã hết hạn lưu 12 giờ.', headers=headers)
                target = item['@microsoft.graph.downloadUrl']
                parsed = urlsplit(target)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Invalid download target')
                # Only redirect: file bytes travel directly from OneDrive to the downloader.
                return web.Response(status=302, headers={**headers, 'Location': target})
        except GraphError as e:
            if e.status == 404:
                raise web.HTTPGone(text='File đã bị xóa hoặc di chuyển.', headers=headers) from None
            raise web.HTTPServiceUnavailable(text='OneDrive đang bận, hãy thử lại.', headers=headers) from None
        except (SourceError, ValueError, TypeError, KeyError, aiohttp.ClientError, TimeoutError):
            raise web.HTTPServiceUnavailable(text='Chưa lấy được link OneDrive, hãy thử lại.', headers=headers) from None
