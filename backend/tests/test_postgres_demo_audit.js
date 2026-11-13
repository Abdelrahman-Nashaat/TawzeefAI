// HR-only audit UI paginates safely and ignores an older delayed response.
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
const response=data=>({status:200,ok:true,json:async()=>data});
let pending;
const fetch=async(path)=>{
  if(path==='/api/auth/me') return {status:401,ok:false,json:async()=>({})};
  if(path==='/api/applications/7/audit') return response({items:[{id:3,actor_id:2,action:'decision',decision:'advance',created_at:'now'}],next_before:3});
  if(path==='/api/applications/7/audit?before=3') return new Promise(resolve=>{pending=resolve;});
  return response([]);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=n=>[n,...n.children.flatMap(descendants)];
const find=text=>descendants(ids.get('applications')).find(n=>n.textContent===text);
async function click(text){const n=find(text);assert(n,`Missing ${text}`);n.handlers.click();await tick();await tick();}
async function run(){
  await tick();
  vm.runInContext('user={role:"hr"}; renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])',context);
  await click('View audit timeline');
  assert(find('now · decision: advance · actor 2'));
  await click('Load older audit events');
  await click('View audit timeline');
  pending(response({items:[{id:2,actor_id:2,action:'screened',decision:null,created_at:'old'}],next_before:null}));
  await tick();
  assert(find('now · decision: advance · actor 2'));
  assert(!find('old · screened · actor 2'));
  vm.runInContext('user={role:"candidate"}; renderApplications([{id:7,status:"submitted",job_id:"j"}],[])',context);
  assert(!find('View audit timeline'));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
