"""Fetch Telegram user ID locally; never persist or print the bot token."""
import getpass
import json
import re
import urllib.request


def main():
    token = getpass.getpass("Token bot mới từ BotFather (ẩn khi nhập): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Token không hợp lệ.")
    print("Gửi /start tới bot mới trên Telegram, rồi nhấn Enter ở đây.")
    input()
    try:
        request = urllib.request.Request("https://api.telegram.org/bot" + token + "/getUpdates",
            data=json.dumps({"timeout": 0, "allowed_updates": ["message"]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as r:
            obj = json.load(r)
        users = {u["message"]["from"]["id"] for u in obj.get("result", []) if u.get("message", {}).get("chat", {}).get("type") == "private"}
        print("User ID nhận được: " + ", ".join(map(str, sorted(users))) if users else "Chưa có tin nhắn. Gửi /start và thử lại.")
    except Exception as e:
        raise SystemExit("Không đọc được Telegram updates (" + type(e).__name__ + "). Bot phải chưa chạy / chưa đặt webhook.") from None


if __name__ == "__main__":
    main()
