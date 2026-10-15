"""Shared-state controller lease. Cloudflare owns Telegram's single webhook."""
import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import logging
import time
import uuid

import aiohttp
from aiohttp import web

log = logging.getLogger('failover')
TABLES = ('jobs', 'updates', 'scans', 'outputs')
MAX_SNAPSHOT = 600000


def host_key(token):
    return hmac.digest(token.encode(), b'bot-failover-host-v1', 'sha256').hex()


def webhook_key(token):
    return hmac.digest(token.encode(), b'bot-failover-webhook-v1', 'sha256').hex()


def export_state(db):
    tables = {}
    available = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in TABLES:
        if name not in available:
            continue
        columns = [r[1] for r in db.execute('PRAGMA table_info('+name+')')]
        rows = [list(r) for r in db.execute('SELECT * FROM '+name)]
        tables[name] = {'columns':columns, 'rows':rows}
    raw = gzip.compress(json.dumps({'version':1,'tables':tables},separators=(',',':')).encode(),mtime=0)
    if len(raw) > MAX_SNAPSHOT:
        raise ValueError('Shared snapshot exceeds safe limit')
    return base64.b64encode(raw).decode(),hashlib.sha256(raw).hexdigest()


def import_state(db, encoded):
    raw = base64.b64decode(encoded,validate=True)
    if len(raw)>MAX_SNAPSHOT:
        raise ValueError('Snapshot too large')
    # Bound decompressed data before parsing untrusted/corrupted state.
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        decoded=stream.read(16*1024**2+1)
    if len(decoded)>16*1024**2:
        raise ValueError('Snapshot expands too far')
    obj=json.loads(decoded)
    if obj.get('version')!=1 or not isinstance(obj.get('tables'),dict):
        raise ValueError('Invalid snapshot')
    entries=[]
    available = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if set(obj['tables']) != set(TABLES).intersection(available):
        raise ValueError('Incomplete snapshot')
    for name,table in obj['tables'].items():
        if name not in TABLES or not isinstance(table,dict):
            raise ValueError('Unexpected snapshot table')
        expected=[r[1] for r in db.execute('PRAGMA table_info('+name+')')]
        cols=table.get('columns');rows=table.get('rows')
        if not isinstance(cols,list) or not cols or len(set(cols))!=len(cols) or any(c not in expected for c in cols):
            raise ValueError('Snapshot schema mismatch')
        if not isinstance(rows,list) or any(not isinstance(r,list) or len(r)!=len(cols) for r in rows):
            raise ValueError('Invalid snapshot rows')
        entries.append((name,cols,rows))
    with db:
        for name,cols,rows in entries:
            db.execute('DELETE FROM '+name)
            identifiers=','.join('"'+c+'"' for c in cols)
            db.executemany('INSERT INTO '+name+' ('+identifiers+') VALUES ('+','.join('?' for _ in cols)+')',rows)


class LeaseLost(Exception):
    pass


