// Formats and rules: format list and editor, rules with reorder, precedence explainer, effective-settings preview.

import { ApiError, get, list, post, put } from '../api.js';
import { PLATFORMS } from '../labels.js';
import {
  busy, choiceGroup, clear, confirmDialog, download, h, pickFile, selectField, switchField, textField, titleRow, toast,
} from '../ui.js';
import { renderEditor, renderList } from './docs.js';

const LAYERS = {
  builtin: ['Built-in', 'neutral'], format: ['Your format', 'accent'], preset: ['Your preset', 'accent'],
  rule: ['Rule', 'warn'], override: ['This clip', 'pos'],
};

export async function render(main, params) {
  if (params[0]) return renderEditor(main, 'formats', params[0], 'formats');
  main.append(titleRow('Formats and rules', 'A format decides which moments become clips. Rules change that for some clips only.'));
  await renderList(main, 'formats', { listId: 'format-list',
    intro: 'Duplicate the built-in format to make your own, or import one from a file.' });
  const lower = h('div', { class: 'grid-main', style: 'margin-top: var(--gap)' });
  main.append(lower);
  const left = h('div', { class: 'stack' });
  lower.append(left, precedence());
  await renderRules(left);
  await renderPreview(main);
}

function precedence() {
  return h('aside', { id: 'precedence', class: 'panel tinted', 'aria-labelledby': 'prec-title' },
    h('h2', { id: 'prec-title' }, 'Which setting wins'),
    h('p', { class: 'small', style: 'margin-bottom: 8px' }, 'Each layer replaces the values of the one before it, key by key.'),
    h('ol', { class: 'steps-list' },
      h('li', {}, h('strong', {}, 'Built-in format and preset. '), 'The defaults that ship with the app.'),
      h('li', {}, h('strong', {}, 'Your format or preset. '), 'Any value you changed or imported replaces the built-in one.'),
      h('li', {}, h('strong', {}, 'Rules, top to bottom. '), 'Every matching rule applies; a lower rule overrides a higher one.'),
      h('li', {}, h('strong', {}, 'Changes to one clip. '), 'What you set in the clip editor always wins.')));
}

// --- rules -----------------------------------------------------------------------------------------------

