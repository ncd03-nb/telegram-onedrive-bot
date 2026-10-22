import {DurableObject} from 'cloudflare:workers';
import {grant,freshState,LEASE_MS,authorized} from './lease.mjs';
import {Controller} from './controller.mjs';
import {BotError} from './services.mjs';
import {publicPage} from './public-pages.mjs';

const reply=(obj,status=200)=>Response.json(obj,{status,headers:{'Cache-Control':'no-store'}});
function bots(env){return JSON.parse(env.BOTS_CONFIG);}
function accepts(update,config){
  const q=update?.callback_query;
  if(q){
    const from=q.from,chat=q.message?.chat;
    if(!Number.isSafeInteger(update.update_id)||!Number.isSafeInteger(from?.id)||from.id<=0||from.is_bot||typeof q.id!=='string'||!/^page:[a-f0-9]{12}:[0-9]{1,4}$/.test(q.data||'')||!Number.isSafeInteger(q.message?.message_id))return false;
    if(!config.public&&!config.users.includes(from.id))return false;
    return chat?.type==='private'?chat.id===from.id:config.groups&&['group','supergroup'].includes(chat?.type)&&chat.id<0;
  }
  const m=update?.message,from=m?.from,chat=m?.chat;
  if(!Number.isSafeInteger(update?.update_id)||!m||!Number.isSafeInteger(from?.id)||from.id<=0||from.is_bot||typeof m.text!=='string')return false;
  if(!config.public && !config.users.includes(from.id))return false;
  if(chat?.type==='private'){if(chat.id!==from.id)return false;}
  else if(!config.groups||!['group','supergroup'].includes(chat?.type)||chat.id>=0)return false;
  if(config.slash_only&&!m.text.trimStart().startsWith('/'))return false;
  const command=m.text.trim().split(/\s+/,1)[0];
  if(command?.startsWith('/')&&command.includes('@')&&command.split('@')[1].toLowerCase()!==config.username)return false;
  return true;
}

export default {
  async fetch(request,env){
    const url=new URL(request.url);
    if(request.method==='GET'){const page=publicPage(url.pathname);if(page)return page;}
    if(request.method==='GET'&&url.pathname==='/health'){
      const states=await Promise.all(['onedrive','payload'].map(async bot=>{const response=await env.BOT_STATE.get(env.BOT_STATE.idFromName(bot)).fetch(new Request('https://state/status',{method:'POST',headers:{'Content-Type':'application/json','X-Bot':bot},body:'{}'}));const obj=await response.json();return [bot,obj.leader||'waiting'];}));
      return reply({ok:true,service:'telegram-cloud-controller',controllers:Object.fromEntries(states)});
    }
    const download=/^\/0:\/([^/]+)\/([^/]+)$/.exec(url.pathname);
    if(download&&request.method==='GET')return env.BOT_STATE.get(env.BOT_STATE.idFromName('onedrive')).fetch(new Request('https://state/index',{method:'POST',redirect:'manual',headers:{'Content-Type':'application/json','X-Bot':'onedrive'},body:JSON.stringify({capability:download[1],name:decodeURIComponent(download[2])})}));
    const match=/^\/(telegram|api)\/(onedrive|payload)(?:\/(tick|save|release|result|status|prepare|activate|audit))?$/.exec(url.pathname);
    if(!match)return reply({error:'not found'},404);
    const [,route,bot,action]=match,config=bots(env)[bot];
    if(!config)return reply({error:'not found'},404);
    if(route==='telegram'){
      if(request.method!=='POST'||request.headers.get('X-Telegram-Bot-Api-Secret-Token')!==config.webhook_key)return reply({error:'unauthorized'},403);
    }else if(!authorized(request.headers.get('Authorization'),config.host_key))return reply({error:'unauthorized'},403);
    if(Number(request.headers.get('Content-Length')||0)>1000000)return reply({error:'too large'},413);
    let body={};
    if(request.method!=='GET'){
      const reader=request.body?.getReader();if(!reader)return reply({error:'invalid body'},400);
      const chunks=[];let size=0;
      while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>1000000){await reader.cancel();return reply({error:'too large'},413);}chunks.push(value);}
      const raw=new Uint8Array(size);let offset=0;for(const chunk of chunks){raw.set(chunk,offset);offset+=chunk.length;}
      try{body=JSON.parse(new TextDecoder().decode(raw));}catch{return reply({error:'invalid JSON'},400);}
    }
    if(!body)return reply({error:'invalid JSON'},400);
    if(route==='telegram'&&!accepts(body,config))return new Response(null,{status:204});
    const stub=env.BOT_STATE.get(env.BOT_STATE.idFromName(bot),{locationHint:'apac'});
    return stub.fetch(new Request('https://state/'+(route==='telegram'?'telegram':action),{
      method:'POST',headers:{'Content-Type':'application/json','X-Bot':bot},body:JSON.stringify(body)}));
  }
};

