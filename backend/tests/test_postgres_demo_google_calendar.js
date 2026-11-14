// Calendar provider actions require a reviewed event and an explicit HR confirmation.
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
const calls = [], confirmations = [];
let approve = false, state = 'pending', deferred, networkFailure = false;
const delivery = () => ({ event_id: 'event123', state, start_at: '2099-01-01T10:00:00+00:00', end_at: '2099-01-01T11:00:00+00:00', context_changed: false });
const response = data => ({ status: 200, ok: true, json: async () => data });
const fetch = async (path, options = {}) => {
  if (path === '/api/auth/me') return { status: 401, ok: false, json: async () => ({}) };
  if (path.startsWith('/api/google/interviews/7/calendar')) {
    calls.push({ path, method: options.method || 'GET', body: options.body && JSON.parse(options.body) });
    if (deferred) return new Promise(resolve => { deferred = resolve; });
    if (networkFailure && path.endsWith('/send')) throw new Error('network failed');
    if (path.endsWith('/send')) state = 'confirmed';
    if (path.endsWith('/remove')) state = 'removed';
    return response(delivery());
  }
  return response([]);
};
const window = { confirm(text) { confirmations.push(text); return approve; } };
const context = vm.createContext({ document, window, fetch, URLSearchParams, console, Date, Error, Promise, setTimeout });
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const descendants = node => [node, ...node.children.flatMap(descendants)];
const find = text => descendants(ids.get('interviews')).find(node => node.textContent === text);
async function click(text) {
  const node = find(text);
  assert(node, `Missing ${text}`);
  node.handlers.click();
  await tick(); await tick();
}
const interview = '[{id:7,application_id:1,status:"scheduled",completed:false,start_at:"2099-01-01T10:00:00Z",end_at:"2099-01-01T11:00:00Z",interviewer:"HR"}]';
async function run() {
  await tick();
  vm.runInContext(`user={role:"candidate"}; renderInterviews(${interview})`, context);
  assert(!find('Prepare Google Calendar review'));
  assert(!find('Check Google Calendar status'));
  vm.runInContext(`user={role:"hr"}; renderInterviews(${interview})`, context);
  assert.equal(calls.length, 0);
  await click('Prepare Google Calendar review');
  assert.deepEqual(calls[0], { path: '/api/google/interviews/7/calendar', method: 'POST', body: { confirmed: true } });
  assert(find('Event ID: event123'));
  assert(find('Start: 2099-01-01T10:00:00+00:00'));
  assert(find('End: 2099-01-01T11:00:00+00:00'));
  await click('Create Google Calendar event');
  assert.equal(calls.length, 1);
  assert(confirmations[0].includes('event123') && confirmations[0].includes('2099-01-01T10:00:00+00:00'));
  approve = true;
  await click('Create Google Calendar event');
  assert.deepEqual(calls[1], { path: '/api/google/interviews/7/calendar/send', method: 'POST', body: { confirmed: true, event_id: 'event123' } });
  assert(find('Google Calendar state: confirmed'));
  approve = false;
  await click('Remove Google Calendar event');
  assert.equal(calls.length, 2);
  approve = true;
  await click('Remove Google Calendar event');
  assert.deepEqual(calls[2], { path: '/api/google/interviews/7/calendar/remove', method: 'POST', body: { confirmed: true, event_id: 'event123' } });
  assert(find('The event was removed. The local interview is still scheduled unless separately cancelled.'));
  state = 'uncertain';
  await click('Check Google Calendar status');
  assert(find('The provider outcome is unresolved. Check the calendar and contact an administrator; do not assume the event was created or removed.'));
  assert(!find('Create Google Calendar event') && !find('Remove Google Calendar event'));
  state = 'pending';
  await click('Check Google Calendar status');
  networkFailure = true;
  await click('Create Google Calendar event');
  assert(find('The request failed or its result is unknown. Check Google Calendar status before taking further action.'));
  assert(!find('Create Google Calendar event') && !find('Remove Google Calendar event'));
  networkFailure = false;
  state = 'uncertain';
  await click('Check Google Calendar status');
  deferred = true;
  await click('Check Google Calendar status');
  const resolve = deferred;
  vm.runInContext('signedOut()', context);
  resolve(response(delivery()));
  await tick();
  assert(!find('Google Calendar state: uncertain'));
}
run().catch(error => { console.error(error); process.exitCode = 1; });
