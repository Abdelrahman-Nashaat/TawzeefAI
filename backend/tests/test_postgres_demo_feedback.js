const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
class Node { constructor(tag='div') { this.tag=tag; this.children=[]; this.handlers={}; this.value=''; this.textContent=''; this.hidden=false; }
  addEventListener(n,f){this.handlers[n]=f} append(...nodes){this.children.push(...nodes)} replaceChildren(...nodes){this.children=nodes} setAttribute(){} reset(){} }
const ids = new Map(), document = {getElementById(id){if(!ids.has(id))ids.set(id,new Node());return ids.get(id)},createElement(t){return new Node(t)}};
const response=(data,status=200)=>({status,ok:status>=200&&status<300,json:async()=>data});
let calls=[], release=null;
const fetch=async(path,options={})=>{calls.push([path,options]); if(path==='/api/auth/me')return response({},401);
  if(path.includes('rubric-history'))return response({items:[{audit_event_id:42,lower:30,upper:90,is_stale:false}]});
  if(path.endsWith('candidate-feedback')&&options.method==='POST')return response({status:'ok'});
  if(path.endsWith('candidate-feedback')&&release)return await new Promise(r=>release(r));
  if(path.endsWith('candidate-feedback'))return response({status:'current',lower:30,upper:90,explanation:'<script>unsafe</script>',notice:'Human review'});
  return response({items:[]});};
const ctx=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),ctx);
const tick=()=>new Promise(r=>setImmediate(r)), all=n=>[n,...n.children.flatMap(all)];
async function run(){await tick(); vm.runInContext('user={role:"hr"}',ctx); let panel=new Node(); ctx.panel=panel;
  await vm.runInContext('manageFeedback({id:7},panel)',ctx);
  const nodes=all(panel), form=nodes.find(n=>n.tag==='form'), text=nodes.find(n=>n.tag==='textarea'), submit=nodes.find(n=>n.textContent==='Share feedback');
  assert(nodes.some(n=>n.textContent.includes('30 - 90'))); text.value='Review project evidence';
  form.handlers.submit({preventDefault(){},submitter:submit}); await tick(); await tick();
  const call=calls.find(([p,o])=>p.endsWith('candidate-feedback')&&o.method==='POST');
  assert.deepEqual(JSON.parse(call[1].body),{audit_event_id:42,explanation:'Review project evidence'});
  vm.runInContext('user={role:"candidate"}; renderApplications([{id:7,status:"submitted",job_id:"j"}],[])',ctx);
  const feedback=all(ids.get('applications')).find(n=>n.textContent==='View HR feedback'); feedback.handlers.click(); await tick(); await tick();
  assert(all(ids.get('applications')).some(n=>n.textContent==='<script>unsafe</script>'));
  let resolve; release=r=>resolve=r; feedback.handlers.click(); await tick();
  vm.runInContext('signedOut();user={role:"candidate"};renderApplications([{id:8,status:"submitted",job_id:"j"}],[])',ctx);
  resolve(response({status:'current',lower:30,upper:90,explanation:'previous account',notice:'review'})); await tick(); await tick();
  assert(!all(ids.get('applications')).some(n=>n.textContent==='previous account'));
}
run().catch(e=>{console.error(e);process.exitCode=1});
