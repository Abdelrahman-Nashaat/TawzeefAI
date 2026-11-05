// Exercise rejection controls and account changes in the PostgreSQL demo.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.handlers = {}; this.value = ''; this.textContent = ''; this.hidden = false; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  reset() {}
}
const ids = new Map();
const document = { getElementById(id) { if (!ids.has(id)) ids.set(id, new Node()); return ids.get(id); }, createElement(tag) { return new Node(tag); } };
const response = (data, status = 200) => ({status, ok: status >= 200 && status < 300, json: async () => data});
let requests = [], delay = false, release;
const fetch = async (path, options = {}) => {
  requests.push([path, options]);
  if (path === '/api/auth/me') return response({}, 401);
  if (path === '/api/applications/7/decision' && delay) return new Promise(resolve => { release = resolve; });
  if (path.includes('interview-cancellations')) return response({items: [], next_before: null});
  if (['/api/jobs', '/api/applications', '/api/interviews'].includes(path)) return response([]);
  return response({});
};
const context = vm.createContext({document, fetch, URLSearchParams, console, Date, Error, Promise, setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const descendants = node => [node, ...node.children.flatMap(descendants)];
function render(role, status) {
  context.accountRole = role; context.appStatus = status;
  vm.runInContext('user={role:accountRole}; renderApplications([{id:7,status:appStatus,job_id:"j",candidate_id:4}],[])', context);
  return descendants(ids.get('applications'));
}
async function submit(form, value) {
  const area = descendants(form).find(node => node.tag === 'textarea');
  area.value = value;
  const button = descendants(form).find(node => node.textContent === 'Reject application');
  form.handlers.submit({preventDefault(){}, submitter:button}); await tick(); await tick();
}
async function run() {
  await tick();
  for (const status of ['submitted','advanced','offered']) {
    const all = render('hr', status);
    const button = all.find(node => node.textContent === 'Reject application');
    assert(button, `HR must be able to reject ${status}`);
    assert(all.some(node => node.textContent.includes('cancels future interviews')));
  }
  for (const status of ['rejected','hired']) assert(!render('hr', status).some(n => n.textContent === 'Reject application'));
  assert(!render('candidate', 'submitted').some(n => n.textContent === 'Reject application'));
  let all = render('hr', 'submitted');
  let form = all.find(node => node.tag === 'form' && descendants(node).some(n => n.textContent === 'Reject application'));
  await submit(form, '   ');
  assert(!requests.some(([p]) => p === '/api/applications/7/decision'));
  assert.equal(ids.get('message').textContent, 'Enter a reason for rejection.');
  await submit(form, '  Lacks required experience  ');
  const sent = requests.find(([p]) => p === '/api/applications/7/decision');
  assert.deepEqual(JSON.parse(sent[1].body), {decision:'reject', note:'Lacks required experience'});
  all = render('hr', 'submitted');
  form = all.find(node => node.tag === 'form' && descendants(node).some(n => n.textContent === 'Reject application'));
  delay = true; submit(form, 'Old account reason'); await tick();
  vm.runInContext('signedOut(); user={role:"candidate"}', context);
  ids.get('message').textContent = 'New user';
  release(response({})); await tick(); await tick();
  assert.equal(ids.get('message').textContent, 'New user');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
