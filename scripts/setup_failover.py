"""Prepare private Render/Cloudflare imports and Actions callbacks. No secrets are printed."""
import argparse
import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from pathlib import Path


def read_env(path):
    return {k:v.strip().strip('\"\'') for line in path.read_text(encoding='utf-8-sig').splitlines()
            if line and not line.startswith('#') and '=' in line for k,v in [line.split('=',1)]}


def key(token,purpose):
    return hmac.digest(token.encode(),purpose.encode(),'sha256').hex()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--payload',type=Path,required=True)
    parser.add_argument('--hub',required=True)
    parser.add_argument('--payload-render',required=True)
    parser.add_argument('--configure-actions',action='store_true')
    args=parser.parse_args()
    roots={'onedrive':Path.cwd(),'payload':args.payload.resolve()}
    config={}
    for bot,root in roots.items():
        env=read_env(root/'.env');token=env['TELEGRAM_BOT_TOKEN']
        if not token:raise SystemExit('Missing bot token: '+bot)
        folder=root/'config'
        render={'TELEGRAM_BOT_TOKEN':token,'ALLOWED_USER_IDS':env.get('ALLOWED_USER_IDS',''),
            'ACTIONS_DISPATCH_TOKEN':env['ACTIONS_DISPATCH_TOKEN'],
            'ACTIONS_REPO':'ncd03-nb/telegram-'+bot+'-bot','ACTIONS_REF':'main',
            'HUB_URL':args.hub.rstrip('/'),'HUB_BOT_ID':bot,'HUB_ROLE':'render',
            'DATA_DIR':'/data','PORT':'8080','GOOGLE_TOKEN_PATH':'/config/google-token.json',
            'GOOGLE_TOKEN_B64':base64.b64encode((folder/'google-token.json').read_bytes()).decode()}
        fallback=args.payload_render.rstrip('/') if bot=='payload' else 'https://telegram-onedrive-standby.onrender.com'
        if bot=='onedrive':
            render.update(BOT_MODE='hub',TRANSFER_BACKEND='actions',RCLONE_CONFIG='/config/rclone.conf',
                RCLONE_CONFIG_B64=base64.b64encode((folder/'rclone.conf').read_bytes()).decode(),
                DOWNLOAD_BASE_URL=fallback,MAX_FILE_GIB=env.get('MAX_FILE_GIB','12'),
                RETENTION_HOURS='12',CLEANUP_INTERVAL_SECONDS='60',MAX_SPOOL_MIB='0')
        else:
            for name,default in {'PUBLIC_BOT':'true','ALLOW_GROUPS':'true','PUBLIC_FILES':'true',
                    'MAX_CONCURRENT_JOBS':'0','MAX_OUTPUT_GIB':'40','EXTRACT_WORKERS':'8',
                    'RANGE_BATCH_MIB':'8','DECODE_WORKERS':'2','DRIVE_CHUNK_MIB':'32'}.items():
                render[name]=env.get(name,default)
        (folder/'render-failover.env').write_text(''.join(k+'='+v+'\n' for k,v in render.items()),encoding='utf-8')
        config[bot]={'host_key':key(token,'bot-failover-host-v1'),
            'webhook_key':key(token,'bot-failover-webhook-v1'),'fallback_url':fallback,
            'users':[int(x) for x in env.get('ALLOWED_USER_IDS','').split(',') if x.strip()],
            'public':bot=='payload','groups':bot=='payload','slash_only':bot=='payload',
            'username':'odrivegay_bot' if bot=='onedrive' else 'pdoplus_bot'}
        if args.configure_actions:
            base='https://api.github.com/repos/'+render['ACTIONS_REPO']+'/actions/variables'
            headers={'Authorization':'Bearer '+env['ACTIONS_DISPATCH_TOKEN'],'Accept':'application/vnd.github+json','Content-Type':'application/json'}
            try:
                with urllib.request.urlopen(urllib.request.Request(base+'/HUB_URL',headers=headers),timeout=30):pass
                method,url='PATCH',base+'/HUB_URL'
            except urllib.error.HTTPError as e:
                if e.code!=404:raise SystemExit('GitHub variable read HTTP '+str(e.code)) from None
                method,url='POST',base
            request=urllib.request.Request(url,method=method,headers=headers,data=json.dumps({'name':'HUB_URL','value':args.hub}).encode())
            try:
                with urllib.request.urlopen(request,timeout=30):pass
            except urllib.error.HTTPError as e:raise SystemExit('GitHub variable write HTTP '+str(e.code)) from None
        print('Prepared private Render import and callback configuration:',bot)
    (roots['onedrive']/'config/failover-secrets.json').write_text(json.dumps({'BOTS_CONFIG':json.dumps(config,separators=(',',':'))}),encoding='utf-8')
    print('Prepared private Cloudflare secret import.')


if __name__=='__main__':main()
