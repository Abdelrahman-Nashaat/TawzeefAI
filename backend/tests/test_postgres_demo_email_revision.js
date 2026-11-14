// Revised drafts require an explicit review and send their current generation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.value='';this.textContent='';}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  reset(){}
}
const ids=new Map();
const document={getElementById(id){if(!ids.has(id))ids.set(id,new Node());return ids.get(id);},createElement(tag){return new Node(tag);}};
let consent=false, state='pending', generation=0, recipient='first@example.com';
const calls=[];
const data=()=>({id:7,recipient,state,generation,subject:'Interview',body:'Reviewed body',start_at:'2099-01-01T10:00:00Z',end_at:'2099-01-01T11:00:00Z'});
const response=value=>({status:200,ok:true,json:async()=>value});
const fetch=async(path,options={})=>{
  if(path==='/api/auth/me')return {status:401,ok:false,json:async()=>({})};
  if(path.startsWith('/api/google/interviews/7/email')){
    calls.push({path,body:options.body&&JSON.parse(options.body)});
    if(path.endsWith('/revise')){recipient=calls.at(-1).body.recipient;generation++;}
    if(path.endsWith('/send'))state='confirmed';
    return response(data());
  }
  return response([]);
};
const context=vm.createContext({document,fetch,window:{confirm:()=>consent},URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=node=>[node,...node.children.flatMap(descendants)];
const find=text=>descendants(ids.get('interviews')).find(node=>node.textContent===text);
async function click(text){const node=find(text);assert(node,`Missing ${text}`);node.handlers.click();await tick();await tick();}
async function run(){
  await tick();
  vm.runInContext('user={role:"hr"}; renderInterviews([{id:7,application_id:1,status:"scheduled",completed:false,start_at:"2099-01-01T10:00:00Z",end_at:"2099-01-01T11:00:00Z",interviewer:"HR"}])',context);
  await click('Check Gmail confirmation status');
  find('Correct recipient (verify independently) ').children[0].value='second@example.com';
  await click('Revise Gmail confirmation draft'); assert.equal(calls.length,1);
  consent=true; await click('Revise Gmail confirmation draft');
  assert.deepEqual(calls[1].body,{confirmed:true,delivery_id:7,expected_generation:0,recipient:'second@example.com'});
  assert(find('Recipient (unverified): second@example.com'));
  await click('Send reviewed Gmail confirmation');
  assert.deepEqual(calls[2].body,{confirmed:true,delivery_id:7,recipient:'second@example.com',generation:1});
  assert(!find('Revise Gmail confirmation draft'));
  state='uncertain'; await click('Check Gmail confirmation status');
  assert(!find('Revise Gmail confirmation draft'));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
