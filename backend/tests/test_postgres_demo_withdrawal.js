// Candidate withdrawal requires an explicit confirmation and remains role-scoped.
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
const requests=[]; let confirmed=false;
const fetch=async(path,opts={})=>{
  requests.push({path,opts});
  if(path==='/api/auth/me') return response({},401);
  if(path==='/api/applications/7/withdraw') return response({id:7,status:'withdrawn'});
  if(path==='/api/applications') return response([{id:7,status:'withdrawn',job_id:'j',candidate_id:4}]);
  return response([]);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout,window:{confirm:()=>confirmed}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=n=>[n,...n.children.flatMap(descendants)];
const find=text=>descendants(ids.get('applications')).find(n=>n.textContent===text);
async function click(text){const node=find(text);assert(node,`Missing ${text}`);node.handlers.click();await tick();await tick();}
async function run(){
  await tick();
  vm.runInContext('user={role:"candidate"}; renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])',context);
  await click('Withdraw application');
  assert.equal(requests.filter(r=>r.path.endsWith('/withdraw')).length,0);
  confirmed=true;
  await click('Withdraw application');
  const withdrawals=requests.filter(r=>r.path.endsWith('/withdraw'));
  assert.equal(withdrawals.length,1);
  assert.equal(JSON.parse(withdrawals[0].opts.body).confirmed,true);
  assert(!find('Withdraw application'));
  vm.runInContext('user={role:"hr"}; renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])',context);
  assert(!find('Withdraw application'));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
