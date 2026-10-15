"""Configure existing bot repository Actions secrets, without printing credentials."""
import argparse
import base64
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--use-git-credential', action='store_true')
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--repo', default='ncd03-nb/telegram-onedrive-bot')
    args = parser.parse_args()
    if args.token_file:
        token = args.token_file.read_text().strip()
    elif args.use_git_credential:
        proc = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
            text=True, capture_output=True, check=True)
        token = dict(line.split('=', 1) for line in proc.stdout.splitlines() if '=' in line)['password']
    else:
        parser.error('Specify --token-file or --use-git-credential')
    from nacl.public import PublicKey, SealedBox
    base = 'https://api.github.com/repos/' + args.repo
    def api(method, endpoint, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(base + endpoint, data=data, method=method,
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise SystemExit(f'GitHub HTTP {e.code}; token needs repo Actions/secrets permissions.') from None
    repo = api('GET', '')
    if repo.get('full_name') != args.repo:
        raise SystemExit('Unexpected bot repo; refused to configure another target.')
    env_path = Path('.env')
    lines = env_path.read_text(encoding='utf-8-sig').splitlines()
    env = dict(x.split('=', 1) for x in lines if x and not x.startswith('#') and '=' in x)
    secrets = {'TELEGRAM_BOT_TOKEN': env['TELEGRAM_BOT_TOKEN'].strip().strip('\"\''),
        'ALLOWED_USER_IDS': env['ALLOWED_USER_IDS'],
        'RCLONE_CONFIG_B64': base64.b64encode(Path('config/rclone.conf').read_bytes()).decode(),
        'GOOGLE_TOKEN_B64': base64.b64encode(Path('config/google-token.json').read_bytes()).decode()}
    key = api('GET', '/actions/secrets/public-key')
    box = SealedBox(PublicKey(base64.b64decode(key['key'])))
    for name, value in secrets.items():
        api('PUT', '/actions/secrets/' + name, {'key_id': key['key_id'],
            'encrypted_value': base64.b64encode(box.encrypt(value.encode())).decode()})
        print('Configured secret: ' + name)
    updates = {'TRANSFER_BACKEND': 'actions', 'ACTIONS_REPO': args.repo,
        'ACTIONS_REF': 'main', 'ACTIONS_DISPATCH_TOKEN': token}
    lines = [x for x in lines if x.split('=', 1)[0] not in updates]
    env_path.write_text('\n'.join(lines) + '\n' + '\n'.join(k + '=' + v for k, v in updates.items()) + '\n', encoding='utf-8')
    env_path.chmod(0o600)
    # A private import file for the existing Render service; never print its value.
    render = Path('config/render-actions.env')
    render.write_text('\n'.join(k + '=' + v for k,v in updates.items()) + '\n', encoding='utf-8')
    render.chmod(0o600)
    print('Configured local controller. Render import file: config/render-actions.env (PRIVATE).')


if __name__ == '__main__':
    main()
