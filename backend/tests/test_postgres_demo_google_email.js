// Gmail UI requires explicit review, hides HR controls from candidates, and never retries on error.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.handlers={}; this.value=''; this.textContent=''; }
  addEventListener(name, fn) { this.handlers[name]=fn; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  reset() {}
}
const ids=new Map();
const document={getElementById(id){if(!ids.has(id))ids.set(id,new Node());return ids.get(id);},createElement(tag){return new Node(tag);}};
const calls=[], confirmations=[];
let approve=false, state='pending', failure=false, pending;
const data=()=>({id:12,recipient:'candidate@example.test',subject:'Interview',body:'Exact\nbody <unsafe>',start_at:'2099-01-01T10:00:00Z',end_at:'2099-01-01T11:00:00Z',state,context_changed:false});
const response=value=>({status:200,ok:true,json:async()=>value});
const fetch=async(path,options={})=>{
  if(path==='/api/auth/me') return {status:401,ok:false,json:async()=>({})};
  if(path.startsWith('/api/google/interviews/7/email')){
    calls.push({path,method:options.method||'GET',body:options.body&&JSON.parse(options.body)});
    if(pending) return new Promise(resolve=>{pending=resolve;});
    if(failure && path.endsWith('/send')) throw new Error('network failed');
    if(path.endsWith('/send')) state='confirmed';
    return response(data());
  }
  return response([]);
};
const window={confirm(text){confirmations.push(text);return approve;}};
const context=vm.createContext({document,window,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=n=>[n,...n.children.flatMap(descendants)];
const find=text=>descendants(ids.get('interviews')).find(n=>n.textContent===text);
async function click(text){const node=find(text);assert(node,`Missing ${text}`);node.handlers.click();await tick();await tick();}
const interview='[{id:7,application_id:1,status:"scheduled",completed:false,start_at:"2099-01-01T10:00:00Z",end_at:"2099-01-01T11:00:00Z",interviewer:"HR"}]';
async function run(){
  await tick();
  vm.runInContext(`user={role:"candidate"}; renderInterviews(${interview})`,context);
  assert(!find('Prepare Gmail confirmation review'));
  vm.runInContext(`user={role:"hr"}; renderInterviews(${interview})`,context);
  const input=find('Recipient email (verify independently) ').children[0]; input.value='candidate@example.test';
  await click('Prepare Gmail confirmation review'); assert.equal(calls.length,0);
  approve=true; await click('Prepare Gmail confirmation review');
  assert.deepEqual(calls[0].body,{confirmed:true,recipient:'candidate@example.test'});
  assert(find('Recipient (unverified): candidate@example.test'));
  assert(find('Subject: Interview'));
  assert(find('Exact\nbody <unsafe>'));
  approve=false; await click('Send reviewed Gmail confirmation'); assert.equal(calls.length,1);
  assert(confirmations.at(-1).includes('candidate@example.test') && confirmations.at(-1).includes('Exact\nbody <unsafe>'));
  approve=true; failure=true; await click('Send reviewed Gmail confirmation');
  assert.deepEqual(calls[1].body,{confirmed:true,delivery_id:12,recipient:'candidate@example.test'});
  assert(find('The send failed or its result is unknown. Check email status; do not retry blindly.'));
  assert(!find('Send reviewed Gmail confirmation'));
  failure=false; state='uncertain'; await click('Check Gmail confirmation status');
  assert(find('Delivery is unresolved. Do not assume the email was sent or retry it; contact an administrator.'));
  assert(!find('Send reviewed Gmail confirmation'));
  pending=true; await click('Check Gmail confirmation status');
  const resolve=pending; vm.runInContext('signedOut()',context); resolve(response(data())); await tick();
  assert(!find('Email state: uncertain'));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