export class BotState extends DurableObject {
  constructor(ctx,env){
    super(ctx,env);this.ctx=ctx;this.env=env;this.sql=ctx.storage.sql;
    this.sql.exec('CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY,json TEXT NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY,payload TEXT,done INTEGER DEFAULT 0,created INTEGER)');
    this.sql.exec('INSERT OR IGNORE INTO meta VALUES (1,?)',JSON.stringify(freshState()));
    this.controller=new Controller(this);
  }
  state(){return JSON.parse(this.sql.exec('SELECT json FROM meta WHERE id=1').one().json);}
  persist(state){this.sql.exec('UPDATE meta SET json=? WHERE id=1',JSON.stringify(state));}
  pending(){return [...this.sql.exec('SELECT id,payload FROM events WHERE done=0 ORDER BY created LIMIT 10')];}
  async schedule(){
    if(this.controller.mode()==='cloudflare')return this.controller.schedule();
    const s=this.state(),now=Date.now(),pending=this.pending().length;
    let next=s.cleanupAt?Math.max(now+1000,s.cleanupAt):null;
    if(pending){const check=s.leader?.until>now?s.leader.until+1000:now+1000;next=Math.min(next??Infinity,check);}
    if(s.cleanupAt&&s.cleanupAt<=now)next=Math.max(now+1000,s.lastCleanup+300000);
    const current=await this.ctx.storage.getAlarm();
    if(next!==null && (!current||Math.abs(current-next)>1000))await this.ctx.storage.setAlarm(next);
    else if(next===null&&current)await this.ctx.storage.deleteAlarm();
  }
  async fetch(request){
    const action=new URL(request.url).pathname.slice(1),body=await request.json(),now=Date.now();
    let s=this.state();s.bot=request.headers.get('X-Bot')||s.bot;
    if(!this.state().bot)this.persist(s);
    if(action==='prepare'||action==='activate'){
      try{return reply(await this.controller[action]());}catch(error){return reply({error:error instanceof BotError?error.message:'Controller initialization failed'},409);}
    }
    if(action==='audit')return reply({controller:this.controller.summary(),jobs:this.controller.jobs().map(j=>({id:j.id,state:j.state,run_id:j.run_id,dispatch_at:j.dispatch_at,expires:j.expires,file_id:j.file_id,etag:j.etag})),outputs:this.controller.jobs().filter(j=>j.files?.length).length});
    if(action==='index')return this.controller.mode()==='cloudflare'?this.controller.index(body.capability,body.name):new Response('Chưa chuyển Index Link.',{status:503});
    if(this.controller.mode()==='cloudflare'){
      if(action==='status')return reply({leader:'cloudflare',epoch:s.epoch,initialized:true,pending:this.pending().length,...this.controller.summary()});
      if(['tick','save','release'].includes(action))return reply({granted:false,leader:'cloudflare',retired:true});
      if(action==='result'&&body.result){try{await this.controller.result(body.id,body.result,body.run_id);await this.controller.schedule();return reply({accepted:true});}catch(error){return reply({error:error instanceof BotError?error.message:'Invalid callback'},400);}}
    }
    if(action==='telegram'){
      if(this.sql.exec('SELECT COUNT(*) AS n FROM events WHERE done=0').one().n>=1000)return reply({error:'queue full'},503);
      this.sql.exec('INSERT OR IGNORE INTO events(id,payload,created) VALUES(?,?,?)','u:'+body.update_id,JSON.stringify(body),now);
      this.persist(s);await this.schedule();
      if(this.controller.mode()==='cloudflare'){this.ctx.waitUntil(this.controller.process());return new Response(null,{status:204});}
      if(!s.leader||s.leader.until<=now)this.ctx.waitUntil(this.wake());
      return new Response(null,{status:204});
    }
    if(action==='result'){
      if(!/^[a-f0-9]{12}$/.test(body.id))return reply({error:'invalid job'},400);
      // Rearm the event: the workflow announces again after artifact upload.
      this.sql.exec('INSERT INTO events(id,payload,created) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET done=0,payload=excluded.payload,created=excluded.created','r:'+body.id,'{}',now);
      this.persist(s);await this.schedule();
      if(this.controller.mode()==='cloudflare'){this.ctx.waitUntil(this.controller.process());return reply({accepted:true});}
      if(!s.leader||s.leader.until<=now)this.ctx.waitUntil(this.wake());
      return reply({accepted:true});
    }
    if(action==='status')return reply({leader:s.leader?.until>now?s.leader.role:null,epoch:s.epoch,initialized:!!s.snapshot,pending:this.pending().length,primary_age_seconds:s.primaryAt?Math.floor((now-s.primaryAt)/1000):null});
    if(!['tick','save','release'].includes(action)||!['docker','render'].includes(body.role)||!/^[a-f0-9]{32}$/.test(body.instance))return reply({error:'invalid request'},400);
    if(s.controller==='staged'&&(body.epoch===null||body.epoch===undefined))return reply({granted:false,leader:'migration'});
    if(body.snapshot){
      if(typeof body.snapshot!=='string'||body.snapshot.length>800000||!/^[a-f0-9]{64}$/.test(body.digest))return reply({error:'invalid snapshot'},400);
      let bytes;try{bytes=Uint8Array.from(atob(body.snapshot),c=>c.charCodeAt(0));}catch{return reply({error:'invalid snapshot'},400);}
      const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');
      if(hash!==body.digest)return reply({error:'snapshot checksum'},400);
      s=this.state();s.bot=request.headers.get('X-Bot')||s.bot;
    }
    if(['save','release'].includes(action)&&(body.epoch===null||body.epoch===undefined))return reply({granted:false});
    if(!s.snapshot&&!body.snapshot)return reply({granted:false});
    if(!grant(s,body,now)){this.persist(s);return reply({granted:false,leader:s.leader?.role??null});}
    if(body.snapshot && (body.epoch!==null&&body.epoch!==undefined || !s.snapshot)){
      s.snapshot=body.snapshot;s.digest=body.digest;
      const due=body.summary?.cleanup_at;
      s.cleanupAt=Number.isSafeInteger(due)&&due>0?due:null;
    }
    for(const id of (body.acked_updates||[]).slice(0,10)){
      if(Number.isSafeInteger(id))this.sql.exec("UPDATE events SET done=1,payload='' WHERE id=?",'u:'+id);
    }
    for(const id of (body.acked_events||[]).slice(0,10)){
      if(typeof id==='string'&&/^[rm]:[a-z0-9:]+$/.test(id))this.sql.exec("UPDATE events SET done=1,payload='' WHERE id=?",id);
    }
    const pending=this.pending();
    this.sql.exec('DELETE FROM events WHERE done=1 AND created<?',now-7*86400000);
    const result={granted:true,epoch:s.epoch,yield_to_docker:body.role==='render'&&s.primaryAt>now-LEASE_MS,
      updates:pending.filter(e=>e.id.startsWith('u:')).map(e=>JSON.parse(e.payload)),events:pending.filter(e=>!e.id.startsWith('u:')).map(e=>e.id)};
    if(body.epoch===null||body.epoch===undefined)result.snapshot=s.snapshot;
    if(action==='release'){s.leader=null;if(body.role==='docker')s.primaryAt=0;}
    this.persist(s);await this.schedule();return reply(result);
  }
  async wake(){
    const s=this.state();if(!s.bot||!this.pending().length)return;
    const config=bots(this.env)[s.bot];if(!config?.fallback_url)return;
    try{await fetch(config.fallback_url+'/wake',{method:'POST',headers:{Authorization:'Bearer '+config.host_key},signal:AbortSignal.timeout(5000)});}catch{}
  }
  async alarm(){
    if(this.controller.mode()==='cloudflare'){await this.controller.process();return;}
    const s=this.state(),now=Date.now();
    if(s.cleanupAt&&s.cleanupAt<=now&&s.lastCleanup+300000<=now){
      this.sql.exec('INSERT OR IGNORE INTO events(id,payload,created) VALUES(?,?,?)','m:'+Math.floor(now/300000),'{}',now);
      s.lastCleanup=now;this.persist(s);
    }
    if(!s.leader||s.leader.until<=now){await this.wake();}
    // Actual queued work only; never ping Render just to prevent idle sleep.
    if(this.pending().length)await this.ctx.storage.setAlarm(Date.now()+60000);
    else await this.schedule();
  }
}
