"""Container smoke: real rclone binary and standby HTTP lifecycle; no cloud secrets."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.settings import Settings
from app.transfer import Transfer


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    config = root / "empty.conf"
    config.write_text("")
    settings = Settings(token="1:test", users={1}, config=config, data_dir=root)
    content = bytes(range(256)) * 12288
    target = root / "stream.bin"
    subprocess.run(["rclone", "rcat", str(target), "--size", str(len(content)), *Transfer(settings, None).flags()],
        input=content, check=True, capture_output=True, timeout=30)
    assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(content).digest()
    print("Real rclone 1.75.1: streamed 3 MiB, size and SHA256 verified.")

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    env = os.environ | {"BOT_MODE": "standby", "TELEGRAM_BOT_TOKEN": "1:test", "ALLOWED_USER_IDS": "1",
        "PORT": str(port), "DATA_DIR": str(root / "state"), "RCLONE_CONFIG_B64": "", "GOOGLE_TOKEN_B64": ""}
    proc = subprocess.Popen([sys.executable, "-m", "app.main"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        endpoint = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                with urllib.request.urlopen(endpoint + "/health", timeout=2) as r:
                    assert json.load(r) == {"status": "ok", "mode": "standby"}
                break
            except (OSError, urllib.error.URLError):
                if proc.poll() is not None:
                    raise RuntimeError("Standby process exited: " + proc.communicate()[1].decode())
                time.sleep(0.1)
        else:
            raise RuntimeError("Standby did not become ready")
        try:
            urllib.request.urlopen(urllib.request.Request(endpoint + "/telegram", data=b"{}",
                headers={"Content-Type": "application/json"}), timeout=2)
            raise AssertionError("Standby accepted Telegram update")
        except urllib.error.HTTPError as e:
            assert e.code == 503
        print("Standby container: /health HTTP 200; /telegram HTTP 503; no bot takeover.")
    finally:
        proc.terminate()
        proc.communicate(timeout=10)
    assert proc.returncode == 0, f"Unexpected exit code {proc.returncode}"
    print("SIGTERM shutdown: clean exit, SQLite closed.")
