// Formats and rules: format list and editor, rules with reorder, precedence explainer, effective-settings preview
// (/api/formats*, /api/rules*, POST /api/formats/preview-effective).

import { ApiError, del, get, list, post, put } from '../api.js';
import { PLATFORMS, channelName } from '../labels.js';
import {
  busy, choiceGroup, clear, confirmDialog, download, h, pickFile, selectField, switchField, textField, titleRow, toast,
} from '../ui.js';
import { renderEditor, renderList } from './docs.js';

const LAYERS = {
  builtin: ['Built-in', 'neutral'], format: ['Your format', 'accent'], rule: ['Rule', 'warn'], override: ['This clip', 'pos'],
};
const TRIGGERS = { chat_spike: 'Chat spike', audio_spike: 'Loud audio', manual: 'You pressed clip' };
const EVIDENCE = { chat: 'Chat', topic: 'Stream topic', speech: 'Speech', gameplay: 'Gameplay', statement: 'A statement', reaction: 'A reaction' };
const DECISIONS = { skip: 'Do not post', hold: 'Hold for review', force_qualify: 'Always post' };
const DEST_NAMES = { youtube: 'YouTube Shorts', tiktok: 'TikTok', instagram: 'Instagram Reels' };
// settings the preview always lists; everything else shows when a format, rule or clip changed it
const KEY_SETTINGS = ['window.pre', 'window.post', 'scoring.min_score', 'detector.sensitivity', 'output.preset_id',
  'output.decision', 'output.title_template', 'output.destinations'];

// how a rule names a streamer: the platform login, or the display name of a creator feed (pipeline.channel_key)
const channelKey = (c) => (c.platform in PLATFORMS ? c.login : c.display_name || c.login);
const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 56) || 'rule';
const lines = (v) => String(v || '').split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

