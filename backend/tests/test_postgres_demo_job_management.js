// Run with Node: HR job creation, editing and stale-session safety.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor() { this.children = []; this.hidden = false; this.value = ''; this.textContent = ''; this.listeners = {}; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  replaceChildren(...nodes) { this.children = nodes; }
  append(...nodes) { this.children.push(...nodes); }
  reset() {}
}
const nodes = new Map();
const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); },
  createElement() { return new Node(); }};
const response = (data, status = 200) => ({status, ok: status >= 200 && status < 300, json: async () => data});
const calls = [];
const job = {id: 'demo-job', title: '<b>Analyst</b>', location: 'Remote', required_skills: ['Python'],
  preferred_skills: [], responsibilities: [], revision: 'a'.repeat(64)};
let delayed = null, waiting = false;
const fetch = async (path, options = {}) => {
  if (path === '/api/auth/me') return response({}, 401);
  if (path === '/api/hr/jobs' && !options.method) return waiting ? new Promise(resolve => { delayed = resolve; }) : response([job]);
  if (path === '/api/hr/jobs' && options.method === 'POST') { calls.push(JSON.parse(options.body)); return response(job, 201); }
  if (path === '/api/hr/jobs/demo-job' && options.method === 'PUT') { calls.push(JSON.parse(options.body)); return response(job); }
  if (['/api/jobs', '/api/applications', '/api/interviews'].includes(path)) return response([]);
  if (path.includes('interview-cancellations')) return response({items: [], next_before: null});
  throw Error('Unexpected request ' + path);
};
const context = vm.createContext({document, fetch, URLSearchParams, console, Date, Error, Promise, setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
async function run() {
  await tick();
  await vm.runInContext('openWorkspace({role:"hr",display_name:"HR"})', context);
  assert.equal(nodes.get('hr-jobs').hidden, false);
  nodes.get('list-hr-jobs').listeners.click(); await tick(); await tick();
  const row = nodes.get('hr-job-list').children[0];
  assert.ok(row.textContent.includes('<b>Analyst</b>'));
  row.children[0].listeners.click(); await tick();
  assert.equal(nodes.get('hr-job-id').disabled, true);
  nodes.get('hr-job-title').value = 'Updated analyst';
  nodes.get('hr-job-form').listeners.submit({preventDefault() {}, submitter: new Node()});
  await tick(); await tick();
  assert.equal(calls[0].expected_revision, 'a'.repeat(64));
  assert.equal(calls[0].job.title, 'Updated analyst');
  nodes.get('new-hr-job').listeners.click();
  nodes.get('hr-job-id').value = 'another-job';
  nodes.get('hr-job-required').value = 'Python\nSQL';
  nodes.get('hr-job-form').listeners.submit({preventDefault() {}, submitter: new Node()});
  await tick(); await tick();
  assert.deepEqual(calls[1].required_skills, ['Python', 'SQL']);
  waiting = true;
  nodes.get('list-hr-jobs').listeners.click(); await tick();
  vm.runInContext('signedOut()', context);
  await vm.runInContext('openWorkspace({role:"candidate",display_name:"Candidate"})', context);
  assert.equal(nodes.get('hr-jobs').hidden, true);
  delayed(response([job])); await tick(); await tick();
  assert.equal(nodes.get('hr-job-list').children.length, 0);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