async function renderRules(container) {
  const panel = h('section', { id: 'rule-list-panel', class: 'panel', 'aria-labelledby': 'rules-title' });
  const msg = h('div', { role: 'status', 'aria-live': 'polite' });
  const editorSlot = h('div');
  container.append(panel);
  const [channels, formats, presets, dests] = await Promise.all([
    get('/channels').then((d) => list(d, 'channels')).catch(() => []),
    get('/formats').then((d) => list(d, 'formats')).catch(() => []),
    get('/presets').then((d) => list(d, 'presets')).catch(() => []),
    get('/destinations').then((d) => list(d, 'destinations')).catch(() => [])]);
  let rules = list(await get('/rules'), 'rules');

  async function saveOrder(next, note) {
    try {
      rules = list(await put('/rules', { rules: next }), 'rules');
      if (note) clear(msg, h('p', { class: 'small muted' }, note));
    } catch (e) { toast(e.message, true); }
    draw();
  }

  function move(i, delta, focusSel) {
    const j = i + delta;
    if (j < 0 || j >= rules.length) return;
    const next = [...rules];
    [next[i], next[j]] = [next[j], next[i]];
    saveOrder(next, `Moved "${next[j].name}" to position ${j + 1}.`).then(() => {
      const same = panel.querySelector(`[data-key="${focusSel}-${next[j].id}"]`);
      const other = panel.querySelector(`[data-key="${focusSel === 'up' ? 'down' : 'up'}-${next[j].id}"]`);
      const el = same && !same.disabled ? same : other;
      if (el) el.focus();
    });
  }

  let dragFrom = null;
  function draw() {
    const addBtn = h('button', { type: 'button', class: 'btn small primary', help: 'Create a new rule at the bottom of the list.' }, 'Add rule');
    addBtn.addEventListener('click', () => openEditor(null));
    const imp = h('button', { type: 'button', class: 'btn small', help: 'Replace all rules with the ones in a .json file.' }, 'Import');
    imp.addEventListener('click', async () => {
      const text = await pickFile();
      if (text === null) return;
      try {
        const data = JSON.parse(text);
        const next = Array.isArray(data) ? data : data.rules;
        if (!await confirmDialog({ title: 'Replace all rules?', body: `The ${rules.length} current rules are replaced by ${(next || []).length} from the file.`, ok: 'Replace rules', danger: true })) return;
        await saveOrder(next, 'Rules imported.');
      } catch (e) { clear(msg, h('p', { class: 'note bad' }, `Not imported: ${e.message}`)); }
    });
    const exp = h('button', { type: 'button', class: 'btn small', help: 'Download all rules as a .json file.' }, 'Export');
    exp.addEventListener('click', () => download('rules.json', `${JSON.stringify({ rules }, null, 2)}\n`));
    const items = rules.map((r, i) => {
      const up = h('button', { type: 'button', class: 'btn small ghost', dataset: { key: `up-${r.id}` }, disabled: i === 0,
        'aria-label': `Move ${r.name} up`, help: 'Move this rule up. Lower rules win over higher ones.' }, 'Up');
      const down = h('button', { type: 'button', class: 'btn small ghost', dataset: { key: `down-${r.id}` }, disabled: i === rules.length - 1,
        'aria-label': `Move ${r.name} down`, help: 'Move this rule down. Lower rules win over higher ones.' }, 'Down');
      up.addEventListener('click', () => move(i, -1, 'up'));
      down.addEventListener('click', () => move(i, 1, 'down'));
      const sw = h('input', { type: 'checkbox', role: 'switch', class: 'switch', checked: r.enabled !== false,
        'aria-label': `${r.name} is on`, help: 'Turn this rule on or off without deleting it.',
        onchange: (e) => saveOrder(rules.map((x) => (x.id === r.id ? { ...x, enabled: e.target.checked } : x))) });
      const edit = h('button', { type: 'button', class: 'btn small', help: 'Change what this rule checks and what it does.' }, 'Edit');
      edit.addEventListener('click', () => openEditor(r));
      const remove = h('button', { type: 'button', class: 'btn small danger', help: 'Delete this rule.' }, 'Delete');
      remove.addEventListener('click', async () => {
        if (!await confirmDialog({ title: `Delete rule "${r.name}"?`, body: 'Clips it matched go back to the format settings.', ok: 'Delete rule', danger: true })) return;
        await saveOrder(rules.filter((x) => x.id !== r.id), `Deleted "${r.name}".`);
      });
      const li = h('li', { draggable: 'true', dataset: { index: i } },
        h('span', { class: 'handle muted', 'aria-hidden': 'true', title: 'Drag to reorder' }, '::'),
        h('div', { class: 'grow' }, h('div', { style: 'font-weight:600' }, r.name), h('div', { class: 'small muted' }, describeRule(r))),
        h('div', { class: 'row' }, sw, up, down, edit, remove));
      li.addEventListener('dragstart', (e) => { dragFrom = i; li.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; });
      li.addEventListener('dragend', () => li.classList.remove('dragging'));
      li.addEventListener('dragover', (e) => { e.preventDefault(); li.classList.add('drop-target'); });
      li.addEventListener('dragleave', () => li.classList.remove('drop-target'));
      li.addEventListener('drop', (e) => {
        e.preventDefault();
        li.classList.remove('drop-target');
        if (dragFrom === null || dragFrom === i) return;
        const next = [...rules];
        const [m] = next.splice(dragFrom, 1);
        next.splice(i, 0, m);
        dragFrom = null;
        saveOrder(next, `Moved "${m.name}" to position ${i + 1}.`);
      });
      return li;
    });
    clear(panel,
      h('header', {}, h('h2', { id: 'rules-title' }, 'Rules'), h('div', { class: 'end row' }, imp, exp, addBtn)),
      h('p', { class: 'muted', style: 'margin-bottom: 8px' }, 'Checked from top to bottom for every clip. Drag a rule, or use Up and Down, to change the order.'),
      msg,
      rules.length ? h('ul', { class: 'rows rule-list' }, items) : h('p', { class: 'muted' }, 'No rules yet. Every clip uses its channel\'s format as it is.'),
      editorSlot);
  }

  function openEditor(rule) {
    const r = rule || { name: '', enabled: true, conditions: {}, actions: {} };
    const c = r.conditions || {};
    const a = r.actions || {};
    const name = textField({ label: 'Rule name', value: r.name, help: 'A short name so you recognise the rule in the list.' });
    const platform = selectField({ label: 'Platform', value: c.platform || '', help: 'Only clips from this platform.',
      options: [{ value: '', label: 'Any platform' }, ...Object.entries(PLATFORMS).map(([v, l]) => ({ value: v, label: l }))] });
    const category = textField({ label: 'Category or game contains', value: c.category || '', help: 'Only when the stream category contains this text.' });
    const title = textField({ label: 'Stream title contains', value: c.title_contains || '', help: 'Only when the stream title contains this text.' });
    const language = textField({ label: 'Spoken language', value: c.language || '', help: 'Two-letter code such as en or es. Empty means any.' });
    const trigger = selectField({ label: 'What found the moment', value: c.trigger || '', help: 'Only clips started by this kind of trigger.',
      options: [{ value: '', label: 'Anything' }, { value: 'chat', label: 'Chat spike' }, { value: 'audio', label: 'Loud audio' }, { value: 'manual', label: 'You pressed clip' }] });
    const evidence = textField({ label: 'Evidence includes', value: c.evidence || '', help: 'Only when this evidence was found, for example title, category or speech.' });
    const minD = textField({ label: 'Shortest clip (seconds)', type: 'number', value: c.min_duration_s ?? '', min: 0, help: 'Only clips at least this long.' });
    const maxD = textField({ label: 'Longest clip (seconds)', type: 'number', value: c.max_duration_s ?? '', min: 0, help: 'Only clips at most this long.' });
    const streamers = choiceGroup({ type: 'checkbox', legend: 'Streamers involved', value: (c.channels || []).map(String),
      help: 'Only clips that involve these channels. None ticked means any channel.',
      options: channels.map((ch) => ({ value: String(ch.id), label: ch.login || ch.url })) });
    const matchAll = choiceGroup({ legend: 'Match', value: c.channels_match || 'any', help: 'For simulcasts with several streamers.',
      options: [{ value: 'any', label: 'Any of the ticked streamers' }, { value: 'all', label: 'All of the ticked streamers together' }] });
    const fmt = selectField({ label: 'Use format', value: a.format_id || '', help: 'Switch these clips to another format.',
      options: [{ value: '', label: 'Keep the channel format' }, ...formats.map((f) => ({ value: f.id, label: f.name || f.id }))] });
    const preset = selectField({ label: 'Use preset', value: a.preset_id || '', help: 'Switch these clips to another look.',
      options: [{ value: '', label: 'Keep the default preset' }, ...presets.map((p) => ({ value: p.id, label: p.name || p.id }))] });
    const hold = switchField({ label: 'Hold for review', checked: Boolean(a.hold), help: 'These clips wait in the review queue even in automatic mode.' });
    const destOpts = [...new Set(dests.map((d) => d.provider))];
    const destGroup = choiceGroup({ type: 'checkbox', legend: 'Only post to', value: a.destinations || [],
      help: 'Limit where these clips go. None ticked keeps the channel setting.',
      options: destOpts.map((p) => ({ value: p, label: (dests.find((d) => d.provider === p) || {}).label || p })) });
    const sets = textField({ label: 'Change settings', type: 'textarea', help: 'One per line as path = value, for example window.post = 15. Values are JSON.',
      value: Object.entries(a.set || {}).map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join('\n'), spellcheck: 'false' });
    const err = h('p', { class: 'error', role: 'alert' });
    const save = h('button', { type: 'submit', class: 'btn primary', help: 'Save this rule.' }, rule ? 'Save rule' : 'Add rule');
    const cancel = h('button', { type: 'button', class: 'btn ghost', help: 'Close without saving.' }, 'Cancel');
    const form = h('form', { id: 'rule-editor', class: 'panel', style: 'margin-top: 16px', novalidate: true, 'aria-labelledby': 're-title' },
      h('h3', { id: 're-title' }, rule ? `Edit "${rule.name}"` : 'New rule'), name.el,
      h('h3', { style: 'margin-top: 16px' }, 'When'),
      h('div', { class: 'inline-fields' }, platform.el, category.el, title.el, language.el, trigger.el, evidence.el, minD.el, maxD.el),
      channels.length ? h('div', { class: 'split', style: 'margin-top: 16px' }, streamers.el, matchAll.el) : null,
      h('h3', { style: 'margin-top: 16px' }, 'Then'),
      h('div', { class: 'inline-fields' }, fmt.el, preset.el), h('div', { style: 'margin-top: 16px' }, hold.el),
      destOpts.length ? destGroup.el : null, sets.el, err, h('div', { class: 'row', style: 'margin-top: 16px' }, save, cancel));
    cancel.addEventListener('click', () => clear(editorSlot));
    form.addEventListener('submit', busy(save, async (e) => {
      e.preventDefault();
      err.textContent = '';
      if (!name.input.value.trim()) { name.el.querySelector('.error').textContent = 'Give the rule a name.'; name.input.focus(); return; }
      const set = {};
      for (const line of sets.input.value.split('\n').map((l) => l.trim()).filter(Boolean)) {
        const m = line.match(/^([\w.]+)\s*=\s*(.+)$/);
        if (!m) { err.textContent = `This line needs the form path = value: ${line}`; sets.input.focus(); return; }
        try { set[m[1]] = JSON.parse(m[2]); } catch { set[m[1]] = m[2]; }
      }
      const num = (f) => (f.input.value === '' ? undefined : Number(f.input.value));
      const strip = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== '' && v !== undefined && v !== false
        && !(Array.isArray(v) && !v.length) && !(v && typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length)));
      const body = { ...(rule || {}), name: name.input.value.trim(), enabled: r.enabled !== false,
        conditions: strip({ platform: platform.input.value, category: category.input.value.trim(), title_contains: title.input.value.trim(),
          language: language.input.value.trim(), trigger: trigger.input.value, evidence: evidence.input.value.trim(),
          min_duration_s: num(minD), max_duration_s: num(maxD), channels: streamers.values().map(Number),
          channels_match: streamers.values().length > 1 ? matchAll.value() : undefined }),
        actions: strip({ format_id: fmt.input.value, preset_id: preset.input.value, hold: hold.input.checked,
          destinations: destGroup.values(), set }) };
      const next = rule ? rules.map((x) => (x.id === rule.id ? body : x)) : [...rules, body];
      try {
        rules = list(await put('/rules', { rules: next }), 'rules');
        clear(editorSlot);
        draw();
        toast('Rule saved.');
      } catch (ex) { err.textContent = ex instanceof ApiError ? ex.message : String(ex); }
    }));
    clear(editorSlot, form);
    name.input.focus();
  }

  draw();
}

