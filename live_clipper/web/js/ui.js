// DOM helpers, form fields, toasts, dialogs and the help tooltip. Text is always set as text, never as HTML.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === 'class') el.className = v;
    else if (k === 'help') el.dataset.help = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked' || k === 'selected' || k === 'disabled' || k === 'hidden') el[k] = Boolean(v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

let uid = 0;
export const nextId = (p = 'f') => `${p}-${++uid}`;

// --- form fields -----------------------------------------------------------------------------------------

function describe(input, id, help) {
  const ids = [`${id}-help`, `${id}-err`];
  input.setAttribute('aria-describedby', ids.join(' '));
  input.dataset.help = help;
  return [h('p', { class: 'help', id: `${id}-help` }, help), h('p', { class: 'error', id: `${id}-err`, role: 'alert' })];
}

export function textField({ id = nextId(), label, help, value = '', type = 'text', name, ...attrs }) {
  const input = h(type === 'textarea' ? 'textarea' : 'input', {
    id, name: name || id, ...(type === 'textarea' ? {} : { type }), ...attrs,
  });
  input.value = value ?? '';
  const [helpEl, errEl] = describe(input, id, help);
  return { el: h('div', { class: 'field' }, h('label', { for: id }, label), helpEl, input, errEl), input };
}

export function selectField({ id = nextId(), label, help, value, options, name, ...attrs }) {
  const input = h('select', { id, name: name || id, ...attrs },
    options.map((o) => h('option', { value: o.value }, o.label)));
  if (value !== undefined && value !== null) input.value = String(value);
  const [helpEl, errEl] = describe(input, id, help);
  return { el: h('div', { class: 'field' }, h('label', { for: id }, label), helpEl, input, errEl), input };
}

export function switchField({ id = nextId(), label, help, checked = false, onchange, disabled, disabledReason }) {
  const input = h('input', { type: 'checkbox', role: 'switch', class: 'switch', id, checked, onchange, disabled });
  input.dataset.help = help;
  const why = disabled && disabledReason ? h('p', { class: 'help', id: `${id}-why` }, disabledReason) : null;
  input.setAttribute('aria-describedby', `${id}-help${why ? ` ${id}-why` : ''}`);
  const el = h('div', { class: 'switch-row' },
    h('div', { class: 'text' }, h('label', { for: id, class: 'label' }, label),
      h('p', { class: 'help', id: `${id}-help` }, help), why),
    input);
  return { el, input };
}

// options: [{value, label, hint?}]; type radio | checkbox. Returns {el, values()}.
export function choiceGroup({ name = nextId('g'), legend, help, type = 'radio', options, value }) {
  const chosen = new Set(Array.isArray(value) ? value.map(String) : [String(value)]);
  const helpId = `${name}-help`;
  const errId = `${name}-err`;
  const inputs = options.map((o) => h('input', {
    type, name, value: o.value, checked: chosen.has(String(o.value)), help: o.hint ? `${o.hint}` : help,
    'aria-describedby': helpId,
  }));
  const el = h('fieldset', { 'aria-describedby': `${helpId} ${errId}` },
    h('legend', {}, legend), help ? h('p', { class: 'help', id: helpId }, help) : null,
    options.map((o, i) => h('label', { class: 'choice' }, inputs[i],
      h('span', {}, o.label, o.hint ? h('small', {}, o.hint) : null))),
    h('p', { class: 'error', id: errId, role: 'alert' }));
  const values = () => inputs.filter((i) => i.checked).map((i) => i.value);
  return { el, inputs, values, value: () => values()[0] };
}

export function setError(container, msg) {
  const err = container.querySelector('.error');
  if (err) err.textContent = msg || '';
  for (const c of container.querySelectorAll('input, select, textarea')) {
    if (msg) c.setAttribute('aria-invalid', 'true');
    else c.removeAttribute('aria-invalid');
  }
}

export function btn(label, help, onclick, cls = '') {
  return h('button', { type: 'button', class: `btn ${cls}`.trim(), help, onclick }, label);
}

// --- feedback --------------------------------------------------------------------------------------------

export function announce(msg) {
  const a = document.getElementById('announcer');
  a.textContent = '';
  setTimeout(() => { a.textContent = msg; }, 30);
}

export function toast(msg, bad = false) {
  const t = h('div', { class: `toast${bad ? ' bad' : ''}`, role: bad ? 'alert' : 'status' }, msg);
  document.getElementById('toasts').append(t);
  setTimeout(() => t.remove(), bad ? 8000 : 4000);
}

// Native <dialog> gives focus trapping, Escape and inert background for free.
export function confirmDialog({ title, body, ok = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    const d = h('dialog', { 'aria-labelledby': 'dlg-title' },
      h('h2', { id: 'dlg-title' }, title), h('p', {}, body),
      h('div', { class: 'row' },
        btn('Cancel', 'Close without changing anything.', () => d.close('cancel')),
        btn(ok, `${ok}. ${body}`, () => d.close('ok'), danger ? 'danger' : 'primary')));
    d.addEventListener('close', () => {
      d.remove();
      if (opener && opener.focus) opener.focus();
      resolve(d.returnValue === 'ok');
    });
    document.body.append(d);
    d.showModal();
  });
}

