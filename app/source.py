import asyncio
import ipaddress
import json
import re
import socket
from contextlib import asynccontextmanager
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit, unquote

import aiohttp


class SourceError(Exception):
    pass


def public_ip(value):
    ip = ipaddress.ip_address(value)
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url):
    try:
        u = urlsplit(url)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.port not in {None, 80, 443}:
            raise ValueError()
        host = u.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not public_ip(host):
                raise ValueError()
        if len(url) > 8192:
            raise ValueError()
    except ValueError:
        raise SourceError("Chỉ nhận URL HTTP(S) công khai, không nhận IP nội bộ / cổng tùy chỉnh.") from None
    return url


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        info = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        if not info or any(not public_ip(row[4][0]) for row in info):
            raise OSError("Địa chỉ nguồn không phải Internet công khai.")
        return [{"hostname": host, "host": row[4][0], "port": port, "family": row[0], "proto": row[2], "flags": socket.AI_NUMERICHOST} for row in info]

    async def close(self):
        pass


def drive_id(url):
    u = urlsplit(url)
    if u.hostname not in {"drive.google.com", "www.drive.google.com"}:
        return None
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", u.path)
    value = m.group(1) if m else parse_qs(u.query).get("id", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", value):
        raise SourceError("Cần link Google Drive của một file, không phải thư mục.")
    return value


def normalize(url):
    validate_url(url)
    file_id = drive_id(url)
    if file_id:
        q = {"id": file_id, "export": "download", "confirm": "t"}
        resource = parse_qs(urlsplit(url).query).get("resourcekey", [None])[0]
        if resource:
            q["resourcekey"] = resource
        return "https://drive.usercontent.google.com/download?" + urlencode(q)
    u = urlsplit(url)
    if u.hostname in {"dropbox.com", "www.dropbox.com"}:
        q = parse_qs(u.query)
        q.pop("raw", None)
        q["dl"] = ["1"]
        return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q, doseq=True), ""))
    if u.hostname in {"mega.nz", "mega.co.nz"}:
        raise SourceError("Link MEGA mã hóa chưa được hỗ trợ. Dùng Google Drive hoặc link tải trực tiếp.")
    return url


def filename(header, url):
    msg = Message()
    msg["Content-Disposition"] = header or ""
    return clean_name(msg.get_filename() or unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "file.bin")


def clean_name(value):
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", value).strip(" .")[:120]
    if not name or name in {".", ".."}:
        return "file.bin"
    return name


class ConfirmForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = ""
        self.fields = {}
        self.inside = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "download-form":
            self.action = a.get("action", "")
            self.inside = True
        if tag == "input" and self.inside and a.get("name") in {"id", "export", "confirm", "uuid", "resourcekey"}:
            self.fields[a["name"]] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


