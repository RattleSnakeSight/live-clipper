// Shared list + editor for formats and presets: a form for every field (with explanations) and a raw JSON
// view. Validation errors from the server are shown next to the field they belong to.

import { ApiError, del, get, list, post, put } from '../api.js';
import { busy, clear, confirmDialog, download, h, nextId, pickFile, switchField, toast } from '../ui.js';

const human = (k) => String(k).replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

// --- JSON Schema (GET /api/formats|presets|rules/schema) -------------------------------------------------
// Pydantic schemas nest models through $ref and optional values through anyOf. `resolve` returns the real node
// with the description of the field that referenced it.
export function resolve(root, raw) {
  let n = raw || {};
  let desc = n.description;
  for (let i = 0; i < 6; i++) {
    if (n.$ref) n = (root.$defs || {})[n.$ref.split('/').pop()] || {};
    else if (n.allOf && n.allOf.length === 1) n = n.allOf[0];
    else if (n.anyOf) n = n.anyOf.find((x) => x.type !== 'null') || {};
    else break;
    desc = desc || n.description;
  }
  return { ...n, description: desc };
}

// The raw schema node of `key` inside `info` (an object, a dict of values, or a list).
export function child(root, info, key) {
  const props = info.properties || {};
  if (key in props) return props[key];
  const extra = info.additionalProperties;
  if (extra && typeof extra === 'object') return { ...extra, description: extra.description || info.description };
  return { description: info.description };
}

// Description of the field at a dotted path, or '' when the schema has none.
export function schemaHelp(schema) {
  return (path) => {
    if (!schema) return '';
    let node = schema;
    for (const k of path.split('.')) node = child(schema, resolve(schema, node), k);
    return resolve(schema, node).description || '';
  };
}

