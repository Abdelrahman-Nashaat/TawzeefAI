// Run with Node: role-appropriate knowledge UI and stale-session response.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor() { this.children=[]; this.hidden=false; this.value=''; this.textContent=''; this.listeners={}; }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  replaceChildren(...nodes) { this.children=nodes; }
  append(...nodes) { this.children.push(...nodes); }
  reset() {}
}
const nodes = new Map();
const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id,new Node()); return nodes.get(id); },createElement() { return new Node(); }};
const response = (data,status=200) => ({status,ok:status>=200&&status<300,json:async()=>data});
let pending;
const fetch = async (path) => {
  if (path === '/api/auth/me') return response({},401);
  if (path === '/api/knowledge/answer') return new Promise(resolve=>{ pending=resolve; });
  if (path==='/api/jobs' || path==='/api/applications' || path==='/api/interviews') return response([]);
  if (path.includes('interview-cancellations')) return response({items:[],next_before:null});
  throw Error('Unexpected request '+path);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function run() {
  await tick();
  await vm.runInContext('openWorkspace({role:"employee",display_name:"Employee"})',context);
  assert.equal(nodes.get('knowledge-add').hidden,true);
  assert.equal(nodes.get('applications-panel').hidden,true);
  document.getElementById('knowledge-question').value='annual leave';
  nodes.get('knowledge-form').listeners.submit({preventDefault(){},submitter:new Node()});
  await tick();
  pending(response({notice:'Excerpts only',sources:[{title:'Policy',page:4,text:'<img src=x>'}]}));
  await tick();
  assert.equal(nodes.get('knowledge-results').children[1].children[1].textContent,'<img src=x>');
  nodes.get('knowledge-form').listeners.submit({preventDefault(){},submitter:new Node()});
  await tick();
  vm.runInContext('signedOut()',context);
  await vm.runInContext('openWorkspace({role:"candidate",display_name:"Candidate"})',context);
  pending(response({notice:'stale',sources:[{title:'Private',page:1,text:'old account'}]}));
  await tick();
  assert.equal(nodes.get('knowledge-results').children.length,0);
}
run().catch(error=>{console.error(error);process.exitCode=1;});
