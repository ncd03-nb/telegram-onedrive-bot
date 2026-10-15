import {Unzip,UnzipInflate} from 'fflate';
const enc=new TextEncoder();
export class BotError extends Error {constructor(message,retry=0){super(message);this.retry=retry;}}
export const now=()=>Date.now()/1000;
export const id=()=>crypto.randomUUID().replaceAll('-','').slice(0,12);
export function credential(env,bot){return JSON.parse(bot==='onedrive'?env.ONEDRIVE_CREDENTIALS:env.PAYLOAD_CREDENTIALS);}
const base64=bytes=>btoa(String.fromCharCode(...bytes));
export function bytes64(value){return Uint8Array.from(atob(value.replaceAll('-','+').replaceAll('_','/')+'='.repeat((4-value.length%4)%4)),c=>c.charCodeAt(0));}
export async function hmac(key,value){const k=await crypto.subtle.importKey('raw',typeof key==='string'?enc.encode(key):key,{name:'HMAC',hash:'SHA-256'},false,['sign']);return new Uint8Array(await crypto.subtle.sign('HMAC',k,enc.encode(value)));}
export async function seal(token,obj,identity){
 const raw=enc.encode(JSON.stringify(obj));if(raw.length>131072)throw new BotError('Dữ liệu tác vụ quá lớn.');
 const key=await crypto.subtle.importKey('raw',await hmac(token,'bot-actions-envelope-v1'),'AES-GCM',false,['encrypt']);
 const iv=crypto.getRandomValues(new Uint8Array(12));
 const cipher=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:enc.encode(identity)},key,raw));
 const all=new Uint8Array(iv.length+cipher.length);all.set(iv);all.set(cipher,12);
 // Avoid apply/spread limits for large scan results.
 let value='';for(let offset=0;offset<all.length;offset+=8192)value+=String.fromCharCode(...all.slice(offset,offset+8192));
 return 'v1.'+btoa(value).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
}
export async function unseal(token,value,identity){
 if(typeof value!=='string'||!value.startsWith('v1.')||value.length>180000)throw new BotError('Kết quả mã hóa không hợp lệ.');
 const raw=bytes64(value.slice(3));const key=await crypto.subtle.importKey('raw',await hmac(token,'bot-actions-envelope-v1'),'AES-GCM',false,['decrypt']);
 const obj=JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:raw.slice(0,12),additionalData:enc.encode(identity)},key,raw.slice(12))));
 if(obj?.id!==identity)throw new BotError('Kết quả không khớp tác vụ.');return obj;
}
export async function limited(response,limit){
 const reader=response.body.getReader();const chunks=[];let length=0;
 while(true){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>limit){await reader.cancel();throw new BotError('Dữ liệu vượt giới hạn.');}chunks.push(value);}
 const raw=new Uint8Array(length);let offset=0;for(const v of chunks){raw.set(v,offset);offset+=v.length;}return raw;
}
export async function telegram(c,method,body){
 const response=await fetch('https://api.telegram.org/bot'+c.token+'/'+method,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(20000)});
 const obj=await response.json();
 if(response.status===429)throw new BotError('Telegram đang giới hạn tần suất.',obj.parameters?.retry_after||5);
 if(!obj.ok){if(method==='editMessageText'&&obj.description?.includes('message is not modified'))return {};throw new BotError('Telegram chưa nhận được tin nhắn.',5);}return obj.result;
}
export const send=(c,m,text,markup)=>telegram(c,'sendMessage',{chat_id:m.chat?.id??m.chat,text:text.slice(0,4000),
 link_preview_options:{is_disabled:true},...(markup?{reply_markup:markup}:{}),
 ...(m.message_id?{reply_parameters:{message_id:m.message_id,allow_sending_without_reply:true}}:{}),
 ...(m.message_thread_id?{message_thread_id:m.message_thread_id}:{})});
