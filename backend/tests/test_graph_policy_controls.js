const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('function setupGraphReview(');
const end = source.indexOf('\nasync function loadStructuredResume(', start);
assert(start >= 0 && end > start);
class Node {
  constructor(tag, cls='', text='') { this.tag=tag; this.className=cls; this.textContent=text; this.childNodes=[]; this.listeners={}; this.value=''; this.disabled=false; this.hidden=false; this.isConnected=true; this.firstChild={textContent:text}; }
  append(...xs) { this.childNodes.push(...xs); }
  prepend(...xs) { this.childNodes.unshift(...xs); }
  replaceChildren(...xs) { this.childNodes=xs; }
  setAttribute() {}
  addEventListener(name, f) { this.listeners[name]=f; }
}
function nodes(root) { return [root, ...root.childNodes.flatMap(nodes)]; }
async function render(state) {
  const target = new Node('div');
  const requests = [];
  const review = {id:'review-id',awaiting_review:true,state:{application_id:1, ...state}};
  const context = {element:(...args)=>new Node(...args), currentUserId:5,
    sessionStorage:{getItem:()=> 'review-id',setItem:()=>{}},
    api:async (path,options)=>{requests.push({path,options}); return options ? {id:'review-id',awaiting_review:false,state:{application_id:1,intent:state.intent,reviewed:false,plan:[]}} : review;},
    isStale:()=>false, displayDate:(x)=>x, console, setTimeout, clearTimeout};
  vm.createContext(context);
  vm.runInContext(source.slice(start,end),context);
  context.setupGraphReview(target,{id:1},async()=>{});
  await new Promise(resolve=>setTimeout(resolve,0));
  return {target,requests};
}
(async()=>{
  const base = {intent:'semantic_review',plan:['parser','matching','semantic','policy','human_review'],semantic_suggestions:{status:'current',criteria:[]},policy:{status:'source_refs_v2'}};
  let {target,requests} = await render(base);
  let buttons=nodes(target).filter(n=>n.tag==='button');
  let discard=buttons.find(n=>n.textContent==='Discard review');
  let confirm=buttons.find(n=>n.className==='small-button' && n.textContent.includes('أؤكد'));
  assert(discard && !discard.disabled && confirm && confirm.disabled, 'stale policy must disable review while permitting discard');
  await discard.listeners.click();
  assert.strictEqual(JSON.parse(requests.at(-1).options.body).reviewed,false);
  ({target}=await render({...base,policy:{status:'abstain'}}));
  buttons=nodes(target).filter(n=>n.tag==='button');
  confirm=buttons.find(n=>n.className==='small-button' && n.textContent.includes('أؤكد'));
  assert(confirm && !confirm.disabled, 'abstention remains reviewable');
  const proposal={type:'advance_and_reserve_slot',application_id:1,availability_scope:'selected_application_only',current_status:'submitted',target_status:'advanced',candidate_id:2,assessment_id:3,slot:{start_at:'2030',end_at:'2031',interviewer:'Example'},approval_fingerprint:'fp'};
  ({target,requests}=await render({...base,intent:'semantic_advance_and_reserve_slot',action_proposal:proposal}));
  buttons=nodes(target).filter(n=>n.tag==='button');
  const approve=buttons.find(n=>n.textContent==='Approve advancement and private slot');
  const decline=buttons.find(n=>n.className==='outline-button' && n.textContent==='أرفض المقترح');
  assert(approve && approve.disabled && decline && !decline.disabled, 'stale policy must block action but permit discard');
  await decline.listeners.click();
  assert.strictEqual(JSON.parse(requests.at(-1).options.body).reviewed,false);
  ({target}=await render({...base, intent:'semantic_advance_and_reserve_slot', action_proposal:proposal,
    semantic_suggestions:{status:'stale'}, policy:{status:'abstain'}}));
  buttons=nodes(target).filter(n=>n.tag==='button');
  const staleApprove=buttons.find(n=>n.textContent==='Approve advancement and private slot');
  const staleDecline=buttons.find(n=>n.className==='outline-button' && n.textContent==='أرفض المقترح');
  assert(staleApprove && staleApprove.disabled && staleDecline && !staleDecline.disabled,
    'stale semantic evidence must block action but permit discard');
  console.log('Graph stale-policy and discard UI checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
