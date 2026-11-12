// Run with Node: check inbox rendering and stale account responses.
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
const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id,new Node()); return nodes.get(id); },createElement() {return new Node();}};
const response = (data,status=200) => ({status,ok:status>=200&&status<300,json:async()=>data});
let pending;
const fetch = async (path) => {
  if (path==='/api/auth/me') return response({},401);
  if (path==='/api/hr/submission-notices') return new Promise(resolve=>{ pending=resolve; });
  if (path==='/api/jobs' || path==='/api/applications' || path==='/api/interviews') return response([]);
  if (path.includes('interview-cancellations')) return response({items:[],next_before:null});
  throw Error('Unexpected request '+path);
};
const context=vm.createContext({document,fetch,URLSearchParams,console,Date,Error,Promise,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function run() {
  await tick();
  await vm.runInContext('openWorkspace({role:"hr",display_name:"HR"})',context);
  assert.equal(nodes.get('submission-notices-panel').hidden,false);
  nodes.get('load-submissions').listeners.click(); await tick();
  pending(response({items:[{id:1,application_id:7,submitted_job_id:'synthetic',is_read:false}],next_before:null}));
  await tick();
  assert.match(nodes.get('submission-notices').children[0].children[0].textContent,/Application 7/);
  nodes.get('load-submissions').listeners.click(); await tick();
  vm.runInContext('signedOut()',context);
  await vm.runInContext('openWorkspace({role:"candidate",display_name:"Candidate"})',context);
  pending(response({items:[{id:2,application_id:99,submitted_job_id:'private',is_read:false}],next_before:null}));
  await tick();
  assert.equal(nodes.get('submission-notices-panel').hidden,true);
  assert.equal(nodes.get('submission-notices').children.length,0);
}
run().catch(error=>{console.error(error);process.exitCode=1;});