// Builds a form for `doc`. Returns {el, read(): doc, showErrors({path: msg})}.
export function docForm(doc, schema, groupSections = false) {
  const root = schema || {};
  const readers = [];
  const errorSlots = {};
  const groupName = nextId('sections');

  function field(path, key, value, raw) {
    if (path === 'output.caption_style') return null; // Legacy JSON field, preserved by read().
    const node = resolve(root, raw);
    const help = node.description || `Sets ${path}.`;
    const id = nextId('d');
    const err = h('p', { class: 'error', id: `${id}-err`, role: 'alert' });
    errorSlots[path] = err;
    const wrap = (control, label = human(key)) => h('div', { class: 'field', dataset: { path } },
      h('label', { for: id }, label), h('p', { class: 'help', id: `${id}-help` }, help), control, err);
    const attrs = { id, help, readonly: path === 'id', 'aria-describedby': `${id}-help ${id}-err`, dataset: { path } };
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const group = h('fieldset', { class: 'panel', style: 'margin-top: 16px', dataset: { path } },
        h('legend', { class: 'label' }, human(key)), h('p', { class: 'help', style: 'margin-bottom: 8px' }, help),
        Object.entries(value).map(([k, v]) => field(`${path}.${k}`, k, v, child(root, node, k))), err);
      if (!groupSections || path.includes('.')) return group;
      return h('details', { class: 'more', name: groupName },
        h('summary', { help: `Edit the ${human(key)} settings. ${help}` }, human(key)), group);
    }
    if (typeof value === 'boolean') {
      const sw = switchField({ id, label: human(key), help, checked: value });
      sw.input.dataset.path = path;
      readers.push([path, () => sw.input.checked]);
      return h('div', { class: 'field', dataset: { path } }, sw.el, err);
    }
    if (typeof value === 'number') {
      const input = h('input', { ...attrs, type: 'number', step: 'any', value: String(value) });
      readers.push([path, () => (input.value === '' ? null : Number(input.value))]);
      return wrap(input);
    }
    if (typeof value === 'string' && node && Array.isArray(node.enum)) {
      const input = h('select', attrs, node.enum.map((v) => h('option', { value: v }, human(v))));
      input.value = value;
      readers.push([path, () => input.value]);
      return wrap(input);
    }
    if (typeof value === 'string') {
      const long = value.length > 70 || value.includes('\n');
      const input = h(long ? 'textarea' : 'input', { ...attrs, ...(long ? {} : { type: 'text' }), spellcheck: 'false' });
      input.value = value;
      readers.push([path, () => input.value]);
      return wrap(input);
    }
    if (Array.isArray(value) && value.every((v) => typeof v === 'string' || typeof v === 'number')) {
      const numeric = value.length > 0 && value.every((v) => typeof v === 'number');
      const input = h('textarea', { ...attrs, rows: Math.max(2, Math.min(6, value.length + 1)), spellcheck: 'false' });
      input.value = value.join('\n');
      input.dataset.help = `${help} One per line.`;
      readers.push([path, () => input.value.split('\n').map((s) => s.trim()).filter(Boolean).map((s) => (numeric ? Number(s) : s))]);
      return wrap(input);
    }
    // null, nested arrays and lists of objects: edit as JSON
    const input = h('textarea', { ...attrs, class: 'code', style: 'min-height: 120px', spellcheck: 'false' });
    input.value = JSON.stringify(value, null, 2);
    input.dataset.help = `${help} Written as JSON.`;
    readers.push([path, () => {
      const t = input.value.trim();
      if (!t) return null;
      try { return JSON.parse(t); } catch { throw new FieldError(path, 'This is not valid JSON.'); }
    }]);
    return wrap(input, `${human(key)} (JSON)`);
  }

  const top = resolve(root, root);
  const el = h('div', {}, Object.entries(doc).map(([k, v]) => field(k, k, v, child(root, top, k))));
  return {
    el,
    read() {
      const out = structuredClone(doc);
      for (const [path, fn] of readers) {
        const keys = path.split('.');
        let o = out;
        for (const k of keys.slice(0, -1)) o = o[k];
        o[keys.at(-1)] = fn();
      }
      return out;
    },
    showErrors(errors) {
      for (const slot of Object.values(errorSlots)) slot.textContent = '';
      el.querySelectorAll('[aria-invalid]').forEach((x) => x.removeAttribute('aria-invalid'));
      let firstBad = null;
      for (const [path, msg] of Object.entries(errors)) {
        const slot = errorSlots[path];
        if (!slot) continue;
        slot.textContent = msg;
        const input = el.querySelector(`[data-path="${CSS.escape(path)}"] input, [data-path="${CSS.escape(path)}"] select, [data-path="${CSS.escape(path)}"] textarea`);
        if (input) { input.setAttribute('aria-invalid', 'true'); firstBad = firstBad || input; }
      }
      if (firstBad) {
        const section = firstBad.closest('details');
        if (section) section.open = true;
        firstBad.focus();
      }
    },
  };
}

class FieldError extends Error {
  constructor(path, msg) { super(msg); this.path = path; }
}

const SINGULAR = { formats: 'format', presets: 'preset' };

