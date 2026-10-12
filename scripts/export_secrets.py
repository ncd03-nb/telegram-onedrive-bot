"""Print Base64 for a selected file explicitly, to paste into the standby host secrets."""
import argparse
import base64
from pathlib import Path

parser = argparse.ArgumentParser(description="Xuất một file bí mật sang Base64. Chạy trong terminal riêng; không chia sẻ output.")
parser.add_argument("file", type=Path)
args = parser.parse_args()
print(base64.b64encode(args.file.read_bytes()).decode("ascii"))
