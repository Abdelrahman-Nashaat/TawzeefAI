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
let pending, requested, approveGeneration = false, requests = 0;
const fetch = async (path, options) => {
  if (path === '/api/auth/me') return response({},401);
  if (path === '/api/knowledge/answer') { requests++; requested = JSON.parse(options.body); return new Promise(resolve=>{ pending=resolve; }); }
  if (path==='/api/jobs' || path==='/api/applications' || path==='/api/interviews') return response([]);
  if (path.includes('interview-cancellations')) return response({items:[],next_before:null});
  throw Error('Unexpected request '+path);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout,window:{confirm:()=>approveGeneration}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function run() {
  await tick();
  await vm.runInContext('openWorkspace({role:"employee",display_name:"Employee"})',context);
  assert.equal(nodes.get('knowledge-add').hidden,true);
  assert.equal(nodes.get('applications-panel').hidden,true);
  document.getElementById('knowledge-question').value='annual leave';
  document.getElementById('knowledge-collection-filter').value='handbook';
  document.getElementById('knowledge-job-filter').value='data-analyst';
  document.getElementById('knowledge-section-filter').value='Benefits';
  nodes.get('knowledge-form').listeners.submit({preventDefault(){},submitter:new Node()});
  await tick();
  assert.equal(requested.collection, 'handbook');
  assert.equal(requested.job_id, 'data-analyst');
  assert.equal(requested.section, 'Benefits');
  pending(response({notice:'Excerpts only',sources:[{title:'Policy',page:4,job_id:'data-analyst',section:'Benefits',text:'<img src=x>'}]}));
  await tick();
  assert.equal(nodes.get('knowledge-results').children[1].children[1].textContent,'<img src=x>');
  assert.match(nodes.get('knowledge-results').children[1].children[0].textContent, /data-analyst.*Benefits/);
  const hybrid = new Node(); hybrid.value = 'hybrid';
  nodes.get('knowledge-form').listeners.submit({preventDefault(){},submitter:hybrid});
  await tick();
  assert.equal(requested.experimental_hybrid, true);
  pending(response({notice:'Experimental leads',sources:[],suggestions:[{title:'Possible policy',page:3,text:'Unverified <img>'}]}));
  await tick();
  assert.match(nodes.get('knowledge-results').children[2].textContent,/Unverified semantic leads/);
  assert.equal(nodes.get('knowledge-results').children[3].children[1].textContent,'Unverified <img>');
  const generate = new Node(); generate.value = 'generate';
  const previousRequests = requests;
  nodes.get('knowledge-form').listeners.submit({preventDefault(){},submitter:generate}); await tick();
  assert.equal(requests, previousRequests);
  approveGeneration = true;
  nodes.get('knowledge-form').listeners.submit({preventDefault(){},submitter:generate}); await tick();
  assert.equal(requested.generate_answer, true);
  pending(response({notice:'AI answer; verify',answer:'<script>generated</script> [pg-doc-1]',sources:[{title:'Policy',page:4,text:'Source'}]})); await tick();
  assert.equal(nodes.get('knowledge-results').children[1].textContent,'<script>generated</script> [pg-doc-1]');
  nodes.get('knowledge-form').listeners.submit({preventDefault(){},submitter:new Node()});
  await tick();
  vm.runInContext('signedOut()',context);
  await vm.runInContext('openWorkspace({role:"candidate",display_name:"Candidate"})',context);
  pending(response({notice:'stale',sources:[{title:'Private',page:1,text:'old account'}]}));
  await tick();
  assert.equal(nodes.get('knowledge-results').children.length,0);
}
run().catch(error=>{console.error(error);process.exitCode=1;});
