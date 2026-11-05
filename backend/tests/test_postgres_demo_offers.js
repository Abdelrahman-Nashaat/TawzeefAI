// Run with Node against the PostgreSQL demo script.
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
let role='candidate', offerStatus='presented', calls=[], deferred=null;
const fetch=async(path,opts={})=>{
  calls.push([path,opts]);
  if(path==='/api/auth/me')return response({},401);
  if(path==='/api/jobs'||path==='/api/applications'||path==='/api/interviews')return response([]);
  if(path.includes('interview-cancellations'))return response({items:[],next_before:null});
  if(path==='/api/applications/7/offers'&&opts.method!=='POST') {
    if(deferred)return await new Promise(resolve=>deferred(resolve));
    return response([{id:9,status:offerStatus,details:'<img src=x onerror=alert(1)>'}]);
  }
  return response({});
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=(n)=>[n,...n.children.flatMap(descendants)];
async function click(node){node.handlers.click();await tick();await tick();}
async function run(){
 await tick();
 vm.runInContext('user={role:"candidate"}',context);
 let container=new Node(); context.container=container;
 await vm.runInContext('showOffers({id:7,status:"offered"},container)',context);
 let all=descendants(container);
 assert(all.some(n=>n.tag==='pre'&&n.textContent.includes('<img')));
 assert(!all.some(n=>n.tag==='img'));
 assert(all.some(n=>n.textContent==='Accept offer'));
 assert(!all.some(n=>n.textContent==='Withdraw offer'));
 await click(all.find(n=>n.textContent==='Accept offer'));
 const acceptance=calls.find(([p])=>p==='/api/offers/9/respond');
 assert.equal(JSON.parse(acceptance[1].body).decision,'accepted');
  container=new Node();context.container=container;
  await vm.runInContext('showOffers({id:7,status:"offered"},container)',context);
  await click(descendants(container).find(n=>n.textContent==='Decline offer'));
  assert.equal(JSON.parse(calls.filter(([p])=>p==='/api/offers/9/respond').at(-1)[1].body).decision,'declined');
 vm.runInContext('user={role:"hr"}',context); container=new Node();context.container=container;
  vm.runInContext('renderApplications([{id:7,status:"advanced",job_id:"j",candidate_id:4}],[])',context);
  let form=descendants(ids.get('applications')).find(n=>n.tag==='form' && descendants(n).some(child=>child.textContent==='Present offer'));
  const terms=descendants(form).find(n=>n.tag==='textarea'); terms.value='Synthetic offer';
  const submit=descendants(form).find(n=>n.textContent==='Present offer');
  form.handlers.submit({preventDefault(){},submitter:submit}); await tick(); await tick();
  const presentation=calls.find(([p,o])=>p==='/api/applications/7/offers'&&o.method==='POST');
  assert.equal(JSON.parse(presentation[1].body).details,'Synthetic offer');
 await vm.runInContext('showOffers({id:7,status:"offered"},container)',context);
 all=descendants(container); assert(all.some(n=>n.textContent==='Withdraw offer'));
 assert(!all.some(n=>n.textContent==='Accept offer'));
 await click(all.find(n=>n.textContent==='Withdraw offer'));
 assert(calls.some(([p])=>p==='/api/offers/9/withdraw'));
 offerStatus='accepted'; container=new Node();context.container=container;
 await vm.runInContext('showOffers({id:7,status:"offered"},container)',context);
 await click(descendants(container).find(n=>n.textContent==='Record hire'));
 assert.equal(JSON.parse(calls.find(([p])=>p==='/api/applications/7/decision')[1].body).decision,'hire');
 let resolve; deferred=fn=>resolve=fn; container=new Node();context.container=container;
 const pending=vm.runInContext('showOffers({id:7,status:"offered"},container).catch(e=>e)',context);
 await tick(); vm.runInContext('signedOut(); user={role:"candidate"}',context);
 resolve(response([{id:9,status:'presented',details:'OLD PRIVATE OFFER'}])); await pending;
 assert.equal(container.children.length,0,'stale offer response must not render');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
