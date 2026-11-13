// Run with Node: verify role visibility and session-bound dashboard output.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor() { this.children=[]; this.hidden=false; this.value=''; this.textContent=''; this.listeners={}; }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  replaceChildren(...nodes) { this.children=nodes; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this[name]=value; }
  reset() {}
}
const nodes = new Map();
const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id,new Node()); return nodes.get(id); },createElement() {return new Node();}};
const response = (data,status=200) => ({status,ok:status>=200&&status<300,json:async()=>data});
let pending;
const fetch = async (path) => {
  if (path==='/api/auth/me') return response({},401);
  if (path==='/api/dashboard/jobs') return new Promise(resolve=>{ pending=resolve; });
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
  assert.equal(nodes.get('hr-dashboard').hidden,false);
  nodes.get('load-dashboard').listeners.click();
  await tick();
  pending(response({definition:'Recorded counts',jobs:[{title:'Example',applications_total:2,screening_events:1,pipeline:[{status:'submitted',count:2}],milestones:{screened:1},time_to_hire:{average_days:null,count:0}}]}));
  await tick();
  assert.equal(nodes.get('dashboard-results').children[1].children[1].textContent,'Applications: 2 | Screening events: 1');
  assert.match(nodes.get('dashboard-results').children[1].children[3].textContent,/unavailable/);
  const chart = nodes.get('dashboard-results').children[1].children[4];
  assert.equal(chart.children[1].children[1]['aria-label'], 'submitted: 2 of 2');
  assert.equal(chart.children[1].children[1].value, 2);
  assert.match(nodes.get('dashboard-results').children[1].children[5].textContent, /screened 1/);
  nodes.get('load-dashboard').listeners.click();
  await tick();
  vm.runInContext('signedOut()',context);
  await vm.runInContext('openWorkspace({role:"candidate",display_name:"Candidate"})',context);
  pending(response({definition:'OLD PRIVATE DATA',jobs:[]}));
  await tick();
  assert.equal(nodes.get('hr-dashboard').hidden,true);
  assert.equal(nodes.get('dashboard-results').children.length,0);
}
run().catch(error=>{console.error(error);process.exitCode=1;});
