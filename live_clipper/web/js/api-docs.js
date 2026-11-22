// API reference at /api/docs: lists every route from /api/openapi.json. Drawn here so the page works offline and
// asks no other host for anything (the framework's own docs page loads its viewer from a public CDN).

import { clear, h } from './ui.js';

const METHODS = ['get', 'post', 'put', 'patch', 'delete'];
const TONE = { get: 'pos', post: 'accent', put: 'warn', patch: 'warn', delete: 'neg' };

// A schema with its $ref links filled in, so a request body reads as one block. Depth-limited: schemas can nest.
function resolve(spec, node, depth = 0) {
  if (!node || typeof node !== 'object' || depth > 5) return node;
  if (Array.isArray(node)) return node.map((x) => resolve(spec, x, depth));
  if (node.$ref) return resolve(spec, node.$ref.split('/').slice(1).reduce((o, k) => o && o[k], spec), depth + 1);
  return Object.fromEntries(Object.entries(node).map(([k, v]) => [k, resolve(spec, v, depth)]));
}

const typeOf = (s) => (s && (s.type || (s.anyOf || []).map((x) => x.type).filter(Boolean).join(' or '))) || 'any';

function operation(spec, path, method, op) {
  const params = op.parameters || [];
  const body = op.requestBody && Object.values(op.requestBody.content || {})[0];
  return h('details', { class: 'op', dataset: { text: `${method} ${path} ${op.summary || ''}`.toLowerCase() } },
    h('summary', { help: 'Show or hide the parameters, request body and responses of this route.' },
      h('span', { class: `pill ${TONE[method]}` }, method.toUpperCase()), h('code', {}, path),
      h('span', { class: 'small muted' }, op.summary || '')),
    op.description ? h('p', {}, op.description) : null,
    params.length ? [h('h3', {}, 'Parameters'), h('ul', {}, params.map((p) => h('li', {}, h('code', {}, p.name),
      ` (${p.in}${p.required ? ', required' : ''}, ${typeOf(p.schema)})`, p.description ? `: ${p.description}` : '')))] : null,
    body ? [h('h3', {}, 'Request body'),
      h('pre', { class: 'log', tabindex: 0, 'aria-label': `Request body of ${method.toUpperCase()} ${path}` },
        JSON.stringify(resolve(spec, body.schema), null, 2))] : null,
    h('h3', {}, 'Responses'),
    h('ul', {}, Object.entries(op.responses || {}).map(([code, r]) => h('li', {}, h('code', {}, code), ` ${r.description || ''}`))));
}

async function render() {
  const state = document.getElementById('state');
  const routes = document.getElementById('routes');
  let spec;
  try {
    const res = await fetch('/api/openapi.json', { credentials: 'same-origin' });
    if (!res.ok) throw new Error(res.status === 401 ? 'this tab is not signed in. Run "live-clipper start" to open a signed-in tab' : `status ${res.status}`);
    spec = await res.json();
  } catch (e) {
    state.textContent = `The route list could not load: ${e.message}.`;
    return;
  }
  // one group per part of the app: /api/channels/..., /api/clips/...
  const groups = new Map();
  for (const [path, item] of Object.entries(spec.paths || {})) {
    const name = path.split('/')[2] || 'core';
    for (const m of METHODS) {
      if (!item[m]) continue;
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(operation(spec, path, m, item[m]));
    }
  }
  const sections = [...groups].sort(([a], [b]) => a.localeCompare(b)).map(([name, ops]) => h('section', { 'aria-label': name },
    h('div', { class: 'group-head' }, h('h2', {}, `/api/${name}`)), h('div', { class: 'group' }, ops)));
  clear(routes, sections);
  const total = [...groups.values()].reduce((n, ops) => n + ops.length, 0);
  const filter = () => {
    const words = document.getElementById('q').value.trim().toLowerCase();
    let shown = 0;
    for (const s of sections) {
      let any = false;
      for (const op of s.querySelectorAll('.op')) {
        op.hidden = Boolean(words) && !op.dataset.text.includes(words);
        if (!op.hidden) { any = true; shown++; }
      }
      s.hidden = !any;
    }
    state.textContent = `${shown} of ${total} routes shown. Version ${spec.info ? spec.info.version : 'unknown'}.`;
  };
  document.getElementById('q').addEventListener('input', filter);
  filter();
}

render();
