// Investigation records unverified observations without a retry control.
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
class Node {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.value='';this.textContent='';}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  reset(){}
}
const ids=new Map(), document={getElementById(id){if(!ids.has(id))ids.set(id,new Node());return ids.get(id);},createElement(tag){return new Node(tag);}};
let approve=false; const calls=[], events=[];
const response=data=>({status:200,ok:true,json:async()=>data});
const fetch=async(path,options={})=>{
  if(path==='/api/auth/me')return {status:401,ok:false,json:async()=>({})};
  if(path==='/api/google/interviews/7/email')return response({id:5,generation:1,state:'uncertain',recipient:'candidate@example.test',subject:'Interview',body:'body',start_at:'2099-01-01T10:00:00Z',end_at:'2099-01-01T11:00:00Z'});
  if(path==='/api/google/interviews/7/email/investigation'){
    if(options.method==='POST'){const body=JSON.parse(options.body);calls.push(body);events.push({observation:body.observation,actor_id:2,created_at:'now',observed_state:'uncertain'});return response({});}
    return response({delivery_id:5,generation:1,state:'uncertain',events});
  }
  return response([]);
};
const context=vm.createContext({document,fetch,window:{confirm:()=>approve},URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve)), descendants=node=>[node,...node.children.flatMap(descendants)];
const find=text=>descendants(ids.get('interviews')).find(node=>node.textContent===text);
async function click(text){const node=find(text);assert(node,`Missing ${text}`);node.handlers.click();await tick();await tick();}
async function run(){
  await tick();
  const interview='[{id:7,application_id:1,status:"scheduled",completed:false,start_at:"2099-01-01T10:00:00Z",end_at:"2099-01-01T11:00:00Z",interviewer:"HR"}]';
  vm.runInContext(`user={role:"candidate"}; renderInterviews(${interview})`,context);
  assert(!find('Open delivery investigation'));
  vm.runInContext(`user={role:"hr"}; renderInterviews(${interview})`,context);
  await click('Check Gmail confirmation status');
  assert(!find('Send reviewed Gmail confirmation'));
  await click('Open delivery investigation');
  assert(find('These are unverified operator reports. They do not establish delivery, release the cancellation block, or permit retry.'));
  await click('Record: Operator reports finding no message');
  assert.equal(calls.length,0);
  approve=true;
  await click('Record: Operator reports finding no message');
  assert.deepEqual(calls[0],{confirmed:true,delivery_id:5,expected_generation:1,expected_state:'uncertain',observation:'reported_message_not_found'});
  assert(find('now · Operator reports finding no message · HR 2 · uncertain'));
  assert(!find('Send reviewed Gmail confirmation'));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
