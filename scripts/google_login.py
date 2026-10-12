"""Run only on your own computer. Creates a Google Drive read-only OAuth token."""
import argparse
import base64
import hashlib
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Đăng nhập Google Drive (quyền chỉ đọc).")
    parser.add_argument("--client", required=True, type=Path, help="Google OAuth Desktop client JSON")
    parser.add_argument("--output", type=Path, default=Path("config/google-token.json"))
    parser.add_argument("--no-open-browser", action="store_true", help="Ghi URL vào config/google-auth-url.txt để mở bằng trình duyệt tùy chọn")
    args = parser.parse_args()
    config = json.loads(args.client.read_text(encoding="utf-8"))
    client = config.get("installed")
    if not client or not all(client.get(k) for k in ("client_id", "client_secret")):
        parser.error("Cần OAuth client loại Desktop app, không phải service account.")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            valid = query.get("state", [""])[0] == state and bool(query.get("code"))
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Đã nhận đăng nhập; quay lại cửa sổ terminal." if valid else "Đăng nhập không hợp lệ.").encode())
            if valid:
                result["code"] = query["code"][0]
                threading.Thread(target=server.shutdown, daemon=True).start()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    query = urllib.parse.urlencode({"client_id": client["client_id"], "redirect_uri": redirect,
        "response_type": "code", "scope": "https://www.googleapis.com/auth/drive.readonly",
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("Trình duyệt sẽ mở đăng nhập Google. Không gửi token vào Telegram.")
    auth_file = Path("config/google-auth-url.txt")
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text("https://accounts.google.com/o/oauth2/v2/auth?" + query, encoding="utf-8")
    if args.no_open_browser:
        print("URL đăng nhập nằm trong " + str(auth_file) + ". Không chia sẻ URL OAuth.", flush=True)
    else:
        webbrowser.open("https://accounts.google.com/o/oauth2/v2/auth?" + query)
    timer = threading.Timer(300, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        timer.cancel()
        server.server_close()
        auth_file.unlink(missing_ok=True)
    if not result.get("code"):
        raise SystemExit("Chưa nhận đăng nhập sau 5 phút. Chạy lại lệnh.")
    data = urllib.parse.urlencode({"code": result["code"], "client_id": client["client_id"],
        "client_secret": client["client_secret"], "redirect_uri": redirect,
        "grant_type": "authorization_code", "code_verifier": verifier}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=data), timeout=30) as r:
            token = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Google token endpoint trả HTTP {e.code}; thử đăng nhập lại.") from None
    if not token.get("refresh_token"):
        raise SystemExit("Google chưa cấp refresh token. Thu hồi quyền ứng dụng và đăng nhập lại.")
    obj = {"type": "authorized_user", "client_id": client["client_id"],
           "client_secret": client["client_secret"], "refresh_token": token["refresh_token"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    args.output.chmod(0o600)
    print("Đã lưu OAuth vào " + str(args.output.resolve()) + ". Đây là file bí mật.")


if __name__ == "__main__":
    main()