export async function render(main, params) {
  if (params[0]) return renderEditor(main, 'formats', params[0], 'formats');
  main.append(titleRow('Formats and rules', 'A format decides which moments become clips. Rules change that for some clips only.'));
  await renderList(main, 'formats', { listId: 'format-list',
    intro: 'Duplicate the built-in format to make your own, or import one from a file. The active format applies to channels that do not pick one.' });
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
  const reload = async () => { rules = list(await get('/rules'), 'rules'); };

  async function change(fn, note) {
    try {
      await fn();
      if (note) clear(msg, h('p', { class: 'small muted' }, note));
    } catch (e) { toast(e.message, true); }
    await reload().catch(() => {});
    draw();
  }

  function move(i, delta, focusSel) {
    const j = i + delta;
    if (j < 0 || j >= rules.length) return;
    const next = rules.map((r) => r.id);
    [next[i], next[j]] = [next[j], next[i]];
    const moved = rules[i];
    change(() => post('/rules/reorder', { ids: next }), `Moved "${moved.name}" to position ${j + 1}.`).then(() => {
      const same = panel.querySelector(`[data-key="${focusSel}-${moved.id}"]`);
      const other = panel.querySelector(`[data-key="${focusSel === 'up' ? 'down' : 'up'}-${moved.id}"]`);
      const el = same && !same.disabled ? same : other;
      if (el) el.focus();
    });
  }

  // a rules file replaces the list: existing ids are updated, new ones added, missing ones deleted, then the order
  async function importRules(next) {
    const have = new Set(rules.map((r) => r.id));
    let done = 0;
    for (const r of next) {
      if (have.has(r.id)) await put(`/rules/${encodeURIComponent(r.id)}`, r); else await post('/rules', r);
      done++;
    }
    const keep = new Set(next.map((r) => r.id));
    for (const r of rules) if (!keep.has(r.id)) await del(`/rules/${encodeURIComponent(r.id)}`);
    await post('/rules/reorder', { ids: next.map((r) => r.id) });
    return done;
  }

  let dragFrom = null;
  function draw() {
    const addBtn = h('button', { type: 'button', class: 'btn small primary', help: 'Create a new rule at the bottom of the list.' }, 'Add rule');
    addBtn.addEventListener('click', () => openEditor(null));
    const imp = h('button', { type: 'button', class: 'btn small', help: 'Replace all rules with the ones in a .json file.' }, 'Import');
    imp.addEventListener('click', async () => {
      const text = await pickFile();
      if (text === null) return;
      let next;
      try {
        const data = JSON.parse(text);
        next = Array.isArray(data) ? data : data.rules;
        if (!Array.isArray(next) || next.some((r) => !r || typeof r !== 'object' || !r.id)) throw new Error('the file needs a list of rules, each with an id');
      } catch (e) { clear(msg, h('p', { class: 'note bad' }, `Not imported: ${e.message}`)); return; }
      if (!await confirmDialog({ title: 'Replace all rules?', body: `The ${rules.length} current rules are replaced by ${next.length} from the file.`, ok: 'Replace rules', danger: true })) return;
      try {
        await importRules(next);
        clear(msg, h('p', { class: 'note info' }, 'Rules imported.'));
      } catch (e) { clear(msg, h('p', { class: 'note bad' }, `Import stopped: ${e.message}`)); }
      await reload().catch(() => {});
      draw();
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
        onchange: (e) => change(() => put(`/rules/${encodeURIComponent(r.id)}`, { ...r, enabled: e.target.checked })) });
      const edit = h('button', { type: 'button', class: 'btn small', help: 'Change what this rule checks and what it does.' }, 'Edit');
      edit.addEventListener('click', () => openEditor(r));
      const remove = h('button', { type: 'button', class: 'btn small danger', help: 'Delete this rule.' }, 'Delete');
      remove.addEventListener('click', async () => {
        if (!await confirmDialog({ title: `Delete rule "${r.name}"?`, body: 'Clips it matched go back to the format settings.', ok: 'Delete rule', danger: true })) return;
        await change(() => del(`/rules/${encodeURIComponent(r.id)}`), `Deleted "${r.name}".`);
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
        const next = rules.map((x) => x.id);
        const [m] = next.splice(dragFrom, 1);
        next.splice(i, 0, m);
        const moved = rules[dragFrom];
        dragFrom = null;
        change(() => post('/rules/reorder', { ids: next }), `Moved "${moved.name}" to position ${i + 1}.`);
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
    const r = rule || {};
    const c = r.conditions || {};
    const sc = r.scenario || {};
    const du = r.duration || {};
    const st = r.streamers || {};
    const a = r.actions || {};
    const name = textField({ label: 'Rule name', value: r.name || '', maxlength: 120, help: 'A short name so you recognise the rule in the list.' });
    const onlyFmt = selectField({ label: 'Only with format', value: r.format_id || '', help: 'Apply this rule only while this format is in effect.',
      options: [{ value: '', label: 'Any format' }, ...formats.map((f) => ({ value: f.id, label: f.name || f.id }))] });
    const platforms = choiceGroup({ type: 'checkbox', legend: 'Platforms', value: c.platforms || [], help: 'Only clips from these platforms. None ticked means any.',
      options: Object.entries(PLATFORMS).map(([v, l]) => ({ value: v, label: l })) });
    const category = textField({ label: 'Category or game matches', value: (c.categories || []).join('\n'), type: 'textarea', rows: 2, spellcheck: 'false',
      help: 'Patterns (regular expressions, any case), one per line. Only when the stream category matches one of them.' });
    const title = textField({ label: 'Stream title matches', value: c.title_regex || '', spellcheck: 'false',
      help: 'A pattern (regular expression, any case). Only when the stream title matches it.' });
    const language = textField({ label: 'Spoken languages', value: (c.languages || []).join(', '), help: 'Two-letter codes such as en, es. Empty means any.' });
    const trigger = choiceGroup({ type: 'checkbox', legend: 'What found the moment', value: sc.trigger_types || [], help: 'Only clips started this way. None ticked means any.',
      options: Object.entries(TRIGGERS).map(([v, l]) => ({ value: v, label: l })) });
    const evidence = choiceGroup({ type: 'checkbox', legend: 'Evidence found', value: sc.evidence || [], help: 'Only clips where all the ticked evidence was found.',
      options: Object.entries(EVIDENCE).map(([v, l]) => ({ value: v, label: l })) });
    const minD = textField({ label: 'Shortest clip (seconds)', type: 'number', value: du.min_s ?? '', min: 0, help: 'Only clips at least this long.' });
    const maxD = textField({ label: 'Longest clip (seconds)', type: 'number', value: du.max_s ?? '', min: 0, help: 'Only clips at most this long.' });
    const keys = [...new Set([...channels.map(channelKey), ...(st.channels || [])])].filter(Boolean);
    const streamers = choiceGroup({ type: 'checkbox', legend: 'Streamers involved', value: st.channels || [],
      help: 'Only clips that involve these streamers (a simulcast can involve several). None ticked means any.',
      options: keys.map((k) => ({ value: k, label: k })) });
    const matchAll = choiceGroup({ legend: 'Match', value: st.mode || 'any', help: 'For simulcasts with several streamers.',
      options: [{ value: 'any', label: 'Any of the ticked streamers' }, { value: 'all', label: 'All of the ticked streamers together' }] });
    const decision = selectField({ label: 'Decision', value: a.decision || '', help: 'Post, hold or skip these clips whatever the score says.',
      options: [{ value: '', label: 'Keep the format decision' }, ...Object.entries(DECISIONS).map(([v, l]) => ({ value: v, label: l }))] });
    const threshold = textField({ label: 'Minimum score', type: 'number', value: a.threshold ?? '', min: 0, step: 'any', help: 'Clips scoring below this are not posted. Empty keeps the format value.' });
    const pre = textField({ label: 'Seconds before the moment', type: 'number', value: a.window_pre ?? '', min: 0, max: 120, step: 'any', help: 'Video kept before the moment. Empty keeps the format value.' });
    const postW = textField({ label: 'Seconds after the moment', type: 'number', value: a.window_post ?? '', min: 0, max: 120, step: 'any', help: 'Video kept after the moment. Empty keeps the format value.' });
    const preset = selectField({ label: 'Use preset', value: a.preset_id || '', help: 'Switch these clips to another look.',
      options: [{ value: '', label: 'Keep the format preset' }, ...presets.map((p) => ({ value: p.id, label: p.name || p.id }))] });
    const tmpl = textField({ label: 'Title template', value: a.title_template || '', help: 'For example {title} | {channel}. Placeholders: {title} {channel} {game} {platform}. Empty keeps the format value.' });
    const destOpts = [...new Set(dests.filter((d) => !d.test).map((d) => d.provider))];
    const destGroup = choiceGroup({ type: 'checkbox', legend: 'Only post to', value: a.destinations || [],
      help: 'Limit where these clips go. None ticked keeps the channel setting.',
      options: (destOpts.length ? destOpts : Object.keys(DEST_NAMES)).map((p) => ({ value: p, label: DEST_NAMES[p] || p })) });
    const err = h('p', { class: 'error', role: 'alert' });
    const save = h('button', { type: 'submit', class: 'btn primary', help: 'Save this rule.' }, rule ? 'Save rule' : 'Add rule');
    const cancel = h('button', { type: 'button', class: 'btn ghost', help: 'Close without saving.' }, 'Cancel');
    const form = h('form', { id: 'rule-editor', class: 'panel', style: 'margin-top: 16px', novalidate: true, 'aria-labelledby': 're-title' },
      h('h3', { id: 're-title' }, rule ? `Edit "${rule.name}"` : 'New rule'), name.el, onlyFmt.el,
      h('h3', { style: 'margin-top: 16px' }, 'When'),
      platforms.el, h('div', { class: 'inline-fields' }, category.el, title.el, language.el, minD.el, maxD.el),
      h('div', { class: 'split', style: 'margin-top: 16px' }, trigger.el, evidence.el),
      keys.length ? h('div', { class: 'split', style: 'margin-top: 16px' }, streamers.el, matchAll.el) : null,
      h('h3', { style: 'margin-top: 16px' }, 'Then'),
      h('div', { class: 'inline-fields' }, decision.el, threshold.el, pre.el, postW.el, preset.el, tmpl.el),
      destGroup.el, err, h('div', { class: 'row', style: 'margin-top: 16px' }, save, cancel));
    cancel.addEventListener('click', () => clear(editorSlot));
    form.addEventListener('submit', busy(save, async (e) => {
      e.preventDefault();
      err.textContent = '';
      if (!name.input.value.trim()) { name.el.querySelector('.error').textContent = 'Give the rule a name.'; name.input.focus(); return; }
      const num = (f) => (f.input.value === '' ? null : Number(f.input.value));
      const body = { ...r, name: name.input.value.trim(), enabled: r.enabled !== false, format_id: onlyFmt.input.value || null,
        conditions: { platforms: platforms.values(), categories: lines(category.input.value).filter((x) => x), title_regex: title.input.value.trim() || null,
          languages: lines(language.input.value).map((x) => x.toLowerCase()) },
        scenario: { trigger_types: trigger.values(), evidence: evidence.values() },
        duration: { min_s: num(minD), max_s: num(maxD) },
        streamers: { ...st, channels: streamers.values(), mode: matchAll.value() || 'any' },
        actions: { ...a, decision: decision.input.value || null, threshold: num(threshold), window_pre: num(pre), window_post: num(postW),
          preset_id: preset.input.value || null, title_template: tmpl.input.value.trim() || null,
          destinations: destGroup.values().length ? destGroup.values() : null } };
      try {
        if (rule) await put(`/rules/${encodeURIComponent(rule.id)}`, body);
        else {
          const base = slug(body.name);
          let id = base;
          for (let n = 2; rules.some((x) => x.id === id); n++) id = `${base}-${n}`;
          await post('/rules', { ...body, id });
        }
        await reload();
        clear(editorSlot);
        draw();
        toast('Rule saved.');
      } catch (ex) {
        const f = ex instanceof ApiError ? ex.fields : {};
        err.textContent = Object.keys(f).length ? Object.entries(f).map(([k, m]) => `${k}: ${m}`).join('; ') : ex.message;
      }
    }));
    clear(editorSlot, form);
    name.input.focus();
  }

  draw();
}

function describeRule(r) {
  const c = r.conditions || {};
  const sc = r.scenario || {};
  const du = r.duration || {};
  const st = r.streamers || {};
  const a = r.actions || {};
  const when = [];
  if (r.format_id) when.push(`format ${r.format_id}`);
  if ((c.platforms || []).length) when.push(c.platforms.map((p) => PLATFORMS[p] || p).join(' or '));
  if ((c.categories || []).length) when.push(`category matches ${c.categories.join(' or ')}`);
  if (c.title_regex) when.push(`title matches ${c.title_regex}`);
  if ((c.languages || []).length) when.push(`language ${c.languages.join(' or ')}`);
  if ((sc.trigger_types || []).length) when.push(sc.trigger_types.map((t) => TRIGGERS[t] || t).join(' or '));
  if ((sc.evidence || []).length) when.push(`evidence ${sc.evidence.join(' and ')}`);
  if (du.min_s !== null && du.min_s !== undefined) when.push(`at least ${du.min_s} s`);
  if (du.max_s !== null && du.max_s !== undefined) when.push(`at most ${du.max_s} s`);
  if ((st.channels || []).length) when.push(`${st.mode === 'all' ? 'all of' : 'any of'} ${st.channels.join(', ')}`);
  const then = [];
  if (a.decision) then.push(DECISIONS[a.decision] || a.decision);
  if (a.threshold !== null && a.threshold !== undefined) then.push(`minimum score ${a.threshold}`);
  if (a.window_pre !== null && a.window_pre !== undefined) then.push(`${a.window_pre} s before`);
  if (a.window_post !== null && a.window_post !== undefined) then.push(`${a.window_post} s after`);
  if (a.preset_id) then.push(`preset ${a.preset_id}`);
  if (a.title_template) then.push(`title "${a.title_template}"`);
  if ((a.destinations || []).length) then.push(`post only to ${a.destinations.join(', ')}`);
  return `When ${when.join(', ') || 'any clip'}: ${then.join(', ') || 'nothing yet'}`;
}

// --- preview ---------------------------------------------------------------------------------------------

async function renderPreview(main) {
  const [channels, formats] = await Promise.all([
    get('/channels').then((d) => list(d, 'channels')).catch(() => []),
    get('/formats').then((d) => list(d, 'formats')).catch(() => [])]);
  const ch = selectField({ label: 'Channel', value: '', help: 'Use this channel\'s platform, format and streamer name.',
    options: [{ value: '', label: 'No channel' }, ...channels.map((c) => ({ value: c.id, label: channelName(c) }))] });
  const fmt = selectField({ label: 'Format', value: '', help: 'The format to start from.',
    options: [{ value: '', label: 'The channel\'s format, else the active one' }, ...formats.map((f) => ({ value: f.id, label: f.name || f.id }))] });
  const platform = selectField({ label: 'Platform', value: 'twitch', help: 'Pretend the clip comes from this platform.',
    options: Object.entries(PLATFORMS).map(([v, l]) => ({ value: v, label: l })) });
  const category = textField({ label: 'Category or game', help: 'Pretend the stream has this category.' });
  const title = textField({ label: 'Stream title', help: 'Pretend the stream has this title.' });
  const trigger = selectField({ label: 'Trigger', value: 'chat_spike', help: 'What found the moment.',
    options: Object.entries(TRIGGERS).map(([v, l]) => ({ value: v, label: l })) });
  const language = textField({ label: 'Spoken language', value: '', help: 'Two-letter code such as en. Empty means unknown.' });
  const duration = textField({ label: 'Clip length (seconds)', type: 'number', value: 30, min: 1, help: 'Pretend the clip is this long.' });
  const clipId = textField({ label: 'Clip number (optional)', type: 'number', min: 1, help: 'Include the changes you made to this clip.' });
  const all = switchField({ label: 'Show every setting', checked: false, help: 'List every value, not only the main ones and those that changed.' });
  const out = h('div', { role: 'status', 'aria-live': 'polite' });
  const run = h('button', { type: 'submit', class: 'btn primary', help: 'Work out every final setting and which layer set it.' }, 'Show effective settings');
  const form = h('form', { novalidate: true }, h('div', { class: 'inline-fields' },
    ch.el, fmt.el, platform.el, category.el, title.el, trigger.el, language.el, duration.el, clipId.el),
  h('div', { style: 'margin-top: 12px' }, all.el), h('div', { class: 'row', style: 'margin-top: 16px' }, run));
  ch.input.addEventListener('change', () => {
    const c = channels.find((x) => String(x.id) === ch.input.value);
    if (c && c.platform in PLATFORMS) platform.input.value = c.platform;
  });
  form.addEventListener('submit', busy(run, async (e) => {
    e.preventDefault();
    try {
      const c = channels.find((x) => String(x.id) === ch.input.value);
      const clip = { kind: trigger.input.value, duration_s: Number(duration.input.value) || null, language: language.input.value.trim() || null };
      if (clipId.input.value) {
        const cl = await get(`/clips/${encodeURIComponent(clipId.input.value)}`);
        clip.override = (cl.trigger && cl.trigger.override) || {};
      }
      const r = await post('/formats/preview-effective', {
        channel: c ? channelKey(c) : null, format_id: fmt.input.value || (c && c.format_id) || null, clip,
        context: { platform: platform.input.value, category: category.input.value.trim(), title: title.input.value.trim(),
          channels: c ? [channelKey(c)] : [] } });
      const rows = Object.entries(r.trail || {})
        .filter(([path, t]) => all.input.checked || t.set_by !== 'builtin' || KEY_SETTINGS.includes(path))
        .sort(([a], [b]) => a.localeCompare(b));
      const matched = (r.matched_rules || []).map((m) => m.name);
      clear(out,
        h('p', { class: 'small', style: 'margin-top: 12px' }, `Format: ${r.format_id}. `,
          matched.length ? `Matching rules: ${matched.join(', ')}.` : 'No rule matches.'),
        rows.length ? h('div', { class: 'table-wrap', style: 'margin-top: 8px; max-height: 480px; overflow: auto' }, h('table', {},
          h('caption', { class: 'sr-only' }, 'Effective settings and the layer that set each one'),
          h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Setting'), h('th', { scope: 'col' }, 'Value'), h('th', { scope: 'col' }, 'Set by'))),
          h('tbody', {}, rows.map(([path, t]) => {
            const [label, tone] = LAYERS[t.set_by] || [t.set_by, 'neutral'];
            const last = (t.layers || []).at(-1) || {};
            return h('tr', {}, h('td', {}, h('code', {}, path)), h('td', {}, h('code', { style: 'overflow-wrap: anywhere' }, JSON.stringify(t.value))),
              h('td', {}, h('span', { class: `pill ${tone}` }, label), h('span', { class: 'small muted' }, ` ${last.name || t.source || ''}`)));
          }))))
          : h('p', { class: 'muted' }, 'No settings came back.'));
    } catch (ex) { clear(out, h('p', { class: 'error' }, ex.message)); }
  }));
  main.append(h('section', { id: 'preview-tool', class: 'panel', style: 'margin-top: var(--gap)', 'aria-labelledby': 'pv-title' },
    h('h2', { id: 'pv-title' }, 'Preview effective settings'),
    h('p', { class: 'muted', style: 'margin-bottom: 12px' }, 'Describe a situation and see the final value of every setting, and which layer set it.'),
    form, out));
}
