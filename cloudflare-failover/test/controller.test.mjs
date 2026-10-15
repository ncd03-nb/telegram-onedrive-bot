import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Miniflare} from 'miniflare';
import {build} from 'esbuild';
import {gzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import {seal,unseal,indexURL} from '../src/services.mjs';
import {readFile} from 'node:fs/promises';

const credentials={token:'test-only-token',github:'test-only-github',repo:'owner/repo',destination:'onedrive:TelegramUploads',drive_id:'test-drive',client_id:'test-client',client_secret:'test-secret',refresh_token:'test-refresh'};
async function setup(t,bot='payload',jobs=[],outputs=[],options={}){
 const calls=[];const config={payload:{host_key:'host',webhook_key:'webhook',users:[123],public:true,groups:true,slash_only:true,username:'payloadbot'},onedrive:{host_key:'host',webhook_key:'webhook',users:[123],public:false,groups:false,username:'odrivebot'}};
 const bundle=await build({entryPoints:[new URL('../src/index.mjs',import.meta.url).pathname.replace(/^\/(\w:)/,'$1')],bundle:true,format:'esm',write:false,external:['cloudflare:workers']});
 const mf=new Miniflare({modules:true,script:bundle.outputFiles[0].text,compatibilityDate:'2026-08-01',durableObjects:{BOT_STATE:{className:'BotState',useSQLite:true}},bindings:{BOTS_CONFIG:JSON.stringify(config),PAYLOAD_CREDENTIALS:JSON.stringify(credentials),ONEDRIVE_CREDENTIALS:JSON.stringify({...credentials,...options.credentials})},outboundService:async request=>{
  const url=new URL(request.url);const body=request.method==='GET'?null:await request.text();calls.push({url:url.href,method:request.method,body,headers:Object.fromEntries(request.headers)});
  if(url.hostname==='api.telegram.org')return Response.json({ok:true,result:{message_id:899+calls.filter(c=>c.url.endsWith('/sendMessage')).length}});
  if(url.hostname==='api.github.com'){
   if(url.pathname.endsWith('/dispatches'))return new Response(null,{status:204});
   return Response.json({workflow_runs:[]});
  }
  if(url.hostname==='login.microsoftonline.com')return Response.json({access_token:'test-access',expires_in:3600,refresh_token:'test-refreshed'});
  if(url.hostname==='graph.microsoft.com'){
   if(request.method==='DELETE')return new Response(null,{status:204});
   return Response.json({id:'file-id',name:'rom.zip',file:{},size:1024,eTag:'changed-metadata',cTag:'original-content',webUrl:'https://tenant.sharepoint.com/file', '@microsoft.graph.downloadUrl':'https://tenant.sharepoint.com/download?fake=1'});
  }
  if(url.hostname==='archive.org'){
   if(url.pathname.startsWith('/download/'))return new Response(new Uint8Array(32),{status:206,headers:{'Content-Range':'bytes 0-31/1024'}});
   return Response.json({metadata:{identifier:url.pathname.split('/').at(-1)},files:options.missing?[]:[{name:'rom.zip',size:1024,md5:options.changed?'b'.repeat(32):'a'.repeat(32)}]});
  }
  if(url.hostname==='s3.us.archive.org'&&request.method==='DELETE')return new Response(null,{status:204});
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
 const done={id:job.id,owner:123,state:'done',files:[file],notified:false};const body={id:job.id,result:{sealed:await seal(credentials.token,done,job.id)},run_id:321};
 assert.equal((await api('result',body)).status,200);assert.equal((await api('result',body)).status,200);
 assert.equal(calls.filter(c=>c.body?.includes('✅ Hoàn thành')).length,1);
 const audit=await (await api('audit')).json();assert.equal(audit.jobs.length,1);assert.equal(audit.jobs[0].state,'done');assert.equal(audit.jobs[0].run_id,321);assert.equal(audit.outputs,1);
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
test('scan sends one final reply, paginates that reply and checks the owner',async t=>{
 const {api,webhook,calls}=await setup(t);
 await webhook(update('/list https://example.org/rom.zip',31));
 await waitFor(()=>calls.some(c=>c.url.endsWith('/dispatches')));
 const input=JSON.parse(calls.find(c=>c.url.endsWith('/dispatches')).body).inputs;
 const metadata=Array.from({length:400},(_,i)=>({name:'partition_'+i,size:1024**2,supported:true}));
 const result={id:input.job_id,owner:123,state:'done',url:'https://example.org/rom.zip',metadata,rom_version:'OS3.test'};
 const body={id:input.job_id,result:{sealed:await seal(credentials.token,result,input.job_id)}};
 assert.equal((await api('result',body)).status,200);await api('result',body);
 const sent=calls.filter(c=>c.url.endsWith('/sendMessage'));assert.equal(sent.length,2);
 assert.equal(JSON.parse(sent[0].body).reply_parameters,undefined);assert.equal(JSON.parse(sent[0].body).message_thread_id,3);
 assert.equal(JSON.parse(sent[1].body).reply_parameters.message_id,7);assert.equal(JSON.parse(sent[1].body).message_thread_id,3);
 const edits=()=>calls.filter(c=>c.url.endsWith('/editMessageText')&&JSON.parse(c.body).message_id===901);
 const first=JSON.parse(sent[1].body);assert.equal(first.chat_id,-10022);
 assert.ok(first.text.includes('400 phân vùng'));assert.ok(first.text.includes('partition_0'));assert.ok(first.text.length<=4000);
 const data=first.reply_markup.inline_keyboard.at(-1).at(-1).callback_data;
 const callback=(uid,owner,message=901)=>({update_id:uid,callback_query:{id:'q'+uid,from:{id:owner,is_bot:false},message:{message_id:message,chat:{id:-10022,type:'supergroup'}},data}});
 await webhook(callback(32,999));await waitFor(()=>calls.some(c=>c.body?.includes('q32')));assert.equal(edits().length,0);
 await webhook(callback(33,123,900));await waitFor(()=>calls.some(c=>c.body?.includes('q33')));assert.equal(edits().length,0);
 await webhook(callback(34,123));await waitFor(()=>edits().length===1);
 const next=JSON.parse(edits().at(-1).body);assert.equal(next.message_id,901);assert.ok(!next.text.includes('\npartition_0 —'));assert.ok(next.text.includes('Trang 2/'));assert.ok(next.text.length<=4000);
 assert.equal(calls.filter(c=>c.url.endsWith('/sendMessage')).length,2);
});
test('successful extraction sends its final download reply to the original user command',async t=>{
 const {api,webhook,calls}=await setup(t);await webhook(update('/extract https://example.org/rom.zip boot',41));
 await waitFor(()=>calls.some(c=>c.url.endsWith('/dispatches')));
 const input=JSON.parse(calls.find(c=>c.url.endsWith('/dispatches')).body).inputs;
 const result={id:input.job_id,owner:123,state:'done',notified:true,files:[{name:'boot.img',size:123,url:'https://drive.google.com/file',rom_version:'OS3.test'}]};
 assert.equal((await api('result',{id:input.job_id,result:{sealed:await seal(credentials.token,result,input.job_id)}})).status,200);
 const sends=calls.filter(c=>c.url.endsWith('/sendMessage'));assert.equal(sends.length,2);
 const final=JSON.parse(sends.at(-1).body);
 assert.equal(final.reply_parameters.message_id,7);assert.equal(final.message_thread_id,3);assert.ok(final.text.includes('boot.img'));assert.equal(final.reply_markup.inline_keyboard[0][0].url,result.files[0].url);
});
test('valid Index Link redirects without exposing credentials to download hosts',async t=>{
 const expires=Date.now()/1000+43200;const job={id:'333333333333',update_id:4,owner:123,chat:123,state:'done',created:1,completed:expires-43200,expires,destination:'onedrive:TelegramUploads/333333333333/rom.zip',total:1024,file_id:'file-id',etag:'original-etag',link_ctag:'original-content',retention:'armed',manifest:1};
 const {mf,calls}=await setup(t,'onedrive',[job]);const url=await indexURL(credentials,job,'https://hub');
 const response=await mf.dispatchFetch(url,{redirect:'manual'});assert.equal(response.status,302,await response.clone().text());assert.equal(response.headers.get('Location'),'https://tenant.sharepoint.com/download?fake=1');
 assert.equal(calls.filter(c=>c.url.startsWith('https://tenant.sharepoint.com')).length,0);
});
test('Python AES-GCM envelopes are readable by the Cloudflare implementation',async()=>{
 const fixture=JSON.parse(await readFile(new URL('./python-envelope.json',import.meta.url),'utf8'));
 const obj=await unseal('fixture-key',fixture.envelope,'0123456789ab');assert.equal(obj.url,'https://example.org/private?token=fixture');
});
test('Archive copy dispatch, final reply, Index redirect and legacy file routing coexist',async t=>{
 const {api,webhook,calls,mf}=await setup(t,'onedrive',[],[],{credentials:{storage_backend:'archive'}});
 const u=update('/copy https://example.org/rom.zip',61);u.message.chat={id:123,type:'private'};
 await webhook(u);await waitFor(()=>calls.some(c=>c.url.includes('/archive-copy.yml/dispatches')));
 const input=JSON.parse(calls.find(c=>c.url.includes('/archive-copy.yml/dispatches')).body).inputs;
 const j=await unseal(credentials.token,input.job_payload,input.job_id);assert.equal(j.storage,'archive');
 const identifier='odrivegay-'+j.id+'-'+'c'.repeat(16),completed=Date.now()/1000;
 const record={version:1,id:j.id,owner:123,chat:123,destination:credentials.destination+'/'+j.id+'/rom.zip',size:1024,completed,expires:completed+43200,file_id:identifier,etag:'a'.repeat(32),link_ctag:'d'.repeat(64)};
 const result={id:j.id,owner:123,state:'done',record,manifest:true,archive:{identifier,name:'rom.zip',url:'https://archive.org/download/'+identifier+'/rom.zip',md5:record.etag,sha256:record.link_ctag}};
 const body={id:j.id,result:{sealed:await seal(credentials.token,result,j.id)}};
 assert.equal((await api('result',body)).status,200);await api('result',body);
 const sends=calls.filter(c=>c.url.endsWith('/sendMessage'));assert.equal(sends.length,2);
 const final=JSON.parse(sends.at(-1).body);assert.equal(final.reply_parameters.message_id,7);assert.equal(final.reply_markup.inline_keyboard[0].length,2);
 const response=await mf.dispatchFetch(final.reply_markup.inline_keyboard[0][0].url,{redirect:'manual'});
 assert.equal(response.status,302);assert.equal(response.headers.get('Location'),result.archive.url);
 assert.equal(calls.filter(c=>c.url.includes('graph.microsoft.com')).length,0);
 assert.ok(calls.filter(c=>c.url.startsWith('https://archive.org')).every(c=>!c.headers.authorization));
});
test('Archive TTL checks the original checksum before deleting a file',async t=>{
 const jid='888888888888',identifier='odrivegay-'+jid+'-'+'c'.repeat(16),completed=Date.now()/1000-44000;
 const job={id:jid,owner:123,chat:123,storage:'archive',state:'done',created:completed,completed,expires:completed+43200,destination:credentials.destination+'/'+jid+'/rom.zip',file_id:identifier,total:1024,etag:'a'.repeat(32),manifest:1,retention:'armed',archive:{identifier,name:'rom.zip',md5:'a'.repeat(32),sha256:'d'.repeat(64),url:'https://archive.org/download/'+identifier+'/rom.zip'}};
 const {calls}=await setup(t,'onedrive',[job],[],{changed:true,credentials:{archive_access:'test-access',archive_secret:'test-secret'}});
 await waitFor(()=>calls.some(c=>c.url.startsWith('https://archive.org/metadata/')));
 assert.equal(calls.filter(c=>c.method==='DELETE').length,0);
});
test('Archive TTL sends delete only to Archive using the retained credentials',async t=>{
 const jid='999999999999',identifier='odrivegay-'+jid+'-'+'c'.repeat(16),completed=Date.now()/1000-44000;
 const job={id:jid,owner:123,chat:123,storage:'archive',state:'done',created:completed,completed,expires:completed+43200,destination:credentials.destination+'/'+jid+'/rom.zip',file_id:identifier,total:1024,etag:'a'.repeat(32),manifest:1,retention:'armed',archive:{identifier,name:'rom.zip',md5:'a'.repeat(32),sha256:'d'.repeat(64),url:'https://archive.org/download/'+identifier+'/rom.zip'}};
 const {calls}=await setup(t,'onedrive',[job],[],{credentials:{archive_access:'test-access',archive_secret:'test-secret'}});
 await waitFor(()=>calls.some(c=>c.method==='DELETE'));
 const deletion=calls.find(c=>c.method==='DELETE');assert.equal(deletion.url,'https://s3.us.archive.org/'+identifier+'/rom.zip');assert.equal(deletion.headers.authorization,'LOW test-access:test-secret');assert.equal(deletion.headers['x-archive-keep-old-version'],'0');
 assert.ok(calls.filter(c=>c.url.startsWith('https://archive.org')).every(c=>!c.headers.authorization));
});
