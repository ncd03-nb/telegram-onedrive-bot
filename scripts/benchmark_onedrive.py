"""Bounded runner upload experiment. Only creates and recycles its own UUID files."""
import base64
import configparser
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def main():
    results = []
    with tempfile.TemporaryDirectory(prefix='od-benchmark-') as folder:
        root = Path(folder)
        config = root / 'rclone.conf'
        config.write_bytes(base64.b64decode(os.environ['RCLONE_CONFIG_B64'], validate=True))
        config.chmod(0o600)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(config, encoding='utf-8-sig')
        legacy = bool(parser.get('onedrive', 'tenant_url', fallback=''))
        source = root / 'random.bin'
        size = 512 * 1024**2
        with source.open('wb') as out:
            for _ in range(512):
                out.write(os.urandom(1024**2))
        prefix = 'onedrive:TelegramUploads/speed-test-' + uuid.uuid4().hex
        common = ['--config', str(config), '--stats', '0', '--log-level', 'ERROR',
                  '--retries', '1', '--low-level-retries', '3', '--timeout', '2m']

        def command(args, **kwargs):
            return subprocess.run(['rclone', *args, *common], capture_output=True,
                                  timeout=240, **kwargs)

        local = command(['hashsum', 'QuickXorHash', str(source)])
        if local.returncode:
            raise SystemExit('Local checksum calculation failed')
        expected = local.stdout.decode().split()[0].lower()
        cases = [('baseline', 50, False), ('large-chunk', 200 if legacy else 55, False),
                 ('large-http1', 200 if legacy else 55, True), ('baseline-http1', 50, True)]
        if os.environ.get('BENCH_PROFILE') == 'http1':
            cases = [case for case in cases if case[2]]
        for label, chunk, http1 in cases:
            dest = prefix + '/' + label + '.bin'
            result = {'case': label, 'chunk_mib': chunk, 'http1': http1, 'bytes': size}
            try:
                started = time.monotonic()
                with source.open('rb') as incoming:
                    proc = command(['rcat', dest, '--size', str(size), '--buffer-size', '1Mi',
                                    '--onedrive-chunk-size', f'{chunk}Mi',
                                    *(['--disable-http2'] if http1 else [])], stdin=incoming)
                elapsed = time.monotonic() - started
                result.update(seconds=round(elapsed, 3), mib_s=round(size / 1024**2 / elapsed, 2))
                if proc.returncode:
                    result['status'] = 'upload-failed'
                    # Report classifications only; provider errors may contain private URLs.
                    err = proc.stderr.decode(errors='replace').lower()
                    result['throttled'] = '429' in err or 'throttl' in err
                else:
                    verified = command(['hashsum', 'QuickXorHash', dest])
                    actual = verified.stdout.decode().split()
                    result['status'] = 'verified' if verified.returncode == 0 and actual and actual[0].lower() == expected else 'checksum-failed'
            except subprocess.TimeoutExpired:
                result['status'] = 'timeout'
            finally:
                # No purge/hard-delete: recycle only this experiment's exact file.
                removed = command(['deletefile', dest])
                result['recycled'] = removed.returncode == 0
                results.append(result)
                print(json.dumps(result), flush=True)
        command(['rmdir', prefix])
    Path(os.environ.get('BENCH_RESULT', '/result/onedrive-speed.json')).write_text(
        json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
