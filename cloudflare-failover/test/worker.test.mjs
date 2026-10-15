import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Miniflare} from 'miniflare';
import {createHash} from 'node:crypto';
import {gzipSync} from 'node:zlib';
import {build} from 'esbuild';

test('durable webhook queue, atomic checkpoint, fencing and callbacks',async()=>{
 const config={onedrive:{host_key:'host',webhook_key:'webhook',users:[123],username:'testbot',groups:false,public:false},
 payload:{host_key:'payload',webhook_key:'payloadhook',users:[],username:'payloadbot',groups:true,public:true,slash_only:true}};
 const bundle=await build({entryPoints:[new URL('../src/index.mjs',import.meta.url).pathname.replace(/^\/(\w:)/,'$1')],bundle:true,format:'esm',write:false,external:['cloudflare:workers']});
 const mf=new Miniflare({modules:true,script:bundle.outputFiles[0].text,
 compatibilityDate:'2026-08-01',durableObjects:{BOT_STATE:{className:'BotState',useSQLite:true}},bindings:{BOTS_CONFIG:JSON.stringify(config)}});
 const call=async(path,body,key='host')=>mf.dispatchFetch('https://hub'+path,{method:'POST',headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},body:JSON.stringify(body)});
 const telegram=async(bot,body,key)=>mf.dispatchFetch('https://hub/telegram/'+bot,{method:'POST',headers:{'X-Telegram-Bot-Api-Secret-Token':key,'Content-Type':'application/json'},body:JSON.stringify(body)});
 try{
  assert.equal((await call('/api/onedrive/tick',{},'bad')).status,403);
  const update={update_id:1234,message:{from:{id:123},chat:{id:123,type:'private'},text:'/status'}};
  assert.equal((await telegram('onedrive',update,'bad')).status,403);
  assert.equal((await telegram('onedrive',update,'webhook')).status,204);
  assert.equal((await telegram('onedrive',update,'webhook')).status,204);
  let status=await (await call('/api/onedrive/status',{})).json();assert.equal(status.pending,1);
  const raw=gzipSync(JSON.stringify({version:1,tables:{}}));
  const seed={snapshot:raw.toString('base64'),digest:createHash('sha256').update(raw).digest('hex')};
  const first={instance:'a'.repeat(32),role:'docker',epoch:null,...seed};
  let granted=await (await call('/api/onedrive/tick',first)).json();assert.equal(granted.granted,true);assert.equal(granted.updates.length,1);
  assert.equal((await (await call('/api/onedrive/tick',{instance:'b'.repeat(32),role:'render',epoch:null})).json()).granted,false);
  const active={...first,epoch:granted.epoch};
  assert.equal((await (await call('/api/onedrive/save',{...active,digest:'0'.repeat(64)})).json()).error,'snapshot checksum');
  granted=await (await call('/api/onedrive/save',{...active,acked_updates:[1234]})).json();assert.equal(granted.updates.length,0);
  await call('/api/onedrive/release',active);
  const other={...first,instance:'c'.repeat(32)};granted=await (await call('/api/onedrive/tick',other)).json();assert.equal(granted.granted,true);
  assert.equal((await (await call('/api/onedrive/save',active)).json()).granted,false);
  assert.equal((await call('/api/onedrive/result',{id:'0123456789ab'})).status,200);
  granted=await (await call('/api/onedrive/tick',{...other,epoch:granted.epoch})).json();assert.deepEqual(granted.events,['r:0123456789ab']);
  await call('/api/onedrive/save',{...other,epoch:granted.epoch,acked_events:granted.events});
  status=await (await call('/api/onedrive/status',{})).json();assert.equal(status.pending,0);
  await call('/api/onedrive/result',{id:'0123456789ab'});
  status=await (await call('/api/onedrive/status',{})).json();assert.equal(status.pending,1);
  await call('/api/onedrive/save',{...other,epoch:granted.epoch,acked_events:['r:0123456789ab']});
  // Bare links and commands for another bot never reach the payload queue.
  update.message.text='https://facebook.com/example';await telegram('payload',update,'payloadhook');
  update.message.text='/extract@otherbot https://example.org boot';await telegram('payload',update,'payloadhook');
  status=await (await call('/api/payload/status',{},'payload')).json();assert.equal(status.pending,0);
  update.message.text='/status';await telegram('payload',update,'payloadhook');
  status=await (await call('/api/payload/status',{},'payload')).json();assert.equal(status.pending,1);
 }finally{await mf.dispose();}
});
