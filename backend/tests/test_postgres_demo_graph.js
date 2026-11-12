// Exercise the HR graph panel and protect it against delayed prior-session responses.
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
let finish;
const fetch=async(path,opts={})=>{
  if(path==='/api/auth/me')return response({},401);
  if(path==='/api/applications/7/graph-review') return await new Promise(resolve=>finish=resolve);
  return response([]);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=(n)=>[n,...n.children.flatMap(descendants)];
async function run(){
  await tick();
  vm.runInContext('user={role:"candidate"}; renderApplications([{id:7,status:"submitted",job_id:"j"}],[])',context);
  assert(!descendants(ids.get('applications')).some(n=>n.textContent==='Run read-only specialist review'));
  vm.runInContext('user={role:"hr"}; renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])',context);
  const form=descendants(ids.get('applications')).find(n=>n.tag==='form' && descendants(n).some(child=>child.textContent==='Run read-only specialist review'));
  const input=descendants(form).find(n=>n.tag==='input'); input.value='Leave policy';
  const submit=descendants(form).find(n=>n.textContent==='Run read-only specialist review');
  form.handlers.submit({preventDefault(){},submitter:submit}); await tick();
  vm.runInContext('signedOut(); user={role:"candidate"}',context);
  finish(response({policy:{sources:[{text:'OLD PRIVATE POLICY'}]}})); await tick(); await tick();
  assert(!descendants(form).some(n=>n.textContent.includes('OLD PRIVATE POLICY')));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