export async function renderList(container, kind, { listId, intro }) {
  const box = h('section', { id: listId, class: 'panel', 'aria-labelledby': `${listId}-title` });
  const msg = h('div', { role: 'status', 'aria-live': 'polite' });
  container.append(box);
  const one = SINGULAR[kind];

  async function load() {
    const rows = list(await get(`/${kind}`), kind);
    const importBtn = h('button', { type: 'button', class: 'btn small', help: `Add a ${one} from a .json file on this computer.` }, 'Import file');
    importBtn.addEventListener('click', async () => {
      const text = await pickFile();
      if (text === null) return;
      let doc;
      try { doc = JSON.parse(text); } catch { clear(msg, h('p', { class: 'note bad' }, 'That file is not valid JSON.')); return; }
      try {
        // formats refuse an existing id (409) unless asked to replace it; presets import under a free id
        let saved;
        try {
          saved = await post(`/${kind}/import`, doc);
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 409)) throw e;
          if (!await confirmDialog({ title: `Replace ${doc.name || doc.id}?`, body: `A ${one} with the id "${doc.id}" exists. Importing replaces it.`, ok: 'Replace', danger: true })) return;
          saved = await post(`/${kind}/import?overwrite=true`, doc);
        }
        saved = Array.isArray(saved) ? saved[0] : saved;
        clear(msg, h('p', { class: 'note info' }, `Imported "${saved.name || saved.id}".`));
        await load();
      } catch (e) { clear(msg, h('p', { class: 'note bad' }, `Not imported: ${e.message}`)); }
    });
    clear(box,
      h('header', {}, h('h2', { id: `${listId}-title` }, human(kind)), h('div', { class: 'end row' }, importBtn)),
      intro ? h('p', { class: 'muted', style: 'margin-bottom: 8px' }, intro) : null, msg,
      h('ul', { class: 'rows' }, rows.map((d) => {
        const actions = [
          h('a', { class: 'btn small', href: `#/${kind}/${encodeURIComponent(d.id)}`, help: `Change the fields of ${d.name || d.id}.` }, 'Edit'),
          actionBtn('Duplicate', `Make an editable copy of ${d.name || d.id}.`, async () => {
            const c = await post(`/${kind}/${encodeURIComponent(d.id)}/duplicate`);
            clear(msg, h('p', { class: 'note info' }, `Created "${c.name || c.id}".`));
            await load();
          }),
          actionBtn('Export', 'Download this as a .json file you can share or import later.', async () => {
            const full = await get(`/${kind}/${encodeURIComponent(d.id)}/export`);
            download(`${d.id}.json`, `${JSON.stringify(full, null, 2)}\n`);
          }),
          kind === 'formats' && !d.active ? actionBtn('Make active', 'Use this format for channels that do not pick one.', async () => {
            await post(`/formats/${encodeURIComponent(d.id)}/activate`);
            clear(msg, h('p', { class: 'note info' }, `"${d.name || d.id}" is now the active format.`));
            await load();
          }) : null,
          d.builtin
            ? actionBtn('Reset', 'Put the built-in values back. Your changes to it are lost.', async () => {
              if (!await confirmDialog({ title: `Reset ${d.name}?`, body: 'Every change to the built-in one is undone.', ok: 'Reset', danger: true })) return;
              await post(`/${kind}/${encodeURIComponent(d.id)}/reset`);
              await load();
              clear(msg, h('p', { class: 'note info' }, `"${d.name}" is back to the built-in values.`));
            })
            : actionBtn('Delete', `Delete ${d.name || d.id}. Channels using it fall back to the built-in one.`, async () => {
              if (!await confirmDialog({ title: `Delete ${d.name || d.id}?`, body: 'Channels and rules using it fall back to the built-in one.', ok: 'Delete', danger: true })) return;
              await del(`/${kind}/${encodeURIComponent(d.id)}`);
              await load();
            }, 'danger'),
        ];
        return h('li', {}, h('div', { class: 'grow' }, h('div', { style: 'font-weight:600' }, d.name || d.id,
          d.builtin ? h('span', { class: 'pill neutral', style: 'margin-left: 8px' }, 'Built-in') : null,
          d.active ? h('span', { class: 'pill pos', style: 'margin-left: 8px' }, 'Active') : null),
        d.description ? h('div', { class: 'small muted' }, d.description) : null), h('div', { class: 'row' }, actions));
      })));
  }
  await load();
}

function actionBtn(label, help, fn, cls = '') {
  const b = h('button', { type: 'button', class: `btn small ${cls}`.trim(), help }, label);
  b.addEventListener('click', busy(b, async () => { try { await fn(); } catch (e) { toast(e.message, true); } }));
  return b;
}

// pydantic-style [{loc, msg}] or {"errors": {...}} into {"a.b": msg}
function fieldErrors(e) {
  return e instanceof ApiError ? e.fields : {};
}

