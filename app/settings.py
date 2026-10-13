import base64
import configparser
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Settings:
    token: str
    users: set[int]
    mode: str = "polling"
    port: int = 8080
    data_dir: Path = Path("/data")
    config: Path = Path("/config/rclone.conf")
    google_token: Path = Path("/config/google-token.json")
    destination: str = "onedrive:TelegramUploads"
    max_bytes: int = 12 * 1024**3
    spool_bytes: int = 512 * 1024**2
    transfer_timeout: int = 21600
    webhook_url: str = ""
    webhook_secret: str = ""
    rclone: str = "rclone"
    retention_hours: int = 12
    cleanup_interval: int = 60
    onedrive_chunk_mib: int = 10
    transfer_backend: str = "local"
    github_token: str = ""
    github_repo: str = "ncd03-nb/telegram-onedrive-bot"
    github_ref: str = "main"
    download_base_url: str = ""

    @classmethod
    def load(cls):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        users = {int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token) or not users:
            raise ValueError("Cần TELEGRAM_BOT_TOKEN và ALLOWED_USER_IDS hợp lệ trong .env.")
        s = cls(token=token, users=users,
                mode=os.environ.get("BOT_MODE", "polling"),
                port=int(os.environ.get("PORT", "8080")),
                data_dir=Path(os.environ.get("DATA_DIR", "/data")),
                config=Path(os.environ.get("RCLONE_CONFIG", "/config/rclone.conf")),
                google_token=Path(os.environ.get("GOOGLE_TOKEN_PATH", "/config/google-token.json")),
                destination=os.environ.get("ONEDRIVE_DEST", "onedrive:TelegramUploads"),
                max_bytes=int(os.environ.get("MAX_FILE_GIB", "12")) * 1024**3,
                spool_bytes=int(os.environ.get("MAX_SPOOL_MIB", "512")) * 1024**2,
                transfer_timeout=int(os.environ.get("TRANSFER_TIMEOUT_SECONDS", "21600")),
                webhook_url=os.environ.get("WEBHOOK_URL", "").rstrip("/"),
                webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
                rclone=os.environ.get("RCLONE_BINARY", "rclone"),
                retention_hours=int(os.environ.get("RETENTION_HOURS", "12")),
                cleanup_interval=int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "60")),
                onedrive_chunk_mib=int(os.environ.get("ONEDRIVE_CHUNK_MIB", "10")),
                transfer_backend=os.environ.get("TRANSFER_BACKEND", "local"),
                github_token=os.environ.get("ACTIONS_DISPATCH_TOKEN", ""),
                github_repo=os.environ.get("ACTIONS_REPO", "ncd03-nb/telegram-onedrive-bot"),
                github_ref=os.environ.get("ACTIONS_REF", "main"),
                download_base_url=os.environ.get("DOWNLOAD_BASE_URL", "https://telegram-onedrive-standby.onrender.com").rstrip("/"))
        if s.download_base_url:
            parsed = urlsplit(s.download_base_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
                raise ValueError("DOWNLOAD_BASE_URL phải là địa chỉ gốc HTTPS.")
        if s.transfer_backend not in {"local", "actions"}:
            raise ValueError("TRANSFER_BACKEND phải là local hoặc actions.")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", s.github_repo):
            raise ValueError("ACTIONS_REPO không hợp lệ.")
        if s.transfer_backend == "actions" and s.mode != "standby" and not s.github_token:
            raise ValueError("Cần ACTIONS_DISPATCH_TOKEN để chạy GitHub Actions.")
        if s.mode not in {"polling", "webhook", "standby"}:
            raise ValueError("BOT_MODE phải là polling, webhook hoặc standby.")
        if not 5 <= s.onedrive_chunk_mib <= 50 or s.onedrive_chunk_mib % 5:
            raise ValueError("ONEDRIVE_CHUNK_MIB phải là bội số 5 từ 5 đến 50 MiB.")
        if s.max_bytes <= 0 or s.spool_bytes < 0 or s.transfer_timeout <= 0 or s.retention_hours <= 0 or s.cleanup_interval < 10:
            raise ValueError("Giới hạn dung lượng / thời gian không hợp lệ.")
        if not re.fullmatch(r"[\w-]+:[^\\]*", s.destination) or any(x in {".", ".."} for x in s.destination.split(":", 1)[1].split("/")):
            raise ValueError("ONEDRIVE_DEST không hợp lệ.")
        if s.mode == "webhook" and (not s.webhook_url.startswith("https://") or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", s.webhook_secret)):
            raise ValueError("Webhook cần WEBHOOK_URL HTTPS và WEBHOOK_SECRET 32–256 ký tự.")
        s.data_dir.mkdir(parents=True, exist_ok=True)
        for env, path in [("RCLONE_CONFIG_B64", s.config), ("GOOGLE_TOKEN_B64", s.google_token)]:
            if os.environ.get(env) and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(os.environ[env], validate=True))
                path.chmod(0o600)
        if s.mode != "standby":
            if not s.config.is_file():
                raise ValueError("Thiếu rclone.conf. Hãy đăng nhập OneDrive bằng rclone config.")
            config = configparser.ConfigParser(interpolation=None)
            config.read(s.config, encoding="utf-8-sig")
            remote = s.destination.split(":", 1)[0]
            if not config.has_section(remote) or config.get(remote, "type", fallback="") != "onedrive":
                raise ValueError("ONEDRIVE_DEST phải dùng remote loại onedrive trong rclone.conf chưa mã hóa.")
            if s.google_token.exists():
                obj = json.loads(s.google_token.read_text(encoding="utf-8"))
                if not all(obj.get(k) for k in ("client_id", "client_secret", "refresh_token")):
                    raise ValueError("google-token.json thiếu thông tin OAuth.")
        return s
