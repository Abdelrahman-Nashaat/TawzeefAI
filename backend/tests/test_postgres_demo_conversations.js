// Run with Node: saved conversation UI and delayed response isolation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor() { this.children=[]; this.hidden=false; this.value=''; this.textContent=''; this.listeners={}; }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  replaceChildren(...nodes) { this.children=nodes; }
  append(...nodes) { this.children.push(...nodes); }
  reset() { this.value=''; }
}
const nodes = new Map();
const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id,new Node()); return nodes.get(id); },createElement() { return new Node(); }};
const response = (data,status=200) => ({status,ok:status>=200&&status<300,json:async()=>data});
let delayed, savedQuestion, deleted, generated = false, hybrid = false, collection, confirms = 0, consent = false;
const fetch = async (path, options={}) => {
  if (path === '/api/auth/me') return response({},401);
  if (path === '/api/jobs' || path === '/api/applications' || path === '/api/interviews') return response([]);
  if (path.includes('interview-cancellations')) return response({items:[],next_before:null});
  if (path === '/api/knowledge/conversations' && options.method === 'POST') return response({id: 2});
  if (path === '/api/knowledge/conversations/2' && options.method === 'DELETE') { deleted = path; return response(null, 204); }
  if (path === '/api/knowledge/conversations') return response({items:[{id:1},{id:2}]});
  if (path === '/api/knowledge/conversations/2/turns' && options.method === 'POST') {
    const payload=JSON.parse(options.body); savedQuestion=payload.question; generated=payload.generate_answer; hybrid=payload.experimental_hybrid; collection=payload.collection;
    return response({id: 1, notice:'Test notice'});
  }
  if (path === '/api/knowledge/conversations/2/turns') return response({items:[{question:'leave',mode:'extractive',sources:[{title:'Policy',page:1,text:'<img src=x>'}]}]});
  if (path === '/api/knowledge/conversations/1/turns') return new Promise(resolve=>{delayed=resolve;});
  throw Error('Unexpected request '+path);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout,window:{confirm:()=>{confirms++;return consent;}}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function run() {
  await tick();
  await vm.runInContext('openWorkspace({role:"employee",display_name:"Employee"})',context);
  nodes.get('conversation-new').listeners.click(); await tick();
  assert.equal(nodes.get('conversation-current').textContent,'Conversation 2');
  document.getElementById('conversation-question').value='leave';
  document.getElementById('conversation-collection-filter').value='job_description';
  nodes.get('conversation-form').listeners.submit({preventDefault(){},submitter:new Node()}); await tick(); await tick();
  assert.equal(savedQuestion,'leave');
  assert.equal(collection,'job_description');
  assert.equal(generated,false);
  assert.equal(hybrid,false);
  assert.equal(nodes.get('conversation-turns').children[0].children[2].textContent,'<img src=x>');
  const generateButton=new Node(); generateButton.value='generate';
  document.getElementById('conversation-question').value='second';
  nodes.get('conversation-form').listeners.submit({preventDefault(){},submitter:generateButton}); await tick();
  assert.equal(confirms,1); assert.equal(savedQuestion,'leave');
  consent=true;
  document.getElementById('conversation-hybrid').checked=true;
  nodes.get('conversation-form').listeners.submit({preventDefault(){},submitter:generateButton}); await tick(); await tick();
  assert.equal(confirms,2); assert.equal(savedQuestion,'second'); assert.equal(generated,true);
  assert.equal(hybrid,true);
  nodes.get('conversation-list').listeners.click(); await tick();
  nodes.get('conversation-list-results').children[0].listeners.click(); await tick();
  nodes.get('conversation-list-results').children[1].listeners.click(); await tick();
  delayed(response({items:[{question:'private old',mode:'extractive',sources:[]}]})); await tick();
  assert.equal(nodes.get('conversation-current').textContent,'Conversation 2');
  assert.equal(nodes.get('conversation-turns').children[0].children[0].textContent,'Question: leave');
  nodes.get('conversation-list-results').children[0].listeners.click(); await tick();
  vm.runInContext('signedOut()',context);
  await vm.runInContext('openWorkspace({role:"employee",display_name:"Next"})',context);
  delayed(response({items:[{question:'private old',mode:'extractive',sources:[]}]})); await tick();
  assert.equal(nodes.get('conversation-turns').children.length,0);
  nodes.get('conversation-new').listeners.click(); await tick();
  nodes.get('conversation-delete').listeners.click(); await tick();
  assert.equal(deleted,'/api/knowledge/conversations/2');
  assert.equal(nodes.get('conversation-current').textContent,'No conversation selected.');
  assert.equal(nodes.get('conversation-delete').hidden,true);
}
run().catch(error=>{console.error(error);process.exitCode=1;});
