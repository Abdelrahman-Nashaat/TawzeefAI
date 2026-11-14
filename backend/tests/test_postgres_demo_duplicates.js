// Exercise the HR duplicate check, including safe rendering and stale sessions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Node {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.handlers = {}; this.value = ''; this.textContent = ''; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  reset() {}
}
const ids = new Map();
const document = {
  getElementById(id) { if (!ids.has(id)) ids.set(id, new Node()); return ids.get(id); },
  createElement(tag) { return new Node(tag); },
};
const response = data => ({ status: 200, ok: true, json: async () => data });
let nextResponse;
const fetch = async path => {
  if (path === '/api/auth/me') return { status: 401, ok: false, json: async () => ({}) };
  if (path === '/api/applications/7/duplicates') return nextResponse();
  return response([]);
};
const context = vm.createContext({ document, fetch, URLSearchParams, console, Date, Error, Promise, setTimeout });
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const descendants = node => [node, ...node.children.flatMap(descendants)];
const find = text => descendants(ids.get('applications')).find(node => node.textContent === text);
async function click(text) {
  const node = find(text);
  assert(node, `Missing ${text}`);
  node.handlers.click();
  await tick();
  await tick();
}
async function run() {
  await tick();
  vm.runInContext('user={role:"candidate"}; renderApplications([{id:7,status:"submitted",job_id:"j"}],[])', context);
  assert(!find('Check exact CV duplicates'));
  vm.runInContext('user={role:"hr"}; renderApplications([{id:7,status:"submitted",job_id:"j",candidate_id:4}],[])', context);
  nextResponse = async () => response({ matches: [{ application_id: 8, candidate_id: 3, job_id: '<unsafe>', status: 'submitted' }], truncated: true });
  await click('Check exact CV duplicates');
  assert(find('Only identical extracted CV text is checked. A match is not proof of fraud.'));
  assert(find('Application 8 · Candidate 3 · Job <unsafe> · submitted'));
  assert(find('Only the first 50 matches are shown.'));
  nextResponse = async () => response({ matches: [], truncated: false });
  await click('Check exact CV duplicates');
  assert(find('No exact matches found.'));
  assert(!find('Only the first 50 matches are shown.'));
  let finish;
  nextResponse = () => new Promise(resolve => { finish = resolve; });
  await click('Check exact CV duplicates');
  vm.runInContext('signedOut()', context);
  finish(response({ matches: [{ application_id: 9, candidate_id: 4, job_id: 'private', status: 'submitted' }], truncated: false }));
  await tick();
  assert(!find('Application 9 · Candidate 4 · Job private · submitted'));
}
run().catch(error => { console.error(error); process.exitCode = 1; });
