// Settings: the setup answers grouped by topic, plus storage cleanup, browser notifications, posting mode,
// tutorial replay, setup re-run and version info.

import { ApiError, get, post, put } from '../api.js';
import { checkValue, effective, isVisible, questionField } from '../questions.js';
import { startTour } from '../tour.js';
import { busy, choiceGroup, clear, confirmDialog, fmtBytes, h, titleRow, toast } from '../ui.js';

const SECTIONS = [
  ['settings-storage', 'Storage', 'Where clips are kept and what is cleaned up.',
    ['storage.dir', 'storage.cap_gb', 'storage.retention_days', 'storage.clean_first', 'storage.cleanup_rules']],
  ['settings-processing', 'Processing', 'How much of this computer the app may use.',
    ['processing.asr_device', 'processing.asr_model', 'processing.asr_compute_type', 'processing.max_concurrent_recordings']],
  ['settings-notify', 'Notifications and startup', 'How the app tells you about new clips and problems.',
    ['notify.method', 'notify.webhook_url', 'notify.webhook_format', 'service.start_at_login']],
];

export async function render(main) {
  const [qd, st, health] = await Promise.all([get('/setup/questions'), get('/setup/state'), get('/health').catch(() => null)]);
  const questions = qd.questions || [];
  const byId = Object.fromEntries(questions.map((q) => [q.id, q]));
  const saved = { ...st.answers };
  const answers = effective(questions, saved);
  main.append(titleRow('Settings', 'Every answer from setup, grouped by topic. Changes apply right away.'));

  for (const [id, title, intro, ids] of SECTIONS) {
    const qs = ids.map((x) => byId[x]).filter(Boolean);
    const fields = qs.map((q) => ({ q, f: questionField(q, answers[q.id]) }));
    const extra = id === 'settings-storage' ? await storageBlock() : id === 'settings-notify' ? browserNotifications() : null;
    const sync = () => {
      const cur = { ...answers };
      for (const { q, f } of fields) if (!f.el.hidden) cur[q.id] = f.read();
      for (const { q, f } of fields) f.el.hidden = !isVisible(q, cur);
    };
    const save = h('button', { type: 'submit', class: 'btn primary', help: `Save the ${title.toLowerCase()} settings.` }, 'Save');
    const out = h('p', { class: 'small', role: 'status', 'aria-live': 'polite' });
    const form = h('form', { class: 'panel', novalidate: true, id, 'aria-labelledby': `${id}-title` },
      h('h2', { id: `${id}-title` }, title), h('p', { class: 'muted', style: 'margin-bottom: 12px' }, intro),
      extra, h('div', { class: 'stack' }, fields.map(({ f }) => f.el)),
      id === 'settings-notify' ? h('p', { class: 'small muted', style: 'margin-top: 12px' },
        'Start at login adds a small entry to your system startup list; turn it off here to remove it. '
        + 'The app keeps recording in the background while it runs.') : null,
      h('div', { class: 'row', style: 'margin-top: 16px' }, save, out));
    form.addEventListener('change', sync);
    form.addEventListener('submit', busy(save, async (e) => {
      e.preventDefault();
      out.textContent = '';
      const changed = {};
      let bad = false;
      for (const { q, f } of fields) {
        if (f.el.hidden) continue;
        const v = f.read();
        const err = checkValue(q, v);
        f.setError(err);
        if (err) { if (!bad) f.focus(); bad = true; continue; }
        if (JSON.stringify(v) !== JSON.stringify(q.id in saved ? saved[q.id] : undefined)) changed[q.id] = v;
      }
      if (bad) return;
      if (!Object.keys(changed).length) { out.textContent = 'Nothing changed.'; return; }
      try {
        await put('/setup/answers', { answers: changed });
        Object.assign(saved, changed);
        Object.assign(answers, changed);
        // setup/complete applies side effects such as start at login
        if ('service.start_at_login' in changed) await post('/setup/complete').catch(() => {});
        out.textContent = 'Saved.';
        toast(`${title} saved.`);
      } catch (ex) {
        const errs = ex instanceof ApiError ? ex.fields : {};
        for (const { q, f } of fields) if (errs[q.id]) f.setError(errs[q.id]);
        out.textContent = Object.keys(errs).length ? '' : ex.message;
      }
    }));
    main.append(form);
    sync();
  }

  main.append(await postingBlock());
  main.append(helpBlock(health));
}

