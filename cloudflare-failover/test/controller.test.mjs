import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Miniflare} from 'miniflare';
import {build} from 'esbuild';
import {gzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import {seal,unseal,indexURL} from '../src/services.mjs';
import {readFile} from 'node:fs/promises';

const credentials={token:'test-only-token',github:'test-only-github',repo:'owner/repo',destination:'onedrive:TelegramUploads',drive_id:'test-drive',client_id:'test-client',client_secret:'test-secret',refresh_token:'test-refresh'};
async function setup(t,bot='payload',jobs=[],outputs=[]){
 const calls=[];const config={payload:{host_key:'host',webhook_key:'webhook',users:[123],public:true,groups:true,slash_only:true,username:'payloadbot'},onedrive:{host_key:'host',webhook_key:'webhook',users:[123],public:false,groups:false,username:'odrivebot'}};
 const bundle=await build({entryPoints:[new URL('../src/index.mjs',import.meta.url).pathname.replace(/^\/(\w:)/,'$1')],bundle:true,format:'esm',write:false,external:['cloudflare:workers']});
 const mf=new Miniflare({modules:true,script:bundle.outputFiles[0].text,compatibilityDate:'2026-08-01',durableObjects:{BOT_STATE:{className:'BotState',useSQLite:true}},bindings:{BOTS_CONFIG:JSON.stringify(config),PAYLOAD_CREDENTIALS:JSON.stringify(credentials),ONEDRIVE_CREDENTIALS:JSON.stringify(credentials)},outboundService:async request=>{
  const url=new URL(request.url);const body=request.method==='GET'?null:await request.text();calls.push({url:url.href,method:request.method,body,headers:Object.fromEntries(request.headers)});
  if(url.hostname==='api.telegram.org')return Response.json({ok:true,result:{message_id:900}});
  if(url.hostname==='api.github.com'){
   if(url.pathname.endsWith('/dispatches'))return new Response(null,{status:204});
   return Response.json({workflow_runs:[]});
  }
  if(url.hostname==='login.microsoftonline.com')return Response.json({access_token:'test-access',expires_in:3600,refresh_token:'test-refreshed'});
  if(url.hostname==='graph.microsoft.com'){
   if(request.method==='DELETE')return new Response(null,{status:204});
   return Response.json({id:'file-id',name:'rom.zip',file:{},size:1024,eTag:'changed-metadata',cTag:'original-content',webUrl:'https://tenant.sharepoint.com/file', '@microsoft.graph.downloadUrl':'https://tenant.sharepoint.com/download?fake=1'});
  }
  throw new Error('Unexpected test fetch: '+url.hostname);
 }});
 t.after(()=>mf.dispose());
 const api=async(action,body={})=>mf.dispatchFetch('https://hub/api/'+bot+'/'+action,{method:'POST',headers:{Authorization:'Bearer host','Content-Type':'application/json'},body:JSON.stringify(body)});
 const tables={};for(const [name,rows] of Object.entries({jobs,outputs,updates:[],scans:[]})){
  const columns=rows.length?Object.keys(rows[0]):name==='jobs'?['id','state']:name==='outputs'?['id','owner','files']:name==='updates'?['id','state']:['id','owner','url','metadata','created'];
  tables[name]={columns,rows:rows.map(r=>columns.map(c=>r[c]??null))};
 }
 const raw=gzipSync(JSON.stringify({version:1,tables}));const host={instance:'a'.repeat(32),role:'docker',epoch:null,snapshot:raw.toString('base64'),digest:createHash('sha256').update(raw).digest('hex')};
 const granted=await (await api('tick',host)).json();assert.equal(granted.granted,true);
 assert.equal((await api('prepare')).status,200);
 assert.equal((await (await api('tick',{instance:'b'.repeat(32),role:'render',epoch:null})).json()).granted,false);
 assert.equal((await api('activate')).status,409);
 await api('release',{...host,epoch:granted.epoch});assert.equal((await api('activate')).status,200);
 const webhook=async(update)=>mf.dispatchFetch('https://hub/telegram/'+bot,{method:'POST',headers:{'Content-Type':'application/json','X-Telegram-Bot-Api-Secret-Token':'webhook'},body:JSON.stringify(update)});
 return {mf,api,webhook,calls};
}
const update=(text,uid=1)=>({update_id:uid,message:{message_id:7,message_thread_id:3,from:{id:123,is_bot:false},chat:{id:-10022,type:'supergroup'},text}});
async function waitFor(fn){for(let attempt=0;attempt<60;attempt++){if(await fn())return;await new Promise(r=>setTimeout(r,50));}throw new Error('Test processing deadline');}

test('migration preserves Drive results and retires every old controller',async t=>{
 const job={id:'0123456789ab',update_id:1,owner:123,chat:123,state:'done',created:1,run_id:987,dispatch_at:2};
 const file={id:'drive-file',name:'payload.zip',size:1024,url:'https://drive.google.com/uc?id=test',members:[{name:'boot.img'}],rom_version:'OS4.test'};
 const {api,webhook,calls}=await setup(t,'payload',[job],[{id:job.id,owner:123,files:JSON.stringify([file])}]);
 const status=await (await api('status')).json();assert.equal(status.leader,'cloudflare');
 assert.equal((await (await api('tick',{instance:'c'.repeat(32),role:'docker',epoch:null})).json()).retired,true);
 await webhook(update('/files '+job.id,10));await waitFor(()=>calls.some(c=>c.body?.includes('payload.zip')));
 const message=JSON.parse(calls.find(c=>c.body?.includes('payload.zip')).body);assert.equal(message.chat_id,-10022);assert.equal(message.message_thread_id,3);assert.equal(message.reply_parameters.message_id,7);
 const audit=await (await api('audit')).json();assert.equal(audit.outputs,1);assert.equal(audit.jobs[0].run_id,987);
});
test('slash-only encrypted dispatch, owner checking and completion deduplication',async t=>{
 const {api,webhook,calls}=await setup(t);
 await webhook(update('https://facebook.com/example',20));await webhook(update('/extract@otherbot https://example.org boot',21));assert.equal(calls.length,0);
 const u=update('/extract https://example.org/rom.zip?private=fixture boot,init_boot',22);await webhook(u);await webhook(u);
 await waitFor(()=>calls.some(c=>c.url.endsWith('/dispatches')));
 const dispatches=calls.filter(c=>c.url.endsWith('/dispatches'));assert.equal(dispatches.length,1);
 const input=JSON.parse(dispatches[0].body).inputs;assert.deepEqual(Object.keys(input).sort(),['job_id','job_payload']);
 assert.ok(!dispatches[0].body.includes('private=fixture'));const job=await unseal(credentials.token,input.job_payload,input.job_id);
 assert.equal(job.chat,-10022);assert.equal(job.partitions,'boot,init_boot');
 const bad={id:job.id,owner:999,state:'done',files:[]};assert.equal((await api('result',{id:job.id,result:{sealed:await seal(credentials.token,bad,job.id)}})).status,400);
 const file={name:'payload.zip',size:123,url:'https://drive.google.com/uc?id=fixture',members:[{name:'boot.img'},{name:'init_boot.img'}],rom_version:'OS4.test'};
 const done={id:job.id,owner:123,state:'done',files:[file],notified:false};const body={id:job.id,result:{sealed:await seal(credentials.token,done,job.id)}};
 assert.equal((await api('result',body)).status,200);assert.equal((await api('result',body)).status,200);
 assert.equal(calls.filter(c=>c.body?.includes('✅ Hoàn thành')).length,1);
 const audit=await (await api('audit')).json();assert.equal(audit.jobs.length,1);assert.equal(audit.jobs[0].state,'done');assert.equal(audit.outputs,1);
});
test('12-hour deletion protects changed eTags; Index Link uses content identity',async t=>{
 const expires=Date.now()/1000-1;const job={id:'111111111111',update_id:3,owner:123,chat:123,state:'done',created:1,completed:expires-43200,expires,destination:'onedrive:TelegramUploads/111111111111/rom.zip',total:1024,file_id:'file-id',etag:'original-etag',link_ctag:'original-content',retention:'armed',manifest:1};
 const {api,calls,mf}=await setup(t,'onedrive',[job]);
 await waitFor(()=>calls.some(c=>c.url.includes('graph.microsoft.com')));
 assert.equal(calls.filter(c=>c.method==='DELETE').length,0);
 const url=await indexURL(credentials,job,'https://hub');assert.equal((await mf.dispatchFetch(url)).status,410);
 const audit=await (await api('audit')).json();assert.equal(audit.jobs[0].expires,expires);assert.equal(audit.jobs[0].etag,'original-etag');assert.equal(audit.jobs[0].state,'done');
});
test('authenticated envelopes reject cross-job replay and tampering',async()=>{
 const value=await seal(credentials.token,{id:'0123456789ab',url:'https://private.example'},'0123456789ab');
 assert.equal((await unseal(credentials.token,value,'0123456789ab')).id,'0123456789ab');
 await assert.rejects(unseal(credentials.token,value,'222222222222'));await assert.rejects(unseal('other-token',value,'0123456789ab'));
});
test('Python AES-GCM envelopes are readable by the Cloudflare implementation',async()=>{
 const fixture=JSON.parse(await readFile(new URL('./python-envelope.json',import.meta.url),'utf8'));
 const obj=await unseal('fixture-key',fixture.envelope,'0123456789ab');assert.equal(obj.url,'https://example.org/private?token=fixture');
});
