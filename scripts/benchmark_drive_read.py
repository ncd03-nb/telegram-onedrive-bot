"""Read-only download experiment; no Drive names/IDs/tokens enter logs."""
import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.source import Sources


async def main():
    with tempfile.TemporaryDirectory() as folder:
        token = Path(folder) / 'google-token.json'
        token.write_bytes(base64.b64decode(os.environ['GOOGLE_TOKEN_B64'], validate=True))
        token.chmod(0o600)
        sources = Sources(SimpleNamespace(google_token=token))
        await sources.start()
        try:
            auth = {'Authorization': 'Bearer ' + await sources.google_access()}
            async with sources.api.get('https://www.googleapis.com/drive/v3/files', headers=auth,
                    params={'q': 'trashed = false', 'fields': 'files(id,size)', 'pageSize': '100'}) as r:
                if r.status != 200:
                    raise RuntimeError('Metadata request failed')
                files = (await r.json()).get('files', [])
            candidate = next((f for f in files if int(f.get('size', 0)) >= 512 * 1024**2), None)
            if not candidate:
                print('No accessible large binary file for read-only benchmark'); return
            url = 'https://www.googleapis.com/drive/v3/files/' + candidate['id'] + '?alt=media&supportsAllDrives=true'
            block = 128 * 1024**2

            async def read_range(start, end):
                headers = {**auth, 'Range': f'bytes={start}-{end}'}
                async with sources.session.get(url, headers=headers, allow_redirects=False) as r:
                    if r.status != 206 or r.headers.get('Content-Range') != f'bytes {start}-{end}/{candidate["size"]}':
                        raise RuntimeError('Source did not honor exact Range')
                    digest = hashlib.sha256(); count = 0
                    async for data in r.content.iter_chunked(1024**2):
                        count += len(data); digest.update(data)
                    if count != end - start + 1:
                        raise RuntimeError('Truncated source')
                    return digest.hexdigest()

            results = []
            started = time.monotonic()
            serial = []
            for n in range(4):
                serial.append(await read_range(n * block, (n + 1) * block - 1))
            seconds = time.monotonic() - started
            results.append({'streams': 1, 'mib_s': round(512 / seconds, 2), 'seconds': round(seconds, 2)})
            print(json.dumps(results[-1]), flush=True)
            started = time.monotonic()
            parallel = await asyncio.gather(*(read_range(n * block, (n + 1) * block - 1) for n in range(4)))
            if serial != parallel:
                raise RuntimeError('Source changed or checksum mismatch')
            seconds = time.monotonic() - started
            results.append({'streams': 4, 'mib_s': round(512 / seconds, 2), 'seconds': round(seconds, 2), 'verified': True})
            print(json.dumps(results[-1]), flush=True)
            Path('/result/drive-read-speed.json').write_text(json.dumps(results), encoding='utf-8')
        finally:
            await sources.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:
        # Exception text from HTTP libraries can contain private URLs.
        raise SystemExit('Read-only benchmark failed: ' + type(error).__name__) from None