function describeRule(r) {
  const c = r.conditions || {};
  const a = r.actions || {};
  const when = [];
  if (c.platform) when.push(PLATFORMS[c.platform] || c.platform);
  if (c.category) when.push(`category has "${c.category}"`);
  if (c.title_contains) when.push(`title has "${c.title_contains}"`);
  if (c.language) when.push(`language ${c.language}`);
  if (c.trigger) when.push(`${c.trigger} trigger`);
  if (c.min_duration_s) when.push(`at least ${c.min_duration_s} s`);
  if (c.max_duration_s) when.push(`at most ${c.max_duration_s} s`);
  if (c.channels && c.channels.length) when.push(`${c.channels.length} streamer${c.channels.length > 1 ? 's' : ''}`);
  const then = [];
  if (a.format_id) then.push(`format ${a.format_id}`);
  if (a.preset_id) then.push(`preset ${a.preset_id}`);
  if (a.hold) then.push('hold for review');
  if (a.destinations && a.destinations.length) then.push(`post only to ${a.destinations.join(', ')}`);
  for (const [k, v] of Object.entries(a.set || {})) then.push(`${k} = ${JSON.stringify(v)}`);
  return `When ${when.join(', ') || 'any clip'}: ${then.join(', ') || 'nothing yet'}`;
}

