"""Subprocess fixture: exercises real pipe backpressure, cancellation and completion."""
import json
import sys
from pathlib import Path

args = sys.argv[1:]
config = Path(args[args.index("--config") + 1])
command = args[0]
def local(dest):
    return config.parent / "output" / dest.split(":", 1)[1]

if command == "rcat":
    dest = local(args[1])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        while data := sys.stdin.buffer.read(8192):
            f.write(data)
    if config.read_text() == "upload-fail":
        print("401 unauthorized", file=sys.stderr)
        sys.exit(1)
elif command == "copyto":
    dest = local(args[2])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(Path(args[1]).read_bytes())
elif command == "lsjson":
    count = local(args[1]).stat().st_size
    print(json.dumps({"Size": count + (1 if config.read_text() == "wrongsize" else 0), "IsDir": False}))
elif command == "about":
    print(json.dumps({"total": 20 * 1024**3, "free": 15 * 1024**3}))
else:
    sys.exit(2)
