"""Prepare private Worker credentials from existing authorized local configuration."""
import argparse
import configparser
import json
import re
import subprocess
import urllib.request
from pathlib import Path
from setup_failover import read_env


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--payload',type=Path,required=True);args=parser.parse_args()
    root=Path.cwd();env=read_env(root/'.env');payload=read_env(args.payload/'.env')
    source=urllib.request.urlopen('https://raw.githubusercontent.com/rclone/rclone/v1.75.1/backend/onedrive/onedrive.go',timeout=30).read().decode()
    default_id=re.search(r'rcloneClientID\s*=\s*"([^"]+)"',source).group(1)
    encrypted=re.search(r'rcloneEncryptedClientSecret\s*=\s*"([^"]+)"',source).group(1)
    secret=subprocess.run(['docker','compose','exec','-T','bot','rclone','reveal',encrypted],capture_output=True,text=True,check=True).stdout.strip()
    cfg=configparser.ConfigParser(interpolation=None);cfg.read(root/'config/rclone.conf',encoding='utf-8-sig');remote=cfg['onedrive'];token=json.loads(remote['token'])
    onedrive={'token':env['TELEGRAM_BOT_TOKEN'],'github':env['ACTIONS_DISPATCH_TOKEN'],'repo':env['ACTIONS_REPO'],
        'destination':env.get('ONEDRIVE_DEST','onedrive:TelegramUploads'),'drive_id':remote['drive_id'],
        'client_id':remote.get('client_id') or default_id,'client_secret':remote.get('client_secret') or secret,
        'refresh_token':token['refresh_token'],'max_file_gib':int(env.get('MAX_FILE_GIB','12')),
        'graph_base':remote['tenant_url'].rstrip('/')+'/v2.0' if remote.get('tenant_url') else 'https://graph.microsoft.com/v1.0'}
    payload_values={'token':payload['TELEGRAM_BOT_TOKEN'],'github':payload['ACTIONS_DISPATCH_TOKEN'],'repo':payload['ACTIONS_REPO']}
    values=json.loads((root/'config/failover-secrets.json').read_text())
    values.update(ONEDRIVE_CREDENTIALS=json.dumps(onedrive,separators=(',',':')),PAYLOAD_CREDENTIALS=json.dumps(payload_values,separators=(',',':')))
    if any(len(value.encode())>5000 for value in values.values()):raise SystemExit('Worker secret exceeds reviewed size limit.')
    (root/'config/cloudflare-controller-secrets.json').write_text(json.dumps(values),encoding='utf-8')
    print('Prepared private Cloudflare credentials for both controllers; no Google write token is copied to the Worker.')


if __name__=='__main__':main()
