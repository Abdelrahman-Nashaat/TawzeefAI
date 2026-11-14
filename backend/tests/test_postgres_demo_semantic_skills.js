// Semantic leads are opt-in, masked, source-bound and cleared on errors.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
class Node {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.handlers={}; this.value=''; this.textContent=''; }
  addEventListener(name, fn) { this.handlers[name]=fn; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  setAttribute() {}
  reset() {}
}
const ids=new Map(), document={getElementById(id){if(!ids.has(id))ids.set(id,new Node());return ids.get(id);},createElement(tag){return new Node(tag);}};
const response=(data,status=200)=>({status,ok:status>=200&&status<300,json:async()=>data});
let consent=false, requests=0, reply=()=>response({source_digest:'a',notice:'Unverified',criteria:[{category:'mandatory_skills',label:'Python',candidates:[{retrieval_method:'exact_phrase',excerpt:'Synthetic Python work'}]}]});
const fetch=async(path)=>{if(path==='/api/auth/me')return response({},401);if(path.endsWith('/rubric-source'))return response({source_digest:'a',resume_text:'Synthetic Python work',job:{}});if(path.endsWith('/semantic-skill-suggestions')){requests++;return reply();}return response([]);};
const context=vm.createContext({document,fetch,window:{confirm:()=>consent},URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve)), descendants=node=>[node,...node.children.flatMap(descendants)];
async function run(){
  await tick();
  const container=new Node(); context.container=container;
  vm.runInContext('user={role:"hr"}',context);
  await vm.runInContext('showRubric({id:7},container)',context);
  const find=text=>descendants(container).find(node=>node.textContent===text);
  const click=async()=>{find('Suggest local skill evidence').handlers.click();await tick();await tick();};
  await click(); assert.equal(requests,0);
  consent=true; await click();
  assert.equal(requests,1); assert(find('exact_phrase: Synthetic Python work')); assert(find('Unverified'));
  reply=()=>response({detail:'Changed'},409); await click();
  assert(!find('exact_phrase: Synthetic Python work')); assert(find('Suggestions unavailable. Reopen the rubric and try again.'));
  reply=()=>response({source_digest:'changed',criteria:[],notice:'stale'}); await click();
  assert(!find('stale')); assert(find('Suggestions unavailable. Reopen the rubric and try again.'));
  let finish; reply=()=>new Promise(resolve=>{finish=resolve;});
  await click(); vm.runInContext('signedOut()',context);
  finish(response({source_digest:'a',criteria:[],notice:'private'})); await tick();
  assert(!find('private'));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
