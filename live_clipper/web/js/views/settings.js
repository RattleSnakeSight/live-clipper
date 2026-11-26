// Settings: the setup answers grouped by topic, plus storage cleanup, browser notifications, posting mode,
// tutorial replay, setup re-run and version info.

import { ApiError, api, get, post, put } from '../api.js';
import { checkValue, effective, isVisible, questionField } from '../questions.js';
import { startTour } from '../tour.js';
import { busy, choiceGroup, clear, confirmDialog, fmtBytes, h, selectField, titleRow, toast } from '../ui.js';
import { agentOverview, agentPanel } from '../agent.js';
import { mountWheel } from '../accent.js';
import { THEMES, currentTheme, saveTheme } from '../theme.js';

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
  main.append(titleRow('Settings', 'Appearance applies immediately. Use Save for Storage, Processing, and Notifications and startup.'));
  main.append(appearanceBlock());

  const dirty = new Set();
  for (const [id, title, intro, ids] of SECTIONS) {
    const qs = ids.map((x) => byId[x]).filter(Boolean);
    const fields = qs.map((q) => ({ q, f: questionField(q, answers[q.id]) }));
    const extra = id === 'settings-storage' ? h('div', {}, await storageBlock()) : id === 'settings-notify' ? browserNotifications() : null;
    const sync = () => {
      const cur = { ...answers };
      for (const { q, f } of fields) if (!f.el.hidden) cur[q.id] = f.read();
      for (const { q, f } of fields) f.el.hidden = !isVisible(q, cur);
    };
    const save = h('button', { type: 'submit', class: 'btn primary', help: `Save the ${title.toLowerCase()} settings.` }, 'Save');
    let retryLogin = id === 'settings-notify' && st.login_verified === false && st.completed;
    const out = h('p', { class: 'small', role: 'status', 'aria-live': 'polite' },
      retryLogin ? 'The saved login preference could not be verified. Choose the setting you want and press Save to retry.' : '');
    const form = h('form', { class: 'panel narrow', novalidate: true, id, 'aria-labelledby': `${id}-title` },
      h('h2', { id: `${id}-title` }, title), h('p', { class: 'muted', style: 'margin-bottom: 12px' }, intro),
      extra, h('div', { class: 'settings-list' }, fields.map(({ f }) => f.el)),
      id === 'settings-notify' ? h('p', { class: 'small muted', style: 'margin-top: 12px' },
        'Start at login adds a small entry to your system startup list; turn it off here to remove it. '
        + 'The app keeps recording in the background while it runs.') : null,
      h('div', { class: 'row', style: 'margin-top: 16px' }, save, out));
    const trackChanges = () => {
      sync();
      const edited = fields.some(({ q, f }) => !f.el.hidden && JSON.stringify(f.read()) !== JSON.stringify(answers[q.id]));
      if (edited) dirty.add(id); else dirty.delete(id);
      out.textContent = edited ? 'Unsaved changes.' : '';
    };
    form.addEventListener('input', trackChanges);
    form.addEventListener('change', trackChanges);
    const inputs = form.querySelector('.settings-list');
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
        if (JSON.stringify(v) !== JSON.stringify(answers[q.id]) || (q.id === 'service.start_at_login' && retryLogin)) changed[q.id] = v;
      }
      if (bad) return;
      if (!Object.keys(changed).length) { out.textContent = 'Nothing changed.'; return; }
      inputs.inert = true;
      try {
        const result = await put('/setup/answers', { answers: changed });
        for (const { q, f } of fields) {
          if (!(q.id in changed)) continue;
          const value = result.answers[q.id];
          answers[q.id] = value;
          if (q.type === 'path' || q.type === 'url') f.el.querySelector('input').value = value;
        }
        retryLogin = false;
        dirty.delete(id);
        if (id === 'settings-storage') extra.replaceChildren(await storageBlock());
        out.textContent = 'storage.dir' in changed ? 'Saved. Restart Live Clipper to use the new folder.' : 'Saved.';
        toast(`${title} saved.`);
      } catch (ex) {
        const errs = ex instanceof ApiError ? ex.fields : {};
        if (errs['service.start_at_login']) retryLogin = true;
        for (const { q, f } of fields) if (errs[q.id]) f.setError(errs[q.id]);
        out.textContent = Object.keys(errs).length ? '' : ex.message;
      } finally { inputs.inert = false; }
    }));
    main.append(form);
    sync();
  }

  main.append(h('section', { id: 'settings-posting', class: 'panel narrow' },
    h('h2', {}, 'Posting'), h('a', { href: '#/destinations' }, 'Manage posting mode and accounts on Destinations')));
  main.append(await agentBlock());
  main.append(helpBlock(health));
  const headings = [...main.querySelectorAll('form[id] > h2, section[id] > h2')];
  const jump = selectField({ label: 'Go to section', help: 'Jump to a group of settings.',
    options: [{ value: '', label: 'Choose a section' }, ...headings.map((heading) => ({
      value: heading.parentElement.id, label: heading.textContent,
    }))] });
  jump.input.addEventListener('change', () => {
    const heading = headings.find((item) => item.parentElement.id === jump.input.value);
    if (heading) {
      heading.tabIndex = -1;
      heading.focus();
      heading.scrollIntoView({ block: 'center' });
    }
    jump.input.value = '';
  });
  main.querySelector('#settings-appearance').before(jump.el);
  const warn = (e) => {
    if (!dirty.size) return;
    e.preventDefault();
    e.returnValue = '';
  };
  window.addEventListener('beforeunload', warn);
  const cleanup = () => window.removeEventListener('beforeunload', warn);
  cleanup.canLeave = () => !dirty.size || window.confirm('Discard unsaved Settings changes?');
  return cleanup;
}

