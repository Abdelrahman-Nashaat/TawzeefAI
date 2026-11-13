// Exercise explicit HR approval and stale-draft behavior in the demo UI.
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
let stale=false;
let advanced=false;
const fetch=async(path,opts={})=>{
  requests.push({path,opts});
  if(path==='/api/auth/me') return response({},401);
  if(path==='/api/applications/7/graph-review-drafts') return response([{id:9,status:'pending'}]);
  if(path==='/api/graph-review-drafts/9') return response({status:'pending',intent:'advance',stale,result:{note:'Review this evidence'}});
  if(path==='/api/graph-review-drafts/9/resume') { advanced=JSON.parse(opts.body).reviewed; return response({}); }
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
  await click('Open saved reviews');
  await click('Open review');
  assert(find('Approve advancement'));
  assert.equal(requests.filter(r=>r.path.endsWith('/resume')).length,0);
  await click('Approve advancement');
  assert.equal(JSON.parse(requests.find(r=>r.path.endsWith('/resume')).opts.body).reviewed,true);
  assert.equal(ids.get('message').textContent,'Application advanced by HR.');
  assert(find('Status: advanced'));
  stale=true;
  await click('Open saved reviews');
  await click('Open review');
  assert(!find('Approve advancement'));
  assert(find('Discard review'));
  await click('Discard review');
  const resume=requests.filter(r=>r.path.endsWith('/resume'));
  assert.equal(JSON.parse(resume[1].opts.body).reviewed,false);
}
run().catch(error=>{console.error(error);process.exitCode=1;});
