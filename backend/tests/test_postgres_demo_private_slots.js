// Run with Node against the PostgreSQL demo script.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.handlers={}; this.value=''; this.textContent=''; this.hidden=false; }
  addEventListener(name,fn) { this.handlers[name]=fn; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  reset() { for(const n of this.children) n.value=''; }
}
const ids=new Map();
const document={getElementById(id){if(!ids.has(id))ids.set(id,new Node());return ids.get(id);},createElement(tag){return new Node(tag);}};
const response=(data,status=200)=>({status,ok:status>=200&&status<300,json:async()=>data});
let calls=[], pendingResolve=null, delayed=false;
const slot={id:5,start_at:'2030-01-01T10:00:00Z',end_at:'2030-01-01T11:00:00Z',interviewer:'<unsafe>',booked:false};
const fetch=async(path,opts={})=>{
  calls.push([path,opts]);
  if(path==='/api/auth/me')return response({},401);
  if(path==='/api/applications/7/private-slots' && opts.method!=='POST') {
    if(delayed)return await new Promise(resolve=>pendingResolve=resolve);
    return response([slot]);
  }
  if(path==='/api/jobs'||path==='/api/applications'||path==='/api/interviews')return response([]);
  if(path.includes('interview-cancellations'))return response({items:[],next_before:null});
  return response({id:5});
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=n=>[n,...n.children.flatMap(descendants)];
async function run(){
 await tick();
 vm.runInContext('user={role:"hr"}; renderApplications([{id:7,status:"advanced",job_id:"j",candidate_id:4}],[])',context);
 const tree=descendants(ids.get('applications'));
 const form=tree.find(n=>n.tag==='form'&&descendants(n).some(c=>c.textContent==='Reserve slot for this candidate'));
 const inputs=descendants(form).filter(n=>n.tag==='input');
 assert.equal(inputs.length,3);
 inputs[0].value='2030-01-01T10:00'; inputs[1].value='2030-01-01T11:00'; inputs[2].value='HR';
 form.handlers.submit({preventDefault(){},submitter:descendants(form).find(n=>n.type==='submit')}); await tick(); await tick();
 const creation=calls.find(([p,o])=>p==='/api/applications/7/private-slots'&&o.method==='POST');
 assert.equal(JSON.parse(creation[1].body).interviewer,'HR');
 const content=descendants(ids.get('applications'));
 assert(content.some(n=>n.textContent.includes('<unsafe>'))); assert(!content.some(n=>n.tag==='unsafe'));
 const withdraw=content.find(n=>n.textContent==='Withdraw reservation');
 withdraw.handlers.click(); await tick(); await tick();
 assert(calls.some(([p])=>p==='/api/slots/5/withdraw'));
 vm.runInContext('user={role:"candidate"}; renderApplications([{id:7,status:"advanced",job_id:"j"}],[])',context);
 assert(!descendants(ids.get('applications')).some(n=>n.textContent==='Reserve slot for this candidate'));
 vm.runInContext('user={role:"hr"}',context); const target=new Node();context.target=target; delayed=true;
 const request=vm.runInContext('showPrivateSlots({id:7},target).catch(e=>e)',context);await tick();
 vm.runInContext('signedOut(); user={role:"candidate"}',context);
 pendingResolve(response([slot]));await request;
 assert.equal(target.children.length,0,'stale HR reservation must not render');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