async function storageBlock() {
  let s;
  try { s = await get('/storage'); } catch { return h('p', { class: 'muted' }, 'Storage use is not available right now.'); }
  const out = h('div', { role: 'status', 'aria-live': 'polite' });
  const pct = s.cap_bytes ? Math.min(100, Math.round((s.usage_bytes / s.cap_bytes) * 100)) : 0;
  const preview = h('button', { type: 'button', class: 'btn small', help: 'Show what a cleanup would delete, without deleting anything.' }, 'Preview cleanup');
  const run = h('button', { type: 'button', class: 'btn small danger', help: 'Delete what the cleanup rules allow now. Clips waiting to be posted are kept.' }, 'Clean up now');
  const show = (r) => clear(out, h('p', { class: 'small', style: 'margin-top: 8px' },
    `${r.dry_run ? 'Would free' : 'Freed'} ${fmtBytes(r.freed_bytes || 0)} (${(r.actions || []).length} item${(r.actions || []).length === 1 ? '' : 's'}).`));
  preview.addEventListener('click', busy(preview, async () => {
    try { show(await post('/storage/cleanup', { dry_run: true })); } catch (e) { clear(out, h('p', { class: 'error' }, e.message)); }
  }));
  run.addEventListener('click', busy(run, async () => {
    if (!await confirmDialog({ title: 'Clean up now?', body: 'Posted clips and recordings past your rules are deleted. Unposted clips are kept.', ok: 'Clean up', danger: true })) return;
    try { show(await post('/storage/cleanup', { dry_run: false })); } catch (e) { clear(out, h('p', { class: 'error' }, e.message)); }
  }));
  return h('div', { class: 'stack', style: 'margin-bottom: 16px' },
    h('dl', { class: 'kv' },
      h('dt', {}, 'Folder'), h('dd', {}, s.dir),
      h('dt', {}, 'Used'), h('dd', {}, `${fmtBytes(s.usage_bytes)} of ${fmtBytes(s.cap_bytes)} (${pct}%)`),
      h('dt', {}, 'Free on disk'), h('dd', {}, fmtBytes(s.free_bytes)),
      h('dt', {}, 'Kept because not posted yet'), h('dd', {}, fmtBytes(s.protected_bytes))),
    h('div', { class: 'progress', role: 'img', 'aria-label': `Storage cap ${pct}% used` },
      h('span', { style: `transform: scaleX(${pct / 100})` })),
    s.health && s.health.state !== 'healthy' ? h('p', { class: 'note' }, (s.health.reasons || []).join(' ') || 'Storage needs attention.') : null,
    h('div', { class: 'row' }, preview, run), out);
}

function browserNotifications() {
  const status = h('span', { class: 'small', role: 'status' });
  const supported = 'Notification' in window;
  const btn = h('button', { type: 'button', class: 'btn small', disabled: !supported,
    help: supported ? 'Ask this browser to show notifications from Live Clipper.' : 'This browser cannot show notifications.' }, 'Allow browser notifications');
  const update = () => {
    status.textContent = !supported ? 'This browser does not support notifications.'
      : Notification.permission === 'granted' ? 'Allowed in this browser.'
        : Notification.permission === 'denied' ? 'Blocked. Allow them in the browser\'s site settings.' : 'Not allowed yet.';
    btn.hidden = supported && Notification.permission === 'granted';
  };
  btn.addEventListener('click', async () => { await Notification.requestPermission(); update(); });
  update();
  return h('div', { class: 'row', style: 'margin-bottom: 16px' }, btn, status);
}

async function postingBlock() {
  const box = h('section', { id: 'settings-posting', class: 'panel', 'aria-labelledby': 'sp-title' }, h('h2', { id: 'sp-title' }, 'Posting'));
  try {
    const m = await get('/publish/mode');
    const g = choiceGroup({ legend: 'Posting mode', value: m.mode, options: [
      { value: 'auto', label: 'Automatic', hint: 'Finished clips go to every connected, turned-on destination.' },
      { value: 'manual', label: 'Manual', hint: 'Every clip waits in the review queue until you press Post.' }] });
    g.inputs.forEach((i) => i.addEventListener('change', async () => {
      try { await put('/publish/mode', { mode: i.value }); toast('Posting mode saved.'); } catch (e) { toast(e.message, true); }
    }));
    box.append(g.el, h('p', { class: 'small', style: 'margin-top: 8px' }, 'Accounts are set up on ',
      h('a', { href: '#/destinations', help: 'Open Destinations to connect accounts.' }, 'Destinations'), '.'));
  } catch {
    box.append(h('p', { class: 'muted' }, 'Posting is not available right now.'));
  }
  return box;
}

function helpBlock(health) {
  const replay = h('button', { type: 'button', class: 'btn', help: 'Show the guided tour of every screen again.' }, 'Replay tutorial');
  replay.addEventListener('click', () => startTour());
  return h('section', { id: 'settings-help', class: 'panel', 'aria-labelledby': 'sh-title' },
    h('h2', { id: 'sh-title' }, 'Help and about'),
    h('div', { class: 'row', style: 'margin: 12px 0' }, replay,
      h('a', { class: 'btn', href: '#/setup', help: 'Go through the setup questions again with your current answers filled in.' }, 'Run setup again')),
    h('dl', { class: 'kv' },
      h('dt', {}, 'Version'), h('dd', {}, health ? health.version : '-'),
      h('dt', {}, 'Your data'), h('dd', {}, 'Kept in the app\'s data folder, outside the program, so updates keep your settings, clips and tutorial progress.')),
    h('p', { style: 'margin-top: 12px' }, 'To update, follow the steps for how you installed it in the ',
      h('a', { href: '#/guide/updating', help: 'Open the Updating page of the guide.' }, 'Updating guide'), '.'));
}