class Sources:
    def __init__(self, settings):
        self.settings = settings
        self.session = None
        self.api = None

    async def start(self):
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=8),
            auto_decompress=False, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120),
            headers={"Accept-Encoding": "identity", "User-Agent": "TelegramOneDriveBot/1.0"})
        self.api = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), trust_env=False)

    async def close(self):
        await self.session.close()
        await self.api.close()

    async def google_access(self):
        # Only local OAuth configuration is used; never accept credentials in chat.
        path = self.settings.google_token
        obj = json.loads(path.read_text(encoding="utf-8"))
        async with self.api.post("https://oauth2.googleapis.com/token", data={"grant_type": "refresh_token",
                "client_id": obj["client_id"], "client_secret": obj["client_secret"], "refresh_token": obj["refresh_token"]}) as r:
            result = await r.json()
            if r.status != 200 or not result.get("access_token"):
                raise SourceError("Google OAuth hết hiệu lực. Chạy lại scripts/google_login.py trên máy của bạn.")
            if result.get("refresh_token"):
                obj["refresh_token"] = result["refresh_token"]
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(obj), encoding="utf-8")
                tmp.chmod(0o600)
                tmp.replace(path)
            return result["access_token"]

    async def get(self, url, headers=None):
        current = url
        headers = dict(headers or {})
        for _ in range(10):
            validate_url(current)
            r = await self.session.get(current, headers=headers, allow_redirects=False)
            if r.status in {301, 302, 303, 307, 308}:
                next_url = urljoin(current, r.headers.get("Location", ""))
                r.release()
                if urlsplit(next_url).hostname != urlsplit(current).hostname:
                    headers.pop("Authorization", None)
                current = next_url
                continue
            if r.status != 200:
                r.release()
                raise SourceError(f"Nguồn trả HTTP {r.status}; kiểm tra quyền chia sẻ, quota hoặc link hết hạn.")
            if r.headers.get("Content-Encoding", "identity") != "identity":
                r.release()
                raise SourceError("Nguồn gửi nội dung nén HTTP; cần link tải trả byte gốc.")
            return r
        raise SourceError("Nguồn chuyển hướng quá nhiều lần.")

    @asynccontextmanager
    async def open(self, url):
        file_id = drive_id(url)
        meta = {}
        auth = None
        if file_id and self.settings.google_token.exists():
            auth = {"Authorization": "Bearer " + await self.google_access()}
            resource = parse_qs(urlsplit(url).query).get("resourcekey", [None])[0]
            if resource:
                auth["X-Goog-Drive-Resource-Keys"] = file_id + "/" + resource
            api = f"https://www.googleapis.com/drive/v3/files/{file_id}"
            async with self.api.get(api, headers=auth, params={"fields": "name,size,md5Checksum,mimeType,capabilities(canDownload)", "supportsAllDrives": "true"}) as r:
                if r.status != 200:
                    raise SourceError(f"Drive API trả HTTP {r.status}. File phải được chia sẻ cho tài khoản Google đã đăng nhập.")
                meta = await r.json()
            if "size" not in meta or meta.get("mimeType", "").startswith("application/vnd.google-apps."):
                raise SourceError("Chỉ nhận file nhị phân; thư mục, shortcut và Google Docs chưa được hỗ trợ.")
            if not meta.get("capabilities", {}).get("canDownload", True):
                raise SourceError("Chủ file đã tắt quyền tải xuống.")
            if int(meta["size"]) > self.settings.max_bytes:
                raise SourceError("File vượt MAX_FILE_GIB.")
            download = api + "?alt=media&supportsAllDrives=true"
        else:
            download = normalize(url)
        r = await self.get(download, auth)
        try:
            if "text/html" in r.headers.get("Content-Type", "").lower():
                if not file_id:
                    raise SourceError("Link trả trang HTML. Cần link tải trực tiếp tới file.")
                form = ConfirmForm()
                form.feed((await r.content.read(128 * 1024)).decode("utf-8", "replace"))
                r.release()
                action = urljoin(str(r.url), form.action)
                if urlsplit(action).scheme != "https" or urlsplit(action).hostname != "drive.usercontent.google.com" or form.fields.get("id") != file_id:
                    raise SourceError("Google Drive yêu cầu đăng nhập / vượt quota. Nên cấu hình Google OAuth cho file 10 GB.")
                r = await self.get(action.split("?", 1)[0] + "?" + urlencode(form.fields))
                if "text/html" in r.headers.get("Content-Type", "").lower():
                    raise SourceError("Google Drive từ chối tải. Kiểm tra quyền / quota hoặc cấu hình Google OAuth.")
            total = int(meta["size"]) if meta else r.content_length
            if total is not None and (total < 0 or total > self.settings.max_bytes):
                raise SourceError("File vượt MAX_FILE_GIB.")
            yield r, {"name": clean_name(meta["name"]) if meta else filename(r.headers.get("Content-Disposition"), str(r.url)),
                      "size": total, "md5": meta.get("md5Checksum")}
        finally:
            r.release()