// --- preview ---------------------------------------------------------------------------------------------

async function renderPreview(main) {
  const [channels, formats] = await Promise.all([
    get('/channels').then((d) => list(d, 'channels')).catch(() => []),
    get('/formats').then((d) => list(d, 'formats')).catch(() => [])]);
  const ch = selectField({ label: 'Channel', value: '', help: 'Use this channel\'s format and settings.',
    options: [{ value: '', label: 'No channel' }, ...channels.map((c) => ({ value: c.id, label: c.login || c.url }))] });
  const fmt = selectField({ label: 'Format', value: (formats[0] || {}).id, help: 'The format to start from.',
    options: formats.map((f) => ({ value: f.id, label: f.name || f.id })) });
  const platform = selectField({ label: 'Platform', value: 'twitch', help: 'Pretend the clip comes from this platform.',
    options: Object.entries(PLATFORMS).map(([v, l]) => ({ value: v, label: l })) });
  const category = textField({ label: 'Category or game', help: 'Pretend the stream has this category.' });
  const title = textField({ label: 'Stream title', help: 'Pretend the stream has this title.' });
  const trigger = selectField({ label: 'Trigger', value: 'chat', help: 'What found the moment.',
    options: [{ value: 'chat', label: 'Chat spike' }, { value: 'audio', label: 'Loud audio' }, { value: 'manual', label: 'You pressed clip' }] });
  const duration = textField({ label: 'Clip length (seconds)', type: 'number', value: 30, min: 1, help: 'Pretend the clip is this long.' });
  const clipId = textField({ label: 'Clip number (optional)', type: 'number', min: 1, help: 'Include the changes you made to this clip.' });
  const out = h('div', { role: 'status', 'aria-live': 'polite' });
  const run = h('button', { type: 'submit', class: 'btn primary', help: 'Work out every final setting and which layer set it.' }, 'Show effective settings');
  const form = h('form', { novalidate: true }, h('div', { class: 'inline-fields' },
    ch.el, fmt.el, platform.el, category.el, title.el, trigger.el, duration.el, clipId.el), h('div', { class: 'row', style: 'margin-top: 16px' }, run));
  form.addEventListener('submit', busy(run, async (e) => {
    e.preventDefault();
    try {
      const r = await post('/formats/preview-effective', {
        channel_id: ch.input.value ? Number(ch.input.value) : null, format_id: fmt.input.value || null,
        clip_id: clipId.input.value ? Number(clipId.input.value) : null,
        context: { platform: platform.input.value, category: category.input.value.trim(), title: title.input.value.trim() },
        scenario: { trigger: trigger.input.value }, duration_s: Number(duration.input.value) || null });
      const rows = r.settings || [];
      clear(out, rows.length ? h('div', { class: 'table-wrap', style: 'margin-top: 16px; max-height: 480px; overflow: auto' }, h('table', {},
        h('caption', { class: 'sr-only' }, 'Effective settings and the layer that set each one'),
        h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Setting'), h('th', { scope: 'col' }, 'Value'), h('th', { scope: 'col' }, 'Set by'))),
        h('tbody', {}, rows.map((s) => {
          const [label, tone] = LAYERS[s.layer] || [s.layer, 'neutral'];
          return h('tr', {}, h('td', {}, h('code', {}, s.path)), h('td', {}, h('code', {}, JSON.stringify(s.value))),
            h('td', {}, h('span', { class: `pill ${tone}` }, label), s.source ? h('span', { class: 'small muted' }, ` ${s.source}`) : null));
        }))))
        : h('p', { class: 'muted' }, 'No settings came back.'));
    } catch (ex) { clear(out, h('p', { class: 'error' }, ex.message)); }
  }));
  main.append(h('section', { id: 'preview-tool', class: 'panel', style: 'margin-top: var(--gap)', 'aria-labelledby': 'pv-title' },
    h('h2', { id: 'pv-title' }, 'Preview effective settings'),
    h('p', { class: 'muted', style: 'margin-bottom: 12px' }, 'Describe a situation and see the final value of every setting, and which layer set it.'),
    form, out));
}
