// Run with Node: HR document management and stale session protections.
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
let pending, deactivated = false;
const fetch = async (path, options) => {
  if (path === '/api/auth/me') return response({}, 401);
  if (path === '/api/knowledge/documents') return pending ? new Promise(resolve => { pending = resolve; }) :
    response({items: deactivated ? [] : [{id: 3, title: '<img src=x>', collection: 'handbook', audience: 'public', page: 2}]});
  if (path === '/api/knowledge/documents/3/deactivate' && options.method === 'POST') { deactivated = true; return response({id: 3}); }
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
  assert.equal(nodes.get('knowledge-management').hidden, false);
  await vm.runInContext('loadKnowledgeDocuments()', context);
  const row = nodes.get('knowledge-documents').children[0];
  assert.ok(row.textContent.includes('<img src=x>'));
  row.children[0].listeners.click();
  await tick(); await tick();
  assert.equal(deactivated, true);
  assert.equal(nodes.get('knowledge-documents').children[0].textContent, 'No active documents.');
  deactivated = false;
  await vm.runInContext('loadKnowledgeDocuments()', context);
  const staleButton = nodes.get('knowledge-documents').children[0].children[0];
  pending = true;
  const delayed = vm.runInContext('loadKnowledgeDocuments().catch(() => {})', context);
  await tick();
  vm.runInContext('signedOut()', context);
  await vm.runInContext('openWorkspace({role:"candidate",display_name:"Candidate"})', context);
  assert.equal(nodes.get('knowledge-management').hidden, true);
  pending(response({items: [{id: 9, title: 'Old HR source', collection: 'handbook', audience: 'hr', page: null}]}));
  await delayed;
  assert.equal(nodes.get('knowledge-documents').children.length, 0);
  staleButton.listeners.click();
  await tick();
  assert.equal(deactivated, false);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
