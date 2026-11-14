// Cancellation warns before the action and explains an earlier confirmation afterward.
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
const document = { getElementById(id) { if (!ids.has(id)) ids.set(id, new Node()); return ids.get(id); }, createElement(tag) { return new Node(tag); } };
let approved = false, warning = '', calls = 0;
const window = { confirm(text) { warning = text; return approved; } };
const fetch = async path => {
  if (path === '/api/auth/me') return { status: 401, ok: false, json: async () => ({}) };
  if (path === '/api/interviews/7/candidate-cancel') {
    calls++;
    return { status: 200, ok: true, json: async () => ({ confirmation_email_sent: true, cancellation_email_sent: false }) };
  }
  return { status: 200, ok: true, json: async () => [] };
};
const context = vm.createContext({ document, window, fetch, URLSearchParams, console, Date, Error, Promise, setTimeout });
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const descendants = node => [node, ...node.children.flatMap(descendants)];
async function run() {
  await tick();
  vm.runInContext('user={role:"candidate"}; refresh=async()=>{}; renderInterviews([{id:7,application_id:1,status:"scheduled",completed:false,start_at:"2099-01-01T10:00:00Z",end_at:"2099-01-01T11:00:00Z",interviewer:"HR"}])', context);
  const button = descendants(ids.get('interviews')).find(node => node.textContent === 'Cancel interview');
  assert(button);
  button.handlers.click(); await tick();
  assert(warning.includes('No cancellation email will be sent') && calls === 0);
  approved = true;
  button.handlers.click(); await tick(); await tick();
  assert.equal(calls, 1);
  assert(ids.get('message').textContent.includes('A prior confirmation email was sent; no cancellation email was sent'));
}
run().catch(error => { console.error(error); process.exitCode = 1; });
