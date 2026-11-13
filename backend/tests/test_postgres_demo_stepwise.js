// Resumable reviews must complete preparation before HR may approve.
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
const requests=[]; let ready=false, proposedSlot;
const fetch=async(path,opts={})=>{
  requests.push({path,opts});
  if(path==='/api/auth/me') return response({},401);
  if(path==='/api/applications/7/graph-review-preparations') { proposedSlot=JSON.parse(opts.body).slot; return response({id:9}); }
  if(path==='/api/applications/7/graph-review-drafts') return response([{id:9,status:'pending'}]);
  if(path==='/api/graph-review-drafts/9') return response({status:'pending',intent:'advance',stale:false,result:{},slot:proposedSlot,
    preparation:{completed:ready?['parser','matching','status','scheduling','policy']:['parser'],next:ready?'human_review':'matching',ready,suggested_step:ready?'review_advance':null}});
  if(path==='/api/graph-review-drafts/9/prepare') { ready=true; return response({}); }
  if(path==='/api/graph-review-drafts/9/prepare-all') { ready=true; return response({}); }
  if(path==='/api/graph-review-drafts/9/resume') return response({});
  if(path==='/api/applications') return response([{id:7,status:'advanced',job_id:'j',candidate_id:4}]);
  if(path==='/api/hr/interview-cancellations') return response({items:[],next_before:null});
  return response([]);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=n=>[n,...n.children.flatMap(descendants)];
const find=text=>descendants(ids.get('applications')).find(n=>n.textContent===text);
async function click(text){const node=find(text);assert(node,`Missing ${text}`);node.handlers.click();await tick();await tick();}
async function run(){
  await tick();
  vm.runInContext('user={role:"hr"}; renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])',context);
  await click('Start resumable advancement proposal');
  await click('Open saved reviews'); await click('Open review');
  assert(find('Continue preparation')); assert(!find('Approve advancement'));
  await click('Complete remaining preparation');
  assert.equal(requests.filter(r=>r.path.endsWith('/prepare-all')).length,1);
  assert(find('Approve advancement'));
  assert.equal(requests.filter(r=>r.path.endsWith('/resume')).length,0);
  await click('Approve advancement');
  assert.equal(JSON.parse(requests.find(r=>r.path.endsWith('/resume')).opts.body).reviewed,true);
  vm.runInContext('renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])',context);
  const start=find('Proposed interview start (local time) ').children[0];
  const end=find('Proposed interview end (local time) ').children[0];
  const interviewer=find('Proposed interviewer ').children[0];
  start.value='2027-01-04T10:00'; end.value='2027-01-04T11:00'; interviewer.value='Synthetic interviewer';
  const resumable=find('Start resumable advancement and private slot');
  const form=descendants(ids.get('applications')).find(n=>n.tag==='form' && n.children.includes(resumable));
  form.handlers.submit({preventDefault(){},submitter:resumable}); await tick(); await tick();
  assert.deepEqual(proposedSlot, {start_at:new Date(start.value).toISOString(),end_at:new Date(end.value).toISOString(),interviewer:'Synthetic interviewer'});
  ready=false; await click('Open saved reviews'); await click('Open review');
  assert(find(`Proposed private slot (UTC): ${proposedSlot.start_at} to ${proposedSlot.end_at}; interviewer: Synthetic interviewer`));
  assert(!find('Approve advancement and private slot'));
  await click('Continue preparation');
  assert(find('Approve advancement and private slot'));
  assert.equal(requests.filter(r=>r.path.endsWith('/resume')).length,1);
  await click('Approve advancement and private slot');
  assert.equal(requests.filter(r=>r.path.endsWith('/resume')).length,2);
}
run().catch(error=>{console.error(error);process.exitCode=1;});
