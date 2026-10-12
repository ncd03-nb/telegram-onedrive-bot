"""Local rclone OAuth callback; never prints tokens or writes them into Git."""
import argparse
import base64
import configparser
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def extract_token(output):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", output):
        try:
            obj, _ = decoder.raw_decode(output[match.start():])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("access_token") and obj.get("refresh_token"):
            return obj
    raise ValueError("OAuth chưa trả đủ token.")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Kết nối OneDrive bằng OAuth chính thức trên máy của bạn.")
    parser.add_argument("--rclone", default="rclone")
    parser.add_argument("--output", type=Path, default=Path("config/rclone.conf"))
    parser.add_argument("--auth-url", type=Path, default=Path("config/onedrive-auth-url.txt"))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Config đã tồn tại. Đổi --output nếu muốn tạo cấu hình mới.")
    args.auth_url.parent.mkdir(parents=True, exist_ok=True)
    # disable_site_permission overrides custom scopes in rclone; omit it here.
    options = {"access_scopes": "Files.ReadWrite offline_access User.Read"}
    encoded = base64.urlsafe_b64encode(json.dumps(options).encode()).rstrip(b"=").decode()
    process = subprocess.Popen([args.rclone, "authorize", "onedrive", encoded, "--auth-no-open-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    captured = []
    try:
        for line in process.stdout:
            captured.append(line)
            match = re.search(r"http://(?:127\.0\.0\.1|localhost):\d+/auth\?[^\s]+", line)
            if match:
                args.auth_url.write_text(match.group(), encoding="utf-8")
                print("Đã chuẩn bị URL đăng nhập trong " + str(args.auth_url) + ".", flush=True)
                print("Mở URL bằng trình duyệt rồi duyệt quyền Microsoft. Không chia sẻ URL OAuth.", flush=True)
        if process.wait() != 0:
            raise ValueError("rclone OAuth chưa hoàn tất; kiểm tra màn hình Microsoft / quyền trường.")
        token = extract_token("".join(captured))
        request = urllib.request.Request("https://graph.microsoft.com/v1.0/me/drive?$select=id,driveType,name",
            headers={"Authorization": "Bearer " + token["access_token"]})
        with urllib.request.urlopen(request, timeout=30) as response:
            drive = json.load(response)
        if drive.get("driveType") not in {"business", "personal", "documentLibrary"} or not drive.get("id"):
            raise ValueError("Không tìm thấy OneDrive cá nhân của tài khoản.")
        config = configparser.ConfigParser(interpolation=None)
        config["onedrive"] = {"type": "onedrive", "token": json.dumps(token), "drive_id": drive["id"],
            "drive_type": drive["driveType"], **options}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as file:
            config.write(file)
        print("Đã lưu cấu hình OneDrive " + drive["driveType"] + " vào " + str(args.output) + ".", flush=True)
    except urllib.error.HTTPError as error:
        print("Microsoft Graph trả HTTP " + str(error.code) + "; chưa lưu config. Kiểm tra quyền được trường cấp.", flush=True)
        raise SystemExit(1) from None
    except (ValueError, OSError) as error:
        # Captured OAuth stdout may contain secrets; never include it in errors.
        print("Kết nối chưa hoàn tất (" + type(error).__name__ + "). Không có token nào được in ra.", flush=True)
        raise SystemExit(1) from None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        args.auth_url.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