export const edit=(c,job,text)=>telegram(c,'editMessageText',{chat_id:job.chat,message_id:job.message_id,text:text.slice(0,4000),link_preview_options:{is_disabled:true}});
export async function github(c,method,path,body){
 const response=await fetch('https://api.github.com/repos/'+c.repo+path,{method,redirect:'manual',headers:{'Authorization':'Bearer '+c.github,'User-Agent':'TelegramCloudController/1.0','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28',...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(25000)});
 if(![200,201,202,204,302].includes(response.status))throw new BotError('GitHub Actions HTTP '+response.status+'.',response.status===429?60:0);
 if(response.status===302)return response.headers.get('Location');
 if(response.status===204)return {};const text=await response.text();return text?JSON.parse(text):{};
}
export async function findRun(c,job){
 const since=new Date(((job.dispatch_at||job.created)-60)*1000).toISOString();
 const obj=await github(c,'GET','/actions/workflows/'+workflow(job)+'/runs?event=workflow_dispatch&created='+encodeURIComponent('>='+since)+'&per_page=100');
 const rows=obj.workflow_runs.filter(r=>r.display_title===prefix(job)+job.id);
 if(rows.length>1)throw new BotError('Có nhiều workflow cùng mã; cần kiểm tra Actions.');return rows[0];
}
export const workflow=job=>job.kind==='scan'?'scan.yml':job.bot==='payload'?'extract.yml':'copy.yml';
export const prefix=job=>job.kind==='scan'?'scan-':job.bot==='payload'?'extract-':'copy-';
export async function artifact(c,job){
 const obj=await github(c,'GET','/actions/runs/'+job.run_id+'/artifacts?per_page=100');
 const entry=obj.artifacts.find(a=>a.name==='result-'+job.id&&!a.expired);
 if(!entry||entry.size_in_bytes>262144)throw new BotError('Chưa có artifact kết quả.',30);
 const location=await github(c,'GET','/actions/artifacts/'+entry.id+'/zip');
 if(!location?.startsWith('https://'))throw new BotError('Artifact không hợp lệ.');
 const response=await fetch(location,{redirect:'error',signal:AbortSignal.timeout(20000)});if(!response.ok)throw new BotError('Chưa tải được artifact.',30);
 const raw=await limited(response,262144);let result,error,length=0;const chunks=[];
 const unzip=new Unzip(file=>{if(file.name!=='result.json'){file.terminate();return;}file.ondata=(err,data,final)=>{
  if(err){error=err;return;}length+=data.length;if(length>180000){file.terminate();error=new Error('oversize');return;}chunks.push(data);
  if(final){const bytes=new Uint8Array(length);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}try{result=JSON.parse(new TextDecoder().decode(bytes));}catch(e){error=e;}}
 };file.start();});unzip.register(UnzipInflate);unzip.push(raw,true);
 if(error||result?.id!==job.id)throw new BotError('Artifact kết quả không hợp lệ.');
 return result.sealed?unseal(c.token,result.sealed,job.id):result;
}
export function source(value){
 let u;try{u=new URL(value.replace('https\\://','https://'));}catch{throw new BotError('Cần link HTTP(S) hợp lệ.');}
 if(!['http:','https:'].includes(u.protocol)||u.username||u.password||(u.port&&!['80','443'].includes(u.port))||value.length>8192||/^(localhost|127\.|10\.|192\.168\.|169\.254\.|0\.|\[)/i.test(u.hostname)||/\.(local|internal|localhost)$/.test(u.hostname))throw new BotError('Chỉ nhận link Internet công khai.');
 return u.href;
}
export const cleanName=name=>name.replace(/[\x00-\x1f\x7f/\\:*?"<>|]/g,'_').replace(/^[ .]+|[ .]+$/g,'').slice(0,120)||'file.bin';

export class Graph {
 constructor(c,storage){this.c=c;this.storage=storage;this.refresh=null;}
 async token(){
  const saved=await this.storage.get('graph_oauth');if(saved?.expires>Date.now()+30000)return saved.access;
  if(this.refresh)return this.refresh;
  this.refresh=(async()=>{const params=new URLSearchParams({client_id:this.c.client_id,client_secret:this.c.client_secret,grant_type:'refresh_token',refresh_token:saved?.refresh||this.c.refresh_token});
   const response=await fetch('https://login.microsoftonline.com/common/oauth2/v2.0/token',{method:'POST',body:params,signal:AbortSignal.timeout(20000)});
   const obj=await response.json();if(!response.ok||!obj.access_token)throw new BotError('OneDrive OAuth cần kết nối lại.',300);
   await this.storage.put('graph_oauth',{access:obj.access_token,refresh:obj.refresh_token||saved?.refresh||this.c.refresh_token,expires:Date.now()+obj.expires_in*1000});return obj.access_token;
  })();try{return await this.refresh;}finally{this.refresh=null;}
 }
 drive(){return '/drives/'+encodeURIComponent(this.c.drive_id);}
 path(destination){const prefix=this.c.destination+'/';if(!destination.startsWith(prefix))throw new BotError('File nằm ngoài thư mục bot.');const path=destination.split(':').slice(1).join(':');if(path.split('/').some(v=>!v||v==='.'||v==='..'))throw new BotError('Đường dẫn không hợp lệ.');return this.drive()+'/root:/'+path.split('/').map(encodeURIComponent).join('/');}
 async request(method,path,body,extra={}){
  const base=this.c.graph_base||'https://graph.microsoft.com/v1.0';const url=new URL(base);if(url.protocol!=='https:'||!(url.hostname==='graph.microsoft.com'||url.hostname.endsWith('.sharepoint.com')))throw new BotError('OneDrive endpoint không hợp lệ.');
  const response=await fetch(base+path,{method,redirect:'manual',headers:{Authorization:'Bearer '+await this.token(),...(body?{'Content-Type':'application/json'}:{}),...extra},...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(20000)});
  if(response.status===404||response.status===412)return {status:response.status};
  if(response.status===401){await this.storage.delete('graph_oauth');throw new BotError('OneDrive yêu cầu làm mới OAuth.',30);}
  if(!response.ok)throw new BotError('OneDrive API HTTP '+response.status+'.',response.status===429?Number(response.headers.get('Retry-After')||60):300);
  return response.status===204?{}:response.json();
 }
 item(destination){return this.request('GET',this.path(destination));}
 manifest(destination,record){return this.request('PUT',this.path(destination.slice(0,destination.lastIndexOf('/'))+'/.bot-retention.json')+':/content',record);}
}
export async function indexURL(c,job,origin){
 if(!job.manifest)return '';const name=job.destination.split('/').at(-1);
 let identity=job.id+'.'+Math.floor(job.expires);if(job.link_ctag){const hash=await crypto.subtle.digest('SHA-256',enc.encode(job.link_ctag));identity+='.'+[...new Uint8Array(hash)].map(v=>v.toString(16).padStart(2,'0')).join('').slice(0,12);}
 const key=await hmac(c.token,'onedrive-download-link-v1');
 const mac=(await hmac(key,c.destination+'\n'+identity+'\n'+name)).slice(0,24);
 return origin+'/0:/'+identity+'.'+base64(mac).replaceAll('+','-').replaceAll('/','_').replaceAll('=','')+'/'+encodeURIComponent(name);
}
