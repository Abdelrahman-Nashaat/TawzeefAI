// Run with Node: delayed responses from an old account must never reach the UI.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Node {
  constructor() { this.children = []; this.hidden = false; this.value = ''; this.textContent = ''; this.listeners = {}; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  replaceChildren(...nodes) { this.children = nodes; }
  append(...nodes) { this.children.push(...nodes); }
  reset() { this.resetCalled = true; }
}
const nodes = new Map();
const document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); }, createElement() { return new Node(); }};
const response = (data, status = 200) => ({status, ok: status >= 200 && status < 300, json: async () => data});
let pending = [];
let deferred = false;
let deferNotice = false;
let deferProgress = false;
const fetch = async (path) => {
  if (path === '/api/auth/me') return response({}, 401);
  if (deferred && path === '/api/applications') return new Promise(resolve => pending.push(resolve));
  if (deferNotice && path.includes('interview-cancellations')) return new Promise(resolve => pending.push(resolve));
  if (deferProgress && path.includes('/progress')) return new Promise(resolve => pending.push(resolve));
  if (path === '/api/jobs') return response([{id:'job', title:'New job'}]);
  if (path === '/api/applications' || path === '/api/interviews') return response([]);
  if (path.includes('interview-cancellations')) return response({items: [], next_before: null});
  throw Error('Unexpected path ' + path);
};
const context = vm.createContext({document, fetch, URLSearchParams, console, Date, Error, Promise, setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const tick = () => new Promise(resolve => setImmediate(resolve));

async function run() {
  await tick();
  for (const status of [200, 401]) {
    vm.runInContext('user = {role:"hr",display_name:"Old"}', context);
    document.getElementById('resume').value = 'old-private-resume.docx';
    deferred = true;
    const old = vm.runInContext('refresh().catch(error => error)', context);
    await tick();
    assert.equal(pending.length, 1);
    vm.runInContext('signedOut()', context);
    assert.equal(nodes.get('resume').value, '');
    assert.equal(nodes.get('slot-form').resetCalled, true);
    deferred = false;
    await vm.runInContext('openWorkspace({role:"candidate",display_name:"New"})', context);
    pending.shift()(response(status === 200 ? [{id:123,job_id:'job',resume_text:'PRIVATE OLD CV'}] : {detail:'expired'}, status));
    await old;
    assert.equal(nodes.get('workspace').hidden, false, 'old 401 must not sign out new user');
    assert.equal(nodes.get('welcome').textContent, 'Welcome, New');
    assert.equal(nodes.get('applications').children.length, 1);
    assert.equal(nodes.get('applications').children[0].textContent, 'No applications yet.');
  }
  vm.runInContext('user = {role:"hr",display_name:"Old"}', context);
  deferNotice = true;
  const oldNotice = vm.runInContext('loadNotices().catch(error => error)', context);
  await tick();
  vm.runInContext('signedOut()', context);
  deferNotice = false;
  await vm.runInContext('openWorkspace({role:"candidate",display_name:"New"})', context);
  pending.shift()(response({items:[{application_id:999,start_at:'2030-01-01',is_read:false}],next_before:null}));
  await oldNotice;
  assert.equal(nodes.get('notices').children[0].textContent, 'No cancellation notices.');
  vm.runInContext('renderApplications([{id:123,job_id:"job",status:"submitted"}], [{id:"job",title:"Old"}])', context);
  const card = nodes.get('applications').children[0];
  const progressButton = card.children.find(node => node.textContent === 'View current stage');
  const progressText = card.children[card.children.indexOf(progressButton) + 1];
  deferProgress = true;
  progressButton.listeners.click();
  await tick();
  vm.runInContext('signedOut()', context);
  deferProgress = false;
  await vm.runInContext('openWorkspace({role:"candidate",display_name:"New"})', context);
  pending.shift()(response({application_id:123, stage:'interviewed'}));
  await tick();
  assert.equal(progressText.textContent, '', 'old progress must not render after logout');
  assert.equal(nodes.get('applications').children[0].textContent, 'No applications yet.');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