async function storageBlock() {
  let s;
  try { s = await get('/storage'); } catch { return h('p', { class: 'muted' }, 'Storage use is not available right now.'); }
  const out = h('div', { role: 'status', 'aria-live': 'polite', 'aria-label': 'Cleanup result' });
  // the figures, drawn again after a cleanup so they show what is on the disk now
  const facts = h('div', { style: 'display: contents' });
  const draw = (st) => {
    const pct = st.cap_bytes ? Math.min(100, Math.round((st.usage_bytes / st.cap_bytes) * 100)) : 0;
    facts.replaceChildren(
      h('dl', { class: 'kv' },
        h('dt', {}, 'Active folder'), h('dd', {}, st.dir),
        st.configured_dir && st.configured_dir !== st.dir
          ? [h('dt', {}, 'After restart'), h('dd', {}, st.configured_dir)] : null,
        h('dt', {}, 'Used'), h('dd', {}, `${fmtBytes(st.usage_bytes)} of ${fmtBytes(st.cap_bytes)} (${pct}%)`),
        h('dt', {}, 'Free on disk'), h('dd', {}, fmtBytes(st.free_bytes)),
        h('dt', {}, 'Kept because not posted yet'), h('dd', {}, fmtBytes(st.protected_bytes))),
      h('div', { class: 'progress', role: 'img', 'aria-label': `Storage cap ${pct}% used` },
        h('span', { style: `transform: scaleX(${pct / 100})` })),
      st.health && st.health.state !== 'healthy' ? h('p', { class: 'note' }, (st.health.reasons || []).join(' ') || 'Storage needs attention.') : null);
  };
  const refresh = async () => { try { draw(await get('/storage')); } catch { /* keep the last figures */ } };
  draw(s);
  const preview = h('button', { type: 'button', class: 'btn small', help: 'Show which clips and how many bytes a cleanup would delete now, without deleting anything.' }, 'Preview cleanup');
  const run = h('button', { type: 'button', class: 'btn small danger', help: 'Delete from this computer, now, every clip that is finished with: fully posted clips and clips you rejected, plus old trash and temporary files. Clips that are waiting, approved, being posted, shared or not decided are kept.' }, 'Clean up now');
  // what a cleanup removes, by category, and what stays: the same words in the preview, the confirmation and the result
  const lines = (r) => {
    const s = r.summary || {};
    const part = (k, label) => (s[k] && s[k].items ? `${s[k].items} ${label} (${fmtBytes(s[k].bytes)})` : null);
    const parts = [part('posted', `fully posted clip${s.posted && s.posted.items === 1 ? '' : 's'}`),
      part('rejected', `rejected clip${s.rejected && s.rejected.items === 1 ? '' : 's'}`),
      part('other', `other item${s.other && s.other.items === 1 ? '' : 's'} (old trash, temporary files, clips past your keep time)`)].filter(Boolean);
    return { parts, kept: `Kept: ${fmtBytes(s.kept_bytes || 0)} of clips that are waiting, approved, being posted, shared or not decided yet.` };
  };
  const items = (n) => `${n} item${n === 1 ? '' : 's'}`;
  // after a run: what was left of what you confirmed and why, what failed, and what was new and not touched
  const notes = (r) => {
    const res = r.results || [];
    const kept = res.filter((x) => x.outcome === 'kept');
    const failed = res.filter((x) => x.outcome === 'failed');
    const fresh = (r.not_confirmed && r.not_confirmed.items) || 0;
    return [
      kept.length ? `${items(kept.length)} you confirmed ${kept.length === 1 ? 'was' : 'were'} left in place: ${[...new Set(kept.map((x) => x.why))].join('; ')}.` : null,
      failed.length ? `${items(failed.length)} could not be deleted completely (files may be in use); try again later.` : null,
      fresh ? `${items(fresh)} that appeared after your preview ${fresh === 1 ? 'was' : 'were'} not part of what you confirmed and ${fresh === 1 ? 'was' : 'were'} left; preview again to see ${fresh === 1 ? 'it' : 'them'}.` : null,
    ].filter(Boolean);
  };
  const show = (r) => {
    const { parts, kept } = lines(r);
    const total = fmtBytes(r.dry_run ? (r.would_free_bytes || 0) : (r.freed_bytes || 0));
    const nothing = r.dry_run ? ' (nothing to clean up)' : ' (nothing was deleted)';
    clear(out, h('p', { class: 'small', style: 'margin-top: 8px' },
      [`${r.dry_run ? 'Would free' : 'Freed'} ${total}${parts.length ? `: ${parts.join(', ')}` : nothing}.`,
        ...(r.dry_run ? [] : notes(r)), kept].join(' ')));
  };
  const fail = (e) => clear(out, h('p', { class: 'error' }, e.message));
  preview.addEventListener('click', busy(preview, async () => {
    try { show(await post('/storage/cleanup', { dry_run: true })); } catch (e) { fail(e); }
  }));
  run.addEventListener('click', busy(run, async () => {
    let plan;
    try { plan = await post('/storage/cleanup', { dry_run: true }); } catch (e) { fail(e); return; }
    const { parts, kept } = lines(plan);
    if (!parts.length) { show(plan); return; }
    // the confirmation names this preview, so only what it lists can be deleted; never run without it
    if (!plan.plan_id) { fail(new Error('This preview cannot be confirmed. Reload the page and try again.')); return; }
    const body = `This deletes these files from this computer now, and it cannot be undone: ${parts.join(', ')}; ${fmtBytes(plan.would_free_bytes || 0)} in all. Only these are deleted; anything that changed since this preview is left in place. The posts already made stay where they are, and the record of what was sent is kept. ${kept}`;
    if (!await confirmDialog({ title: 'Clean up now?', body, ok: 'Delete these files', danger: true })) return;
    try { show(await post('/storage/cleanup', { dry_run: false, plan_id: plan.plan_id })); } catch (e) { fail(e); }
    await refresh();
  }));
  return h('div', { class: 'stack', style: 'margin-bottom: 16px' }, facts, h('div', { class: 'row' }, preview, run), out);
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

function appearanceBlock() {
  const g = choiceGroup({ legend: 'Theme', name: 'theme', value: currentTheme(),
    help: 'Automatic follows this computer. Choosing Light or Dark overrides it.',
    options: Object.entries(THEMES).map(([value, label]) => ({ value, label })) });
  g.inputs.forEach((i) => i.addEventListener('change', async () => {
    g.inputs.forEach((input) => { input.disabled = true; });
    try { await saveTheme(i.value); toast(`${THEMES[i.value]} theme on.`); }
    catch (e) {
      g.inputs.forEach((input) => { input.checked = input.value === currentTheme(); });
      toast(e.message, true);
    } finally { g.inputs.forEach((input) => { input.disabled = false; }); }
  }));
  const panel = h('section', { id: 'settings-appearance', class: 'panel narrow', 'aria-labelledby': 'sa-title' },
    h('h2', { id: 'sa-title' }, 'Appearance'), g.el);
  mountWheel(panel);
  return panel;
}

async function agentBlock() {
  const box = h('section', { id: 'settings-agent', class: 'panel narrow', 'aria-labelledby': 'sg-title' },
    h('h2', { id: 'sg-title' }, 'Agent setup'));
  const info = await agentOverview(true);
  if (!info) return box.append(h('p', { class: 'muted' }, 'Agent setup is not available right now.')), box;
  if (info.paired) {
    const revoke = h('button', { type: 'button', class: 'btn danger', help: 'Stop the paired agent from answering key requests. Keys already saved stay.' }, 'Revoke');
    revoke.addEventListener('click', async () => {
      if (!await confirmDialog({ title: 'Revoke agent access?', body: 'The helper can no longer answer requests; open requests are cancelled. Saved keys stay.', ok: 'Revoke', danger: true })) return;
      try { await api('/agent/pairing', { method: 'DELETE' }); toast('Agent access revoked.'); location.reload(); } catch (e) { toast(e.message, true); }
    });
    box.append(h('p', {}, `Paired with ${info.paired.agent_label}. Its helper is ${info.helper.online ? 'running' : 'not running'}.`),
      h('div', { class: 'row', style: 'margin: 8px 0 12px' }, revoke));
  }
  const pick = selectField({ label: 'Platform', help: 'Choose which keys to ask the agent for.',
    options: info.targets.map((t) => ({ value: t.target, label: `${t.target.startsWith('source') ? 'Watch' : 'Post'}: ${t.label}` })) });
  const slot = h('div');
  const draw = async () => { const p = await agentPanel(pick.input.value, () => {}, 'link'); slot.replaceChildren(...(p ? [p] : [])); };
  pick.input.addEventListener('change', draw);
  box.append(pick.el, slot);
  await draw();
  return box;
}

function helpBlock(health) {
  const replay = h('button', { type: 'button', class: 'btn', help: 'Show the guided tour of every screen again.' }, 'Replay tutorial');
  replay.addEventListener('click', () => startTour());
  return h('section', { id: 'settings-help', class: 'panel narrow', 'aria-labelledby': 'sh-title' },
    h('h2', { id: 'sh-title' }, 'Help and about'),
    h('div', { class: 'row', style: 'margin: 12px 0' }, replay,
      h('a', { class: 'btn', href: '#/setup', help: 'Go through the setup questions again with your current answers filled in.' }, 'Run setup again')),
    h('dl', { class: 'kv long' },
      h('dt', {}, 'Version'), h('dd', {}, health ? health.version : '-'),
      h('dt', {}, 'Your data'), h('dd', {}, 'Kept in the app\'s data folder, outside the program, so updates keep your settings, clips and tutorial progress.')),
    h('p', { style: 'margin-top: 12px' }, 'To update, follow the steps for how you installed it in the ',
      h('a', { href: '#/guide/updating', help: 'Open the Updating page of the guide.' }, 'Updating guide'), '.'));
}