class Failover:
    def __init__(self, bot):
        self.bot,self.s=bot,bot.s
        self.instance=uuid.uuid4().hex
        self.epoch=None;self.active=False;self.connected=False
        self.session=None;self.controllers=[];self.event=asyncio.Event()
        self.lock=asyncio.Lock();self.digest='';self.pending_ids=set();self.event_ids=set()
        self.role=self.s.hub_role
        self.retry_after=0
        self.retired=False

    def summary(self):
        due=self.bot.store.db.execute("SELECT MIN(expires) FROM jobs WHERE state='done' AND retention IN ('pending','armed')").fetchone()[0]
        return {'cleanup_at':int(due*1000) if due else None}

    async def request(self, action='tick'):
        async with self.lock:
            body={'instance':self.instance,'role':self.role,'epoch':self.epoch}
            if self.active or (self.epoch is None and self.role=='docker'):
                snapshot,digest=export_state(self.bot.store.db)
                if digest!=self.digest:
                    body.update(snapshot=snapshot,digest=digest,summary=self.summary())
                body['acked_updates']=[r[0] for r in self.bot.store.db.execute("SELECT id FROM updates WHERE state='done'") if r[0] in self.pending_ids]
                body['acked_events']=list(self.event_ids)
            async with self.session.post(self.s.hub_url+'/api/'+self.s.hub_bot+'/'+action,
                    json=body,headers={'Authorization':'Bearer '+host_key(self.s.token)}) as response:
                if response.status!=200:
                    raise LeaseLost('Hub unavailable')
                result=await response.json()
            if result.get('granted'):
                self.epoch=result['epoch']
                if 'snapshot' in body:self.digest=body['digest']
                self.pending_ids.difference_update(body.get('acked_updates',[]))
                self.event_ids.difference_update(body.get('acked_events',[]))
            return result

    async def checkpoint(self):
        if not self.active or self.epoch is None:
            raise LeaseLost('Controller is not active')
        result=await self.request('save')
        if not result.get('granted'):
            raise LeaseLost('Controller lease was lost')

    async def dispatch_guard(self):
        try:
            await self.checkpoint()
        except Exception:
            # Preserve dispatch intent for reconciliation, never mark a job failed
            # merely because its controller lost connectivity or ownership.
            self.event.set()
            raise asyncio.CancelledError('Dispatch stopped until ownership is confirmed') from None

    def idle(self):
        db=self.bot.store.db
        if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]:return False
        if db.execute("SELECT COUNT(*) FROM updates WHERE state IN ('pending','running')").fetchone()[0]:return False
        retention=getattr(self.bot,'retention',None)
        return not retention or not retention.lock.locked()

    async def deactivate(self):
        self.active=False
        for task in self.controllers:task.cancel()
        await asyncio.gather(*self.controllers,return_exceptions=True)
        self.controllers=[]
        await self.bot.hub_deactivate()
        self.epoch=None;self.digest='';self.pending_ids.clear();self.event_ids.clear()

    async def run(self):
        self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),trust_env=False)
        self.bot.actions.before_dispatch=self.dispatch_guard
        try:
            while True:
                try:
                    if time.monotonic()<self.retry_after:
                        await asyncio.sleep(min(15,self.retry_after-time.monotonic()))
                        continue
                    result=await self.request()
                    self.connected=True
                    if result.get('retired'):
                        if self.active:await self.deactivate()
                        self.retired=True
                        log.info('Cloudflare owns this bot; local controller remains disabled.')
                        await asyncio.Event().wait()
                    if not result.get('granted'):
                        if self.active:await self.deactivate()
                        self.epoch=None;self.digest=''
                    else:
                        if not self.active:
                            if not result.get('snapshot'):raise LeaseLost('No shared state')
                            import_state(self.bot.store.db,result['snapshot'])
                            self.digest=''
                            self.bot.store.recover()
                            self.active=True
                            async with asyncio.timeout(20):
                                self.controllers=await self.bot.hub_activate()
                            # Ensure initialization did not outlive the lease.
                            await self.checkpoint()
                        for update in result.get('updates',[]):
                            self.pending_ids.add(update['update_id'])
                            self.bot.store.ingest(update)
                        self.event_ids.update(result.get('events',[]))
                        if self.role=='render' and result.get('yield_to_docker') and self.idle():
                            await self.checkpoint()
                            await self.request('release')
                            await self.deactivate()
                        elif any(t.done() for t in self.controllers):
                            raise LeaseLost('Controller task stopped')
                except asyncio.CancelledError:raise
                except Exception as error:
                    self.connected=False
                    log.warning('Failover reconnect: %s',type(error).__name__)
                    if self.active:await self.deactivate()
                    self.epoch=None;self.digest=''
                    # A reachable hub with broken local dependencies must not
                    # let Docker repeatedly re-claim ownership and starve Render.
                    self.retry_after=time.monotonic()+75
                self.event.clear()
                interval=5 if self.active or self.role=='docker' else 15
                try:await asyncio.wait_for(self.event.wait(),interval)
                except asyncio.TimeoutError:pass
        finally:
            if self.active:
                for task in self.controllers:task.cancel()
                await asyncio.gather(*self.controllers,return_exceptions=True)
                self.controllers=[]
                try:
                    await self.checkpoint()
                    await self.request('release')
                except Exception:pass
                await self.deactivate()
            await self.session.close()

    async def wake(self, request):
        if self.role!='render' or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+host_key(self.s.token)):
            raise web.HTTPNotFound()
        self.event.set()
        return web.json_response({'accepted':True},status=202)

    def status(self):
        return {'role':self.role,'leader':self.active,'connected':self.connected,'epoch':self.epoch,'retired':self.retired}


async def notify_result(settings, job_id, result=None):
    if not settings.hub_url:return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),trust_env=False) as session:
            for attempt in range(3):
                try:
                    async with session.post(settings.hub_url+'/api/'+settings.hub_bot+'/result',
                            json={'id':job_id,**({'result':result} if result else {})},headers={'Authorization':'Bearer '+host_key(settings.token)}) as r:
                        if r.status==200:return
                except (aiohttp.ClientError,TimeoutError):pass
                await asyncio.sleep(2**attempt)
    except Exception:pass


def validate_hub(settings):
    from urllib.parse import urlsplit
    if not settings.hub_url:return
    u=urlsplit(settings.hub_url)
    if (u.scheme!='https' or not (u.hostname or '').endswith('.workers.dev') or u.username or u.password
            or u.path or u.query or u.fragment or u.port not in (None,443) or settings.hub_role not in {'docker','render'}
            or settings.hub_bot not in {'onedrive','payload'}):
        raise ValueError('Invalid failover hub configuration')
