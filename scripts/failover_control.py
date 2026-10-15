"""Failover cutover/status helper; credentials never appear in output."""
import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from setup_failover import read_env,key


def request(url,body=None,headers=None):
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json','User-Agent':'BotFailoverAdmin/1.0',**(headers or {})})
    try:
        with urllib.request.urlopen(req,timeout=30) as response:
            raw=response.read();return json.loads(raw) if raw else {}
    except Exception as error:
        raise SystemExit('Request failed: '+str(getattr(error,'code',type(error).__name__))) from None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['configure-docker','set-webhook','status','probe','prepare','activate','audit'])
    parser.add_argument('--repo',type=Path,default=Path.cwd())
    parser.add_argument('--bot',choices=['onedrive','payload'],required=True)
    parser.add_argument('--hub',default='https://telegram-bot-failover.der-templates.workers.dev')
    parser.add_argument('--text',default='/status')
    args=parser.parse_args();root=args.repo.resolve();env=read_env(root/'.env');token=env['TELEGRAM_BOT_TOKEN']
    origin=args.hub.rstrip('/');host_headers={'Authorization':'Bearer '+key(token,'bot-failover-host-v1')}
    if args.action=='configure-docker':
        values={'HUB_URL':origin,'HUB_BOT_ID':args.bot,'HUB_ROLE':'docker'}
        if args.bot=='onedrive':values.update(BOT_MODE='hub',TRANSFER_BACKEND='actions')
        lines=[x for x in (root/'.env').read_text(encoding='utf-8-sig').splitlines() if x.split('=',1)[0] not in values]
        (root/'.env').write_text('\n'.join(lines)+'\n'+''.join(k+'='+v+'\n' for k,v in values.items()),encoding='utf-8')
        (root/'config/render-hub.env').write_text(''.join(k+'='+('render' if k=='HUB_ROLE' else v)+'\n' for k,v in values.items()),encoding='utf-8')
        print('Configured Docker and public Render hub settings:',args.bot);return
    status=request(origin+'/api/'+args.bot+'/status',{},host_headers)
    if args.action in {'prepare','activate','audit'}:
        print(json.dumps(request(origin+'/api/'+args.bot+'/'+args.action,{},host_headers)));return
    if args.action=='set-webhook':
        if not status['initialized'] or status['leader']!='docker':raise SystemExit('Refused cutover: Docker must first synchronize its existing state.')
        response=request('https://api.telegram.org/bot'+token+'/setWebhook',{
            'url':origin+'/telegram/'+args.bot,'secret_token':key(token,'bot-failover-webhook-v1'),
            'allowed_updates':['message'],'drop_pending_updates':False,'max_connections':2})
        if not response.get('ok'):raise SystemExit('Telegram webhook rejected')
        print('Permanent Cloudflare webhook configured:',args.bot)
    elif args.action=='probe':
        owner=next(int(x) for x in env['ALLOWED_USER_IDS'].split(',') if x.strip())
        identifier=-int(time.time()*1000)
        update={'update_id':identifier,'message':{'message_id':0,'date':int(time.time()),
            'from':{'id':owner,'is_bot':False,'first_name':'Cloudflare controller check'},'chat':{'id':owner,'type':'private'},'text':args.text}}
        request(origin+'/telegram/'+args.bot,update,{'X-Telegram-Bot-Api-Secret-Token':key(token,'bot-failover-webhook-v1')})
        print(json.dumps({'bot':args.bot,'probe_update':identifier}))
    else:
        webhook=request('https://api.telegram.org/bot'+token+'/getWebhookInfo',{})
        if not webhook.get('ok'):raise SystemExit('Telegram webhook status unavailable')
        info=webhook['result']
        print(json.dumps({'bot':args.bot,'hub':status,'webhook_url':info['url'],
            'telegram_pending':info['pending_update_count']}))


if __name__=='__main__':main()
