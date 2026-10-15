"""Wake the controller after artifact upload, including a failed container build."""
import hmac
import json
import os
import re
import time
import urllib.request
from urllib.parse import urlsplit


def main():
    origin=os.getenv('HUB_URL','').rstrip('/')
    if not origin:return
    parsed=urlsplit(origin);bot=os.environ['HUB_BOT_ID'];job=os.environ['JOB_ID']
    if (parsed.scheme!='https' or not (parsed.hostname or '').endswith('.workers.dev') or parsed.path
            or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port not in (None,443)
            or bot not in {'onedrive','payload'} or not re.fullmatch('[a-f0-9]{12}',job)):
        raise SystemExit('Invalid controller callback configuration')
    secret=hmac.digest(os.environ['TELEGRAM_BOT_TOKEN'].encode(),b'bot-failover-host-v1','sha256').hex()
    for attempt in range(3):
        result=None
        if os.getenv('RESULT_PATH'):
            from pathlib import Path
            path=Path(os.environ['RESULT_PATH'])
            if path.is_file() and path.stat().st_size<=180000:result=json.loads(path.read_text())
        request=urllib.request.Request(origin+'/api/'+bot+'/result',data=json.dumps({'id':job,'result':result}).encode(),
            headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json','User-Agent':'BotFailoverCallback/1.0'})
        try:
            with urllib.request.urlopen(request,timeout=20) as response:
                if response.status==200:return
        except Exception:pass
        time.sleep(2**attempt)
    raise SystemExit('Controller callback unavailable; result artifact remains available for recovery.')


if __name__=='__main__':main()