// A dialog with one text field. `submit(value)` runs on OK; if it throws, the message shows under the field and
// the dialog stays open. Resolves true after a successful submit, false on cancel.
export function inputDialog({ title, body, label, help, ok = 'Save', submit, type = 'text' }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    const f = textField({ label, help, type, autocomplete: 'off', spellcheck: 'false' });
    const okBtn = btn(ok, `${ok}. ${help}`, () => form.requestSubmit(), 'primary');
    const form = h('form', { novalidate: true },
      h('h2', { id: 'dlg-title' }, title), h('p', {}, body), f.el,
      h('div', { class: 'row', style: 'margin-top: 12px' }, btn('Cancel', 'Close without changing anything.', () => d.close('cancel')), okBtn));
    const d = h('dialog', { 'aria-labelledby': 'dlg-title' }, form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      okBtn.disabled = true;
      setError(f.el, '');
      try { await submit(f.input.value.trim()); d.close('ok'); } catch (ex) {
        setError(f.el, (ex.fields && Object.values(ex.fields)[0]) || ex.message);
        f.input.focus();
      } finally { okBtn.disabled = false; }
    });
    d.addEventListener('close', () => {
      d.remove();
      if (opener && opener.focus) opener.focus();
      resolve(d.returnValue === 'ok');
    });
    document.body.append(d);
    d.showModal();
    f.input.focus();
  });
}

export function busy(button, fn) {
  return async (...args) => {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    try { return await fn(...args); } finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  };
}

// --- formatting ------------------------------------------------------------------------------------------

export const sentence = (s) => String(s || '').replace(/^./, (c) => c.toUpperCase());

export function fmtBytes(n) {
  if (n === null || n === undefined) return '-';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}

export function fmtDuration(s) {
  if (s === null || s === undefined) return '-';
  s = Math.round(Number(s));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}

export function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
}

export function ago(ts) {
  if (!ts) return '-';
  const t = typeof ts === 'number' ? ts * 1000 : Date.parse(ts);
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} days ago`;
}

export function download(filename, text) {
  const a = h('a', { href: URL.createObjectURL(new Blob([text], { type: 'application/json' })), download: filename });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

export function pickFile(accept = '.json,application/json') {
  return new Promise((resolve) => {
    const input = h('input', { type: 'file', accept, hidden: true, help: 'Choose a file to import.' });
    input.addEventListener('change', async () => {
      const f = input.files[0];
      input.remove();
      resolve(f ? await f.text() : null);
    });
    document.body.append(input);
    input.click();
  });
}

// --- help tooltip ----------------------------------------------------------------------------------------
// Every control carries data-help. Fields show it as visible helper text; other controls get a hidden
// description for screen readers and a tooltip on hover or keyboard focus (Escape hides it).

const HELP_ID = (el) => el.dataset.helpId || (el.dataset.helpId = nextId('help'));

function wire(el) {
  if (el.hasAttribute('aria-describedby') || el.dataset.helpWired) return;
  el.dataset.helpWired = '1';
  const id = HELP_ID(el);
  const s = h('span', { id, class: 'sr-only' }, el.dataset.help);
  el.after(s);
  el.setAttribute('aria-describedby', id);
}

export function initHelp() {
  const tip = document.getElementById('tooltip');
  let current = null;
  const show = (el) => {
    if (!el.dataset.help || el.matches('input, select, textarea, .switch')) return;
    current = el;
    tip.textContent = el.dataset.help;
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    let top = r.bottom + 6;
    if (top + tr.height > innerHeight - 8) top = r.top - tr.height - 6;
    tip.style.top = `${Math.max(8, top)}px`;
    tip.style.left = `${Math.max(8, Math.min(r.left, innerWidth - tr.width - 8))}px`;
  };
  const hide = () => { tip.hidden = true; current = null; };
  document.addEventListener('mouseover', (e) => {
    const el = e.target.closest('[data-help]');
    if (el && !document.querySelector('.tour-layer')) show(el);
    else if (!e.target.closest('#tooltip')) hide();
  });
  document.addEventListener('focusin', (e) => {
    const el = e.target.closest('[data-help]');
    if (el && el.matches(':focus-visible')) show(el); else hide();
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && current) hide(); });
  window.addEventListener('scroll', hide, true);
  const scan = (root) => {
    if (root.nodeType !== 1) return;
    if (root.matches('[data-help]')) wire(root);
    root.querySelectorAll('[data-help]').forEach(wire);
  };
  scan(document.body);
  new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach(scan)))
    .observe(document.body, { childList: true, subtree: true });
}

// Runs fn now and every ms while the tab is visible. Returns a stop function for the view's cleanup.
export function poll(fn, ms) {
  let stopped = false;
  const tick = () => { if (!stopped && document.visibilityState === 'visible') fn().catch(() => {}); };
  const timer = setInterval(tick, ms);
  return () => { stopped = true; clearInterval(timer); };
}

// Replaces children while keeping keyboard focus on the element with the same data-key.
export function patch(container, children) {
  const key = document.activeElement && container.contains(document.activeElement)
    ? document.activeElement.dataset.key : null;
  clear(container, children);
  if (key) {
    const again = container.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (again) again.focus();
  }
}

export function titleRow(title, sub, ...actions) {
  return h('div', { class: 'title-row' },
    h('div', {}, h('h1', {}, title), sub ? (sub instanceof Node ? sub : h('p', {}, sub)) : null),
    actions.length ? h('div', { class: 'row' }, actions) : null);
}
