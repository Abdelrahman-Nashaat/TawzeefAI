// HR can start and reopen adaptive reviews; model planning requires consent.
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
const ids = new Map();
const document = {getElementById(id){if(!ids.has(id))ids.set(id,new Node());return ids.get(id);}, createElement(tag){return new Node(tag);}};
const response = data => ({status:200,ok:true,json:async()=>data});
const sent = [];
let consent = false;
let ready = false;
const fetch = async (path, options={}) => {
  if(path==='/api/auth/me') return {status:401,ok:false,json:async()=>({})};
  if(path==='/api/applications/7/adaptive-review-preparations') {
    sent.push(JSON.parse(options.body));
    return response({id:12,plan:['parser','status'],plan_source:sent.at(-1).model_plan ? 'deterministic_fallback' : 'deterministic'});
  }
  if(path==='/api/applications/7/graph-review-drafts') return response([{id:12,status:'pending'}]);
  if(path==='/api/graph-review-drafts/12') return response({status:'pending',stale:false,intent:'review',result:{},plan:['parser','status'],plan_source:'deterministic',preparation:{ready,completed:ready?['parser','status']:['parser'],next:ready?'human_review':'status',suggested_step:'review_rubric'}});
  return response([]);
};
const context=vm.createContext({document,fetch,window:{confirm:()=>consent},URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=node=>[node,...node.children.flatMap(descendants)];
const find=text=>descendants(ids.get('applications')).find(node=>node.textContent===text);
async function click(text){const node=find(text);assert(node,`Missing ${text}`);node.handlers.click();await tick();await tick();}
async function run(){
  await tick();
  vm.runInContext('user={role:"candidate"}; renderApplications([{id:7,status:"submitted",job_id:"j"}],[])',context);
  assert(!find('Start adaptive review'));
  vm.runInContext('user={role:"hr"}; renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])',context);
  await click('Start AI-planned adaptive review');
  assert.equal(sent.length,0);
  await click('Start adaptive review');
  assert.deepEqual(sent[0],{policy_question:'',model_plan:false});
  assert(find('Adaptive review 12 started. Plan: parser, status. Source: deterministic. Open saved reviews to continue preparation.'));
  consent=true;
  await click('Start AI-planned adaptive review');
  assert.equal(sent[1].model_plan,true);
  assert(find('Adaptive review 12 started. Plan: parser, status. Source: deterministic_fallback. Open saved reviews to continue preparation.'));
  await click('Open saved reviews');
  await click('Open review');
  assert(find('Adaptive plan: parser, status. Source: deterministic.'));
  assert(find('Preparation complete: parser. Next: status.'));
  ready=true;
  await click('Open saved reviews');
  await click('Open review');
  assert(find("Suggested next step: Review the applicant's assessment. HR must decide."));
  assert(!find('Ask AI for a review suggestion'));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
