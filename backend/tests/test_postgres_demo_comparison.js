// Node DOM/fetch harness for HR comparison and session changes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.handlers={}; this.value=''; this.textContent=''; this.hidden=false; }
  addEventListener(name,fn) { this.handlers[name]=fn; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  reset() {}
}
const ids=new Map();
const document={getElementById(id){if(!ids.has(id))ids.set(id,new Node());return ids.get(id);},createElement(tag){return new Node(tag);}};
const response=(data,status=200)=>({status,ok:status>=200&&status<300,json:async()=>data});
let resolveComparison, request;
const fetch=async(path,opts={})=>{
  if(path==='/api/auth/me')return response({},401);
  if(path==='/api/hr/rubric-comparison') {request=JSON.parse(opts.body); return await new Promise(resolve=>resolveComparison=resolve);}
  return response([]);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descend=(node)=>[node,...node.children.flatMap(descend)];
async function run(){
  await tick();
  vm.runInContext('user={role:"candidate"}; renderApplications([{id:1},{id:2}],[])',context);
  assert(!descend(ids.get('applications')).some(node=>node.textContent==='Compare HR reviews'));
  vm.runInContext('user={role:"hr"}; renderApplications([{id:1},{id:2}],[])',context);
  let nodes=descend(ids.get('applications'));
  nodes.filter(node=>node.type==='checkbox').forEach(node=>node.checked=true);
  nodes.find(node=>node.textContent==='Compare HR reviews').handlers.click(); await tick();
  assert.deepEqual(request.application_ids,[1,2]);
  resolveComparison(response({notice:'Human judgment',reviews:[{application_id:1,lower:20,upper:80},{application_id:2,lower:30,upper:90}],pairs:[{application_ids:[1,2],higher_range:null}]}));
  await tick(); await tick(); nodes=descend(ids.get('applications'));
  assert(nodes.some(node=>node.textContent.includes('order indeterminate')));
  nodes.find(node=>node.textContent==='Compare HR reviews').handlers.click(); await tick();
  vm.runInContext('signedOut(); user={role:"candidate"}',context);
  resolveComparison(response({notice:'OLD PRIVATE',reviews:[],pairs:[]})); await tick(); await tick();
  assert(!descend(ids.get('applications')).some(node=>node.textContent==='OLD PRIVATE'));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
