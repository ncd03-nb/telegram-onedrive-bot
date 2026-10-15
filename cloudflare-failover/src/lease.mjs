export const LEASE_MS=60000;
export function freshState(){return {epoch:0,leader:null,primaryAt:0,snapshot:null,digest:'',cleanupAt:null,lastCleanup:0,bot:null};}
export function grant(state,body,now){
  if(body.role==='docker')state.primaryAt=now;
  if(body.epoch!==null && body.epoch!==undefined){
    if(!state.leader || state.leader.instance!==body.instance || state.epoch!==body.epoch || state.leader.until<=now)return false;
  }else{
    if(state.leader && state.leader.until>now && state.leader.instance!==body.instance)return false;
    if(body.role==='render' && (state.primaryAt>now-LEASE_MS || !state.snapshot))return false;
    state.epoch+=1;
    state.leader={instance:body.instance,role:body.role,until:now+LEASE_MS};
  }
  state.leader.until=now+LEASE_MS;
  return true;
}
export function authorized(header,key){
  const expected='Bearer '+key;
  if(typeof header!=='string'||header.length!==expected.length)return false;
  let difference=0;for(let i=0;i<expected.length;i++)difference|=header.charCodeAt(i)^expected.charCodeAt(i);
  return difference===0;
}
