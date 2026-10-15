import {credential,BotError,now,id,seal,unseal,bytes64,limited,telegram,send,edit,github,findRun,workflow,artifact,source,cleanName,Graph,indexURL} from './services.mjs';
const odHelp='/copy <link> [tên file]\n/status [mã]\n/cancel <mã>\n/find <từ khóa>\n/link <mã> — Index Link và tải trực tiếp\n/dl <mã>\n/check — quota OneDrive\nFile bot upload tự xóa sau 12 giờ.';
const payloadHelp='/list <link ZIP OTA hoặc payload.bin>\n/extract <link hoặc mã ROM> boot,init_boot,vbmeta\n/extract <mã ROM> all\n/status [mã]\n/cancel <mã>\n/files <mã>\nNhiều phân vùng được gộp thành một ZIP. Chỉ nhận lệnh / trong chat riêng hoặc group, không cần tag bot.';
const terminal=new Set(['done','failed','cancelled','deleted','interrupted']);
export class Controller {
 constructor(parent){this.p=parent;this.sql=parent.sql;this.ctx=parent.ctx;this.env=parent.env;this.processing=null;
  this.sql.exec('CREATE TABLE IF NOT EXISTS cloud_jobs(id TEXT PRIMARY KEY,update_id INTEGER UNIQUE,owner INTEGER,state TEXT,created REAL,json TEXT)');
  this.sql.exec('CREATE TABLE IF NOT EXISTS cloud_scans(id TEXT PRIMARY KEY,owner INTEGER,created REAL,json TEXT)');
 }
 mode(){return this.p.state().controller||'legacy';}
 bot(){return this.p.state().bot;}
 config(){return credential(this.env,this.bot());}
 origin(){return 'https://telegram-bot-failover.der-templates.workers.dev';}
 get(jid,owner){const row=[...this.sql.exec('SELECT json FROM cloud_jobs WHERE id=?',jid)][0];if(!row)return;const j=JSON.parse(row.json);return owner===undefined||j.owner===owner?j:undefined;}
 jobs(query='',values=[]){return [...this.sql.exec('SELECT json FROM cloud_jobs '+query,...values)].map(r=>JSON.parse(r.json));}
 save(j){this.sql.exec('INSERT INTO cloud_jobs VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,state=excluded.state,created=excluded.created,json=excluded.json',j.id,j.update_id??null,j.owner,j.state,j.created||now(),JSON.stringify(j));}
 summary(){return {mode:this.mode(),jobs:[...this.sql.exec('SELECT state,COUNT(*) AS count FROM cloud_jobs GROUP BY state')],scans:this.sql.exec('SELECT COUNT(*) AS count FROM cloud_scans').one().count,pending:this.p.pending().length};}
 async prepare(){const s=this.p.state();if(!s.snapshot)throw new BotError('Chưa có dữ liệu để chuyển.');if(s.controller==='cloudflare')return this.summary();s.controller='staged';this.p.persist(s);return this.summary();}
 async activate(){
  const s=this.p.state();if(s.controller==='cloudflare')return this.summary();
  if(s.controller!=='staged'||s.leader?.until>Date.now())throw new BotError('Cần dừng controller cũ trước khi chuyển.');
  const stream=new Blob([bytes64(s.snapshot)]).stream().pipeThrough(new DecompressionStream('gzip'));
  const raw=new TextDecoder().decode(await limited(new Response(stream),16*1024**2));
  const tables=JSON.parse(raw).tables;const rows=table=>(table?.rows||[]).map(row=>Object.fromEntries(table.columns.map((c,i)=>[c,row[i]])));
  this.ctx.storage.transactionSync(()=>{
   for(const j of rows(tables.jobs)){j.bot=s.bot;j.kind=j.bot==='payload'?'extract':'copy';if(j.bot==='payload')j.partitions=j.requested_name;
    const out=rows(tables.outputs).find(v=>v.id===j.id&&v.owner===j.owner);if(out)j.files=JSON.parse(out.files);this.save(j);}
   for(const scan of rows(tables.scans))this.sql.exec('INSERT OR REPLACE INTO cloud_scans VALUES(?,?,?,?)',scan.id,scan.owner,scan.created,JSON.stringify({...scan,metadata:JSON.parse(scan.metadata)}));
   for(const update of rows(tables.updates))if(update.state==='done')this.sql.exec("UPDATE events SET done=1,payload='' WHERE id=?",'u:'+update.id);
   s.controller='cloudflare';s.leader=null;s.epoch++;this.p.persist(s);
  });
  await this.schedule();this.ctx.waitUntil(this.process());return this.summary();
 }
 async schedule(delay=0){
  if(this.mode()!=='cloudflare')return;
  const jobs=this.jobs();const times=[];
  if(this.p.pending().length)times.push(Date.now()+Math.max(1000,delay*1000));
  for(const j of jobs){
   if(j.notification_needed)times.push(Math.max(Date.now()+1000,(j.notify_retry||now()+delay)*1000));
   if(['queued','running'].includes(j.state))times.push(Math.max(Date.now()+1000,(j.next_check||now()+30)*1000));
   if(j.state==='done'&&this.bot()==='onedrive'&&['pending','armed'].includes(j.retention))times.push(Math.max(Date.now()+1000,(j.cleanup_retry||j.expires)*1000));
  }
  const next=times.length?Math.min(...times):null;const current=await this.ctx.storage.getAlarm();
  if(next!==null&&(!current||next<current||current<=Date.now()))await this.ctx.storage.setAlarm(next);
  else if(next===null&&current)await this.ctx.storage.deleteAlarm();
 }
 process(){if(this.processing)return this.processing;this.processing=this._process().finally(()=>{this.processing=null;});return this.processing;}
 async _process(){
  if(this.mode()!=='cloudflare')return;let retry=0;
  if(this.p.state().command_retry>now()){await this.schedule(this.p.state().command_retry-now());return;}
  for(const event of this.p.pending().slice(0,5)){
   try{
    if(event.id.startsWith('u:')){const update=JSON.parse(event.payload);if(update.callback_query)await this.page(update.callback_query);else await this.command(update);}
    else if(event.id.startsWith('r:')){const j=this.get(event.id.slice(2));if(j&&!terminal.has(j.state)){j.next_check=now();this.save(j);}}
    this.sql.exec("UPDATE events SET done=1,payload='' WHERE id=?",event.id);
   }catch(error){
    if(error instanceof BotError&&!error.retry){if(event.id.startsWith('u:')){try{await send(this.config(),JSON.parse(event.payload).message,error.message);}catch{retry=5;break;}}this.sql.exec("UPDATE events SET done=1,payload='' WHERE id=?",event.id);}
    else{retry=error.retry||15;break;}
   }
  }
  for(const j of this.jobs("WHERE state IN ('queued','running') AND COALESCE(json_extract(json,'$.next_check'),0)<=? ORDER BY created LIMIT 10",[now()])){
   if(j.next_check&&j.next_check>now())continue;
   try{await this.advance(j);}catch(error){j.next_check=now()+(error.retry||60);j.last_error=error instanceof BotError?error.message:'Đang chờ kết nối lại Actions.';this.save(j);}
  }
  for(const j of this.jobs("WHERE state IN ('done','failed') ORDER BY created LIMIT 2000"))if(j.notification_needed&&!(j.notify_retry>now())){try{await this.finishNotify(j);}catch(error){retry=error.retry||15;j.notify_retry=now()+retry;this.save(j);}}
  if(this.bot()==='onedrive')for(const j of this.jobs("WHERE state='done' ORDER BY created LIMIT 2000")){
   if(!['pending','armed'].includes(j.retention)||j.cleanup_retry>now()||j.expires>now()&&j.retention!=='pending')continue;
   try{await this.cleanup(j);}catch(error){j.cleanup_retry=now()+(error.retry||300);j.cleanup_error='OneDrive chưa kết nối; lịch xóa sẽ được thử lại.';this.save(j);}
  }
  this.sql.exec('DELETE FROM cloud_scans WHERE created<?',now()-86400);
  this.sql.exec('DELETE FROM events WHERE done=1 AND created<?',Date.now()-7*86400000);
  if(retry){const s=this.p.state();s.command_retry=now()+retry;this.p.persist(s);}
  await this.schedule(retry);
 }
 message(j){return {chat:{id:j.chat},message_id:j.request_message_id,message_thread_id:j.thread_id};}
 describe(j){const label={queued:'Đang chờ runner',running:'Đang xử lý',done:'Hoàn thành',failed:'Thất bại',cancelled:'Đã hủy',deleted:'Đã xóa',interrupted:'Bị ngắt'}[j.state]||j.state;return j.id+' — '+label+(j.destination?'\n📄 '+j.destination.split('/').at(-1):'')+(j.phase?'\n'+j.phase:'')+(j.expires?'\nHạn lưu: '+new Date(j.expires*1000).toLocaleString('vi-VN',{timeZone:'Asia/Bangkok'}):'')+(j.error?'\n'+j.error:'');}
 async command(update){
  const m=update.message,owner=m.from.id,c=this.config(),text=m.text.trim().replace('https\\://','https://');
  const space=text.search(/\s/),head=(space<0?text:text.slice(0,space)).split('@')[0].toLowerCase(),args=space<0?'':text.slice(space).trim();
  const help=this.bot()==='onedrive'?odHelp:payloadHelp;
  if(['/start','/help'].includes(head)){await send(c,m,help);return;}
  if(head==='/whoami'){await send(c,m,'Telegram user ID: '+owner);return;}
  if(head==='/status'){
   const jobs=args?[this.get(args,owner)].filter(Boolean):this.jobs('WHERE owner=? ORDER BY created DESC LIMIT 5',[owner]);await send(c,m,jobs.map(j=>this.describe(j)).join('\n\n')||'Chưa có tác vụ.');return;
  }
  if(['/find','/search'].includes(head)&&this.bot()==='onedrive'){
   if(!args)throw new BotError('Dùng /find <từ khóa>.');const rows=this.jobs("WHERE owner=? AND state='done' ORDER BY created DESC",[owner]).filter(j=>j.expires>now()&&j.destination?.toLowerCase().includes(args.toLowerCase())).slice(0,10);
   await send(c,m,rows.map(j=>this.describe(j)+'\n/link '+j.id).join('\n\n')||'Không có file còn hạn.');return;
  }
  if(['/files','/link','/dl','/download'].includes(head)){
   const j=this.get(args,owner);if(!j||j.state!=='done')throw new BotError('Chưa có kết quả; dùng /status để lấy mã.');
   if(this.bot()==='onedrive'){await this.links(j,m,['/dl','/download'].includes(head));return;}
   if(!j.files?.length)throw new BotError('Chưa có file kết quả.');for(const file of j.files)await this.file(file,m);return;
  }
  if(head==='/cancel'){
   const j=this.get(args,owner);if(!j||terminal.has(j.state))throw new BotError('Không có tác vụ đang chạy thuộc bạn.');
   if(j.dispatch_at){const run=j.run_id?{id:j.run_id}:await findRun(c,j);if(!run)throw new BotError('GitHub chưa cấp runner; thử hủy lại sau vài giây.');await github(c,'POST','/actions/runs/'+run.id+'/cancel');}
   j.state='cancelled';j.url='';this.save(j);await send(c,m,'Đã yêu cầu hủy '+j.id+'.');return;
  }
  if(head==='/check'&&this.bot()==='onedrive'){
   const drive=new Graph(c,this.ctx.storage);const obj=await drive.request('GET',drive.drive());await send(c,m,'OneDrive kết nối được.\nCòn trống: '+((obj.quota?.remaining||0)/1024**3).toFixed(2)+' GiB');return;
  }
  let kind=this.bot()==='onedrive'?'copy':'extract',url='',requested='';
  if(this.bot()==='payload'){
   if(['/list','/partitions'].includes(head)){kind='scan';url=source(args);}
   else if(head==='/extract'){
    const split=args.search(/\s/);if(split<0)throw new BotError('Dùng /extract <link hoặc mã ROM> boot,init_boot,vbmeta');
    const input=args.slice(0,split);requested=args.slice(split).trim();
    if(input.startsWith('http'))url=source(input);
    else{const row=[...this.sql.exec('SELECT json FROM cloud_scans WHERE id=? AND owner=? AND created>?',input,owner,now()-86400)][0];if(!row)throw new BotError('Mã ROM không có hoặc đã hết hạn; gửi lại link.');url=JSON.parse(row.json).url;}
    if(requested!=='all'&&!/^[A-Za-z0-9_,\s-]{1,1500}$/.test(requested))throw new BotError('Tên phân vùng không hợp lệ.');
   }else{if(m.chat.type==='private')await send(c,m,help);return;}
  }else{
   const input=head==='/copy'?args:text;const split=input.search(/\s/);url=source(split<0?input:input.slice(0,split));requested=split<0?'':cleanName(input.slice(split).trim());
   const parsed=new URL(url);if(parsed.hostname==='drive.google.com'&&parsed.pathname.includes('/folders/'))throw new BotError('Chỉ nhận link file Drive, chưa nhận folder.');
  }
  let j=this.jobs('WHERE update_id=?',[update.update_id])[0];
  if(!j){
   const count=this.sql.exec("SELECT COUNT(*) AS count FROM cloud_jobs WHERE owner=? AND state IN ('queued','running')",owner).one().count;if(count>=3)throw new BotError('Mỗi người tối đa 3 tác vụ đang chờ/chạy.');
   j={id:id(),bot:this.bot(),kind,update_id:update.update_id,owner,chat:m.chat.id,url,requested_name:requested,partitions:requested,state:'queued',created:now(),thread_id:m.message_thread_id||0,request_message_id:m.message_id||0};this.save(j);
  }
  if(!j.message_id){const sent=await send(c,m,(kind==='scan'?'🔎 Đang đọc danh sách phân vùng…':'⏳ '+j.id+' — Đang chờ GitHub Actions\nTiến độ: 0%'));j.message_id=sent.message_id;this.save(j);}
 }
 async advance(j){
  const c=this.config();
  if(!j.dispatch_at){
   j.state='running';j.phase='dispatching';j.dispatch_at=now();j.next_check=now()+30;this.save(j);
   // Durable intent before the POST. An ambiguous response is reconciled,
   // never repeated, to protect against duplicate uploads.
   const input={job_id:j.id,job_payload:await seal(c.token,j,j.id)};
   if(j.bot==='onedrive')Object.assign(input,{max_file_gib:String(c.max_file_gib||12),retention_hours:'12'});
   try{const response=await github(c,'POST','/actions/workflows/'+workflow(j)+'/dispatches',{ref:'main',inputs:input});j=this.get(j.id);if(terminal.has(j.state))return;if(response.workflow_run_id)j.run_id=response.workflow_run_id;j.phase='queued';j.url='';this.save(j);}catch(error){j=this.get(j.id);if(!terminal.has(j.state)){j.last_error='Đang xác minh workflow đã gửi; không dispatch lại.';this.save(j);}}
   return;
  }
  const run=j.run_id?await github(c,'GET','/actions/runs/'+j.run_id):await findRun(c,j);
  j=this.get(j.id);if(terminal.has(j.state))return;
  if(!run){if(now()-j.dispatch_at>600){j.state='failed';j.error='Chưa tìm thấy workflow sau 10 phút. Kiểm tra Actions trước khi gửi lại.';this.save(j);await edit(c,j,this.describe(j));}else{j.next_check=now()+30;this.save(j);}return;}
  j.run_id=run.id;j.phase=run.status;j.next_check=now()+60;this.save(j);
  if(run.status==='completed'){
   try{await this.result(j.id,await artifact(c,j));}catch(error){
    if(run.conclusion==='success')throw error;
    await this.result(j.id,{id:j.id,owner:j.owner,state:run.conclusion==='cancelled'?'cancelled':'failed',error:'Workflow '+run.conclusion+'; kiểm tra Actions.'});
   }
  }else if(j.phase==='queued'&&!j.queue_notified){await edit(c,j,'⏳ '+j.id+' — GitHub đang cấp runner\nTiến độ: 0%');j.queue_notified=true;this.save(j);}
 }
 async result(jid,data,runID){
  let j=this.get(jid);if(!j)throw new BotError('Tác vụ không tồn tại.');
  const c=this.config();const result=data?.sealed?await unseal(c.token,data.sealed,jid):data;
  j=this.get(jid);
  if(result?.id!==jid||!['done','failed','cancelled'].includes(result.state)||result.owner!==undefined&&result.owner!==j.owner)throw new BotError('Kết quả không khớp tác vụ.');
  if(Number.isSafeInteger(runID)&&runID>0){j.run_id=runID;this.save(j);}
  if(terminal.has(j.state)&&!(j.state==='cancelled'&&j.bot==='onedrive'&&result.state==='done'))return {accepted:true};
  if(result.state==='done'){
   if(j.kind==='scan'){
    if(!Array.isArray(result.metadata)||result.metadata.length>1000||typeof result.url!=='string')throw new BotError('Danh sách ROM không hợp lệ.');
    this.sql.exec('INSERT OR REPLACE INTO cloud_scans VALUES(?,?,?,?)',jid,j.owner,now(),JSON.stringify({id:jid,owner:j.owner,url:result.url,metadata:result.metadata,rom_version:result.rom_version}));
    j.scan_notification={metadata:result.metadata,rom_version:result.rom_version};
   }else if(j.bot==='payload'){
    if(!Array.isArray(result.files)||result.files.length>1)throw new BotError('Kết quả file không hợp lệ.');j.files=result.files;
   }else{
    const record=result.record;this.validateRecord(record,j);
    Object.assign(j,{destination:record.destination,total:record.size,bytes:record.size,completed:record.completed,expires:record.expires,file_id:record.file_id,etag:record.etag,link_ctag:record.link_ctag||'',retention:record.etag?'armed':'pending',manifest:Number(Boolean(result.manifest))});
   }
  }
  j.state=result.state;j.phase='completed';j.url='';j.completed=j.completed||now();j.error=result.error||'';
  j.notification_needed=true;this.save(j);
  if(j.notification_needed){try{await this.finishNotify(j);}catch(error){j.notify_retry=now()+(error.retry||15);this.save(j);}}return {accepted:true};
 }
 validateRecord(r,j){
  const prefix=this.config().destination+'/'+j.id+'/';const name=r?.destination?.slice(prefix.length);
  if(r?.version!==1||r.id!==j.id||r.owner!==j.owner||r.chat!==j.chat||!r.destination?.startsWith(prefix)||!name||/[\/\\]/.test(name)||name.startsWith('.')||!Number.isSafeInteger(r.size)||r.size<0||!r.file_id||!Number.isFinite(r.completed)||!Number.isFinite(r.expires)||r.expires<=r.completed||r.expires-r.completed>13*3600)throw new BotError('Danh tính file không khớp tác vụ.');
 }
 async finishNotify(j){
  if(j.scan_notification){const scan=j.scan_notification;
   await this.present(j,'📦 ROM '+j.id+' — '+scan.metadata.length+' phân vùng\n📱 '+(scan.rom_version||'Không xác định')+'\n/extract '+j.id+' boot,init_boot,vbmeta\nMã ROM có hiệu lực 24 giờ.',scan.metadata.map(p=>p.name+' — '+(p.size/1024**2).toFixed(1)+' MiB'+(p.supported?'':' [delta]')));delete j.scan_notification;
  }else if(j.state!=='done')await edit(this.config(),j,'❌ '+this.describe(j));
  else if(j.bot==='payload')for(const file of j.files||[])await this.file(file,this.message(j),j);
  else await this.links(j,this.message(j),false,true);j.notification_needed=false;this.save(j);
 }
 async present(j,heading,lines=[],markup){
  const pages=[];let text=heading.slice(0,1000)+'\n';
  for(const line of lines){const row=line.slice(0,2000);if(text.length+row.length+1>3800){pages.push(text.trimEnd());text=heading.slice(0,1000)+'\n';}text+='\n'+row;}
  pages.push(text.trimEnd());j.response_pages=pages;j.response_markup=markup||{inline_keyboard:[]};this.save(j);await this.renderPage(j,0);
 }
 async renderPage(j,page){
  const pages=j.response_pages,buttons=[...(j.response_markup?.inline_keyboard||[])];
  if(pages.length>1){const nav=[];if(page>0)nav.push({text:'‹ Trước',callback_data:'page:'+j.id+':'+(page-1)});if(page+1<pages.length)nav.push({text:'Sau ›',callback_data:'page:'+j.id+':'+(page+1)});buttons.push(nav);}
  await edit(this.config(),j,pages[page]+(pages.length>1?'\n\nTrang '+(page+1)+'/'+pages.length:''),{inline_keyboard:buttons});
 }
 async page(q){
  const [,jid,value]=q.data.split(':'),j=this.get(jid,q.from.id),page=Number(value);
  if(!j||j.chat!==q.message.chat.id||j.message_id!==q.message.message_id||!j.response_pages||page>=j.response_pages.length||j.kind==='scan'&&j.completed<now()-86400){await telegram(this.config(),'answerCallbackQuery',{callback_query_id:q.id,text:'Danh sách đã hết hạn hoặc không thuộc yêu cầu của bạn.'});return;}
  await this.renderPage(j,page);await telegram(this.config(),'answerCallbackQuery',{callback_query_id:q.id});
 }
 async file(file,m,target){
  const url=new URL(file.url);if(url.protocol!=='https:')throw new BotError('Link Drive không hợp lệ.');
  const zip=file.name.endsWith('.zip');const detail=file.members?'\n📦 '+file.members.length+' phân vùng\n'+file.members.map(v=>v.name).join(', '):'\n📦 '+file.name;
  const heading='✅ Hoàn thành\n📱 ROM: '+(file.rom_version||'Không xác định')+'\n📄 '+file.name+'\n💾 '+(file.size/1024**2).toFixed(2)+' MiB',markup={inline_keyboard:[[{text:zip?'⬇ Tải ZIP từ Drive':'⬇ Tải .img từ Drive',url:file.url}]]};
  if(target)await this.present(target,heading,file.members?['📦 '+file.members.length+' phân vùng',...file.members.map(v=>v.name)]:['📦 '+file.name],markup);
  else await send(this.config(),m,heading+detail,markup);
 }
 async arm(j,item){
  if(item.status===404||!item.file||item.size!==j.total||j.file_id&&item.id!==j.file_id||j.etag&&item.eTag!==j.etag)throw new BotError('File đã thay đổi; không đặt lại lịch xóa.');
  Object.assign(j,{file_id:item.id,etag:j.etag||item.eTag,link_ctag:j.link_ctag||item.cTag||'',retention:'armed'});this.save(j);
  const record={version:1,id:j.id,owner:j.owner,chat:j.chat,destination:j.destination,size:j.total,completed:j.completed,expires:j.expires,file_id:j.file_id,etag:j.etag,...(j.link_ctag?{link_ctag:j.link_ctag}:{})};
  await new Graph(this.config(),this.ctx.storage).manifest(j.destination,record);j.manifest=1;this.save(j);
 }
 async links(j,m,directOnly=false,inPlace=false){
  if(!j.expires||j.expires<=now())throw new BotError('File đã hết hạn 12 giờ.');const graph=new Graph(this.config(),this.ctx.storage);const item=await graph.item(j.destination);
  if(item.status===404||!item.file||item.id!==j.file_id||item.size!==j.total||j.link_ctag&&item.cTag!==j.link_ctag)throw new BotError('File đã bị xóa, thay thế hoặc sửa nội dung.');
  if(!j.manifest||!j.link_ctag){if(!item.cTag&&item.eTag!==j.etag)throw new BotError('Chưa xác minh được nội dung file.');j.link_ctag=item.cTag||'';await this.arm(j,{...item,eTag:j.etag||item.eTag});}
  const direct=item['@microsoft.graph.downloadUrl'];if(!direct?.startsWith('https://'))throw new BotError('OneDrive chưa trả link tải.',30);
  const buttons=[];if(!directOnly)buttons.push({text:'⚡ Index Link',url:await indexURL(this.config(),j,this.origin())});buttons.push({text:'⬇ Tải trực tiếp',url:direct});
  const text='✅ Hoàn thành\n📄 '+item.name,markup={inline_keyboard:[buttons]};
  if(inPlace)await edit(this.config(),j,text,markup);else await send(this.config(),m,text,markup);
 }
 async cleanup(j){
  const graph=new Graph(this.config(),this.ctx.storage);const item=await graph.item(j.destination);
  if(item.status===404){j.state='deleted';j.retention='missing';this.save(j);return;}
  if(j.retention==='pending'){await this.arm(j,item);if(j.expires>now())return;}
  if(item.id!==j.file_id||item.eTag!==j.etag||!item.file){j.retention='conflict';j.cleanup_error='File đã được sửa/thay thế; không tự xóa.';this.save(j);return;}
  const response=await graph.request('DELETE',graph.drive()+'/items/'+encodeURIComponent(j.file_id),undefined,{'If-Match':j.etag});
  if(response.status===412){j.retention='conflict';j.cleanup_error='File đổi trong lúc xóa; đã bỏ qua.';this.save(j);return;}
  j.state='deleted';j.retention=response.status===404?'missing':'deleted';j.cleanup_error='';this.save(j);
  try{const manifest=await graph.item(j.destination.slice(0,j.destination.lastIndexOf('/'))+'/.bot-retention.json');if(manifest.id)await graph.request('DELETE',graph.drive()+'/items/'+encodeURIComponent(manifest.id),undefined,{'If-Match':manifest.eTag});}catch{}
 }
 async index(capability,name){
  const match=/^([a-f0-9]{12})\.([0-9]{1,12})(?:\.([a-f0-9]{12}))?\.([A-Za-z0-9_-]{32})$/.exec(capability);
  const headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Robots-Tag':'noindex, nofollow'};
  const fail=(text,status)=>new Response(text,{status,headers});
  if(!match||!name||name.startsWith('.')||/[\/\\\r\n]/.test(name))return fail('Link không hợp lệ.',404);
  const [,jid,expiry,tag]=match;if(Number(expiry)<=now())return fail('Link đã hết hạn 12 giờ.',410);
  const j=this.get(jid);if(!j||j.state!=='done'||Math.floor(j.expires)!==Number(expiry)||j.destination.split('/').at(-1)!==name)return fail('File không còn khả dụng.',410);
  const expected=await indexURL(this.config(),tag?j:{...j,link_ctag:''},this.origin());const part=new URL(expected).pathname.split('/')[2];if(part!==capability)return fail('Link không hợp lệ.',404);
  try{const item=await new Graph(this.config(),this.ctx.storage).item(j.destination);
   if(item.status===404||item.id!==j.file_id||item.size!==j.total||(tag?item.cTag!==j.link_ctag:item.eTag!==j.etag))return fail('File đã thay đổi hoặc bị thay thế.',410);
   const direct=item['@microsoft.graph.downloadUrl'];if(!direct?.startsWith('https://'))return fail('OneDrive chưa có link tải.',503);return new Response(null,{status:302,headers:{...headers,Location:direct}});
  }catch{return fail('Chưa lấy được link OneDrive, hãy thử lại.',503);}
 }
}
