// Exercise the HR proposal UI, visible approval details and stale sessions.
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
const response=(data,status=200)=>({status,ok:status>=200&&status<300,json:async()=>data});
const requests=[];
let stale=false, slot=null, conflict=false, advanced=false;
const fetch=async(path,opts={})=>{
  requests.push({path,opts});
  if(path==='/api/auth/me') return response({},401);
  if(path==='/api/applications/7/graph-review-drafts' && opts.method==='POST') {
    slot=JSON.parse(opts.body).slot; return response({id:9});
  }
  if(path==='/api/applications/7/graph-review-drafts') return response([{id:9,status:'pending'}]);
  if(path==='/api/graph-review-drafts/9') return response({status:'pending',intent:'advance',stale,slot,result:{note:'Review this evidence'}});
  if(path==='/api/graph-review-drafts/9/resume') { if(conflict) return response({detail:'Interviewer or candidate slot conflict'},409); advanced=true; return response({}); }
  if(path==='/api/applications') return response([{id:7,status:advanced?'advanced':'submitted',job_id:'j',candidate_id:4}]);
  if(path==='/api/hr/interview-cancellations') return response({items:[],next_before:null});
  return response([]);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=n=>[n,...n.children.flatMap(descendants)];
const find=text=>descendants(ids.get('applications')).find(n=>n.textContent===text);
async function click(text){ const node=find(text); assert(node,`Missing ${text}`); node.handlers.click(); await tick(); await tick(); }
async function run(){
  await tick();
  vm.runInContext('user={role:"hr"}; renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])',context);
  const form=descendants(ids.get('applications')).find(n=>n.tag==='form' && descendants(n).some(x=>x.textContent==='Propose advancement and private slot'));
  const fields=descendants(form).filter(n=>n.tag==='input');
  fields[0].value='2030-01-01T10:00'; fields[1].value='2030-01-01T11:00'; fields[2].value='<Example>';
  form.handlers.submit({preventDefault(){},submitter:descendants(form).find(n=>n.type==='submit')}); await tick(); await tick();
  assert.equal(slot.interviewer,'<Example>');
  assert.equal(requests.filter(r=>r.path.endsWith('/resume')).length,0);
  await click('Open saved reviews'); await click('Open review');
  assert(find(`Proposed private slot (UTC): ${slot.start_at} to ${slot.end_at}; interviewer: ${slot.interviewer}`));
  assert(find('Approve advancement and private slot'));
  const oldApproval=find('Approve advancement and private slot');
  vm.runInContext('signedOut(); user={role:"candidate"}',context);
  oldApproval.handlers.click(); await tick(); await tick();
  assert.equal(requests.filter(r=>r.path.endsWith('/resume')).length,0,'stale session must not approve');
  vm.runInContext('user={role:"hr"}; renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])',context);
  stale=true; await click('Open saved reviews'); await click('Open review');
  assert(!find('Approve advancement and private slot')); assert(find('Discard review'));
  stale=false; await click('Open saved reviews'); await click('Open review');
  conflict=true; await click('Approve advancement and private slot');
  assert(!find('Approve advancement and private slot'));
  assert(find('The review could not be completed. Reopen it before trying again.'));
  conflict=false; await click('Open saved reviews'); await click('Open review');
  await click('Approve advancement and private slot');
  assert.equal(JSON.parse(requests.find(r=>r.path.endsWith('/resume')).opts.body).reviewed,true);
  assert.equal(ids.get('message').textContent,'Application advanced and private slot reserved. The candidate must book it.');
  assert(find('Status: advanced'));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
