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
let delayed, savedQuestion;
const fetch = async (path, options={}) => {
  if (path === '/api/auth/me') return response({},401);
  if (path === '/api/jobs' || path === '/api/applications' || path === '/api/interviews') return response([]);
  if (path.includes('interview-cancellations')) return response({items:[],next_before:null});
  if (path === '/api/knowledge/conversations' && options.method === 'POST') return response({id: 2});
  if (path === '/api/knowledge/conversations') return response({items:[{id:1},{id:2}]});
  if (path === '/api/knowledge/conversations/2/turns' && options.method === 'POST') {
    savedQuestion = JSON.parse(options.body).question; return response({id: 1});
  }
  if (path === '/api/knowledge/conversations/2/turns') return response({items:[{question:'leave',mode:'extractive',sources:[{title:'Policy',page:1,text:'<img src=x>'}]}]});
  if (path === '/api/knowledge/conversations/1/turns') return new Promise(resolve=>{delayed=resolve;});
  throw Error('Unexpected request '+path);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function run() {
  await tick();
  await vm.runInContext('openWorkspace({role:"employee",display_name:"Employee"})',context);
  nodes.get('conversation-new').listeners.click(); await tick();
  assert.equal(nodes.get('conversation-current').textContent,'Conversation 2');
  document.getElementById('conversation-question').value='leave';
  nodes.get('conversation-form').listeners.submit({preventDefault(){},submitter:new Node()}); await tick(); await tick();
  assert.equal(savedQuestion,'leave');
  assert.equal(nodes.get('conversation-turns').children[0].children[2].textContent,'<img src=x>');
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
}
run().catch(error=>{console.error(error);process.exitCode=1;});