export async function renderEditor(main, kind, id, backLabel) {
  const one = SINGULAR[kind];
  const [doc, schema] = await Promise.all([
    get(`/${kind}/${encodeURIComponent(id)}`), get(`/${kind}/schema`).catch(() => null)]);
  let form = docForm(doc, schema, kind === 'formats');
  const formPane = h('div', { id: 'pane-form', role: 'tabpanel', 'aria-labelledby': 'tab-form' }, form.el);
  const json = h('textarea', { id: 'doc-json', class: 'code', spellcheck: 'false', 'aria-describedby': 'doc-json-help doc-json-err',
    help: 'The same settings as raw JSON. Errors show below before anything is saved.' });
  const jsonErr = h('p', { class: 'error', id: 'doc-json-err', role: 'alert' });
  json.addEventListener('input', () => {
    try { JSON.parse(json.value); jsonErr.textContent = ''; json.removeAttribute('aria-invalid'); } catch (e) {
      jsonErr.textContent = `Not valid JSON: ${e.message}`;
      json.setAttribute('aria-invalid', 'true');
    }
  });
  const jsonPane = h('div', { id: 'pane-json', role: 'tabpanel', 'aria-labelledby': 'tab-json', hidden: true },
    h('label', { for: 'doc-json', class: 'label' }, 'JSON'),
    h('p', { class: 'help', id: 'doc-json-help' }, 'Edit everything at once, or paste a whole file.'), json, jsonErr);
  const summary = h('div', { role: 'alert' });
  let current = 'form';
  const tabs = ['form', 'json'].map((t) => h('button', { type: 'button', role: 'tab', id: `tab-${t}`, 'aria-controls': `pane-${t}`,
    'aria-selected': String(t === 'form'), tabindex: t === 'form' ? 0 : -1,
    help: t === 'form' ? 'Edit with a form, one field at a time.' : 'Edit the raw JSON.' }, t === 'form' ? 'Form' : 'JSON'));
  const select = (t) => {
    if (t === current) return;
    try {
      if (t === 'json') json.value = JSON.stringify(form.read(), null, 2);
      else {
        const parsed = JSON.parse(json.value);
        form = docForm(parsed, schema, kind === 'formats');
        clear(formPane, form.el);
      }
    } catch (e) {
      clear(summary, h('p', { class: 'note bad' }, `Fix this before switching: ${e.message}`));
      return;
    }
    clear(summary);
    current = t;
    tabs.forEach((b) => { const on = b.id === `tab-${t}`; b.setAttribute('aria-selected', String(on)); b.tabIndex = on ? 0 : -1; });
    formPane.hidden = t !== 'form';
    jsonPane.hidden = t !== 'json';
  };
  tabs.forEach((b, i) => {
    b.addEventListener('click', () => select(i ? 'json' : 'form'));
    b.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { const o = tabs[1 - i]; o.focus(); select(i ? 'form' : 'json'); }
    });
  });
  const save = h('button', { type: 'button', class: 'btn primary', help: `Check and save this ${one}.` }, 'Save');
  save.addEventListener('click', busy(save, async () => {
    clear(summary);
    let body;
    try { body = current === 'json' ? JSON.parse(json.value) : form.read(); } catch (e) {
      if (e instanceof FieldError) form.showErrors({ [e.path]: e.message });
      clear(summary, h('p', { class: 'note bad' }, e.message));
      return;
    }
    try {
      await put(`/${kind}/${encodeURIComponent(id)}`, body);
      form.showErrors({});
      clear(summary, h('p', { class: 'note info' }, 'Saved.'));
      toast('Saved.');
    } catch (e) {
      const errs = fieldErrors(e);
      if (current === 'form') form.showErrors(errs);
      clear(summary, h('div', { class: 'note bad' }, h('p', {}, 'Not saved. Fix these first:'),
        h('ul', {}, (Object.keys(errs).length ? Object.entries(errs) : [['', e.message]])
          .map(([p, m]) => h('li', {}, p ? `${p}: ${m}` : m)))));
    }
  }));
  const exportBtn = h('button', { type: 'button', class: 'btn', help: 'Download the saved version as a .json file.' }, 'Export');
  exportBtn.addEventListener('click', async () => {
    download(`${id}.json`, `${JSON.stringify(await get(`/${kind}/${encodeURIComponent(id)}/export`), null, 2)}\n`);
  });
  main.append(
    h('div', { class: 'title-row' }, h('div', {}, h('h1', {}, `Edit ${doc.name || id}`),
      h('p', {}, `Every field of this ${one}, with what it does.`)),
    h('div', { class: 'row' }, h('a', { class: 'btn ghost', href: `#/${kind}`, help: `Back to ${backLabel} without saving.` }, `Back to ${backLabel}`), exportBtn, save)),
    h('section', { id: 'doc-editor', class: 'panel' }, h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Editor view' }, tabs),
      summary, formPane, jsonPane, h('div', { class: 'row end', style: 'margin-top: 16px' }, save.cloneNode(true))));
  // the bottom Save mirrors the top one
  const bottom = main.querySelector('#doc-editor .row.end button');
  bottom.addEventListener('click', () => save.click());
}
