import {test} from 'node:test';
import assert from 'node:assert/strict';
import {freshState,grant,LEASE_MS,authorized} from '../src/lease.mjs';
test('Docker lease, crash failover, fencing, and safe failback',()=>{
 const s=freshState(),docker={instance:'d',role:'docker',epoch:null},render={instance:'r',role:'render',epoch:null};
 assert.equal(grant(s,docker,100000),true);s.snapshot='seed';const old=s.epoch;
 assert.equal(grant(s,render,100010),false);
 assert.equal(grant(s,render,100000+LEASE_MS+1),true);const backup=s.epoch;
 assert.equal(grant(s,{...docker,epoch:old},200000),false);
 assert.equal(grant(s,docker,200000),false);
 assert.equal(s.leader.role,'render');
 assert.equal(grant(s,{...render,epoch:backup},200001),true);
 s.leader=null;assert.equal(grant(s,docker,200002),true);assert.equal(s.epoch,backup+1);
 assert.equal(grant(s,{...render,epoch:backup},200003),false);
});
test('Render cannot initialize an empty state or override fresh primary',()=>{
 const s=freshState();assert.equal(grant(s,{instance:'r',role:'render',epoch:null},100000),false);
});
test('authentication rejects absent and incorrect credentials',()=>{
 assert.equal(authorized(null,'key'),false);assert.equal(authorized('Bearer bad','key'),false);assert.equal(authorized('Bearer key','key'),true);
});
