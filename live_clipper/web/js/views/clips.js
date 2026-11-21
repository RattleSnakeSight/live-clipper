// Clips: review queue (manual posting), library with filters, and the per-clip editor.

import { ApiError, get, list, post } from '../api.js';
import { CLIP_STATES, DELIVERY_STATES, pill } from '../labels.js';
import { ago, busy, fmtDuration, fmtTime, h, poll, patch, selectField, textField, titleRow, toast } from '../ui.js';

export async function render(main, params) {
  if (params[0]) return renderClip(main, params[0]);
  const queue = h('section', { id: 'review-queue', class: 'panel', 'aria-labelledby': 'rq-title' });
  const results = h('div');
  const [channels, mode] = await Promise.all([
    get('/channels').then((d) => list(d, 'channels')).catch(() => []),
    get('/publish/mode').then((d) => d.mode).catch(() => null)]);
  const ch = selectField({ label: 'Channel', help: 'Show clips from one channel only.', value: '',
    options: [{ value: '', label: 'All channels' }, ...channels.map((c) => ({ value: c.id, label: c.login || c.url }))] });
  const st = selectField({ label: 'Status', help: 'Show clips in one state only.', value: '',
    options: [{ value: '', label: 'Any status' }, ...Object.entries(CLIP_STATES).map(([v, [l]]) => ({ value: v, label: l }))] });
  const q = textField({ label: 'Title contains', type: 'search', help: 'Show clips whose title contains these words.' });
  const apply = h('button', { type: 'submit', class: 'btn', help: 'Apply the filters to the list below.' }, 'Filter');
  const filters = h('form', { class: 'inline-fields', role: 'search', 'aria-label': 'Filter clips', style: 'align-items: end; margin-bottom: 12px' },
    ch.el, st.el, q.el, h('div', {}, apply));
  filters.addEventListener('submit', (e) => { e.preventDefault(); loadLibrary(); });
  ch.input.addEventListener('change', () => loadLibrary());
  st.input.addEventListener('change', () => loadLibrary());

  main.append(titleRow('Clips', 'Every clip the app made. Open one to preview, fix or post it.'),
    queue,
    h('section', { id: 'clip-library', class: 'panel', 'aria-labelledby': 'lib-title' },
      h('h2', { id: 'lib-title' }, 'Library'), filters, results));

  async function loadQueue() {
    const held = mode === 'manual' ? list(await get('/publish/deliveries?status=held'), 'deliveries') : [];
    const byClip = new Map();
    for (const d of held) {
      if (!byClip.has(d.clip_id)) byClip.set(d.clip_id, []);
      byClip.get(d.clip_id).push(d);
    }
    patch(queue, [
      h('header', {}, h('h2', { id: 'rq-title' }, 'Waiting for your approval'),
        h('a', { class: 'end small', href: '#/destinations', help: 'Switch between automatic and manual posting.' }, 'Change posting mode')),
      mode !== 'manual'
        ? h('p', { class: 'muted' }, mode === 'auto' ? 'Posting is automatic, so nothing waits here. Switch to manual on Destinations to approve each clip.'
          : 'Posting is not available yet.')
        : byClip.size === 0 ? h('p', { class: 'muted' }, 'Nothing is waiting. New clips appear here before they are posted.')
          : h('ul', { class: 'rows' }, [...byClip].map(([clipId, ds]) => h('li', {},
            h('div', { class: 'grow' }, h('a', { href: `#/clips/${clipId}`, help: 'Open this clip to preview or edit it before posting.' }, `Clip ${clipId}`)),
            ds.map((d) => postButton(d, loadQueue)))))]);
  }

  async function loadLibrary() {
    const qs = new URLSearchParams();
    if (ch.input.value) qs.set('channel_id', ch.input.value);
    if (st.input.value) qs.set('status', st.input.value);
    if (q.input.value.trim()) qs.set('q', q.input.value.trim());
    const rows = list(await get(`/clips${qs.toString() ? `?${qs}` : ''}`), 'clips');
    patch(results, rows.length ? h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Clip', 'Channel', 'Made', 'Length', 'Status'].map((t, i) => h('th', { scope: 'col', class: i === 3 ? 'n' : null }, t)))),
      h('tbody', {}, rows.map((c) => h('tr', {},
        h('td', {}, h('a', { href: `#/clips/${c.id}`, help: 'Open this clip to preview, edit or post it.' }, c.title || `Clip ${c.id}`)),
        h('td', {}, c.channel || '-'), h('td', {}, ago(c.created_at)), h('td', { class: 'n' }, fmtDuration(c.duration_s)),
        h('td', {}, pill(CLIP_STATES, c.status)))))))
      : h('p', { class: 'muted' }, qs.toString() ? 'No clips match these filters.' : 'No clips yet. They appear a minute or two after a moment is found.'));
  }

  await Promise.all([loadQueue(), loadLibrary()]);
  return poll(async () => { await loadQueue(); }, 8000);
}

function postButton(d, refresh) {
  const b = h('button', { type: 'button', class: 'btn small primary', dataset: { key: `post-${d.id}` },
    help: d.test ? 'Send it to the test destination. Nothing is posted anywhere.' : `Post this clip to ${d.destination_label} now.` },
  `Post to ${d.destination_label || d.destination}`);
  b.addEventListener('click', busy(b, async () => {
    try {
      const r = await post(`/publish/deliveries/${d.id}/post`);
      toast(r.url ? `Posted: ${r.url}` : 'Sent for posting.');
      await refresh();
    } catch (e) { toast(e.message, true); }
  }));
  return b;
}

async function renderClip(main, id) {
  const [clip, presets, dests] = await Promise.all([
    get(`/clips/${encodeURIComponent(id)}`),
    get('/presets').then((d) => list(d, 'presets')).catch(() => []),
    get('/destinations').then((d) => list(d, 'destinations')).catch(() => [])]);
  const history = h('div');
  const video = h('video', { controls: true, preload: 'metadata', src: `/api/clips/${encodeURIComponent(id)}/media/final`,
    'aria-label': `Preview of ${clip.title || `clip ${id}`}` });
  const noVideo = h('p', { class: 'muted', hidden: true }, 'The video is not ready yet. It appears here when rendering finishes.');
  video.addEventListener('error', () => { video.hidden = true; noVideo.hidden = false; });

  const tin = textField({ label: 'Start (seconds)', type: 'number', value: clip.trim_in ?? 0, step: '0.1', min: '0',
    help: 'Where the clip starts, counted from the start of the recorded moment.' });
  const tout = textField({ label: 'End (seconds)', type: 'number', value: clip.trim_out ?? clip.duration_s ?? '', step: '0.1', min: '0',
    help: 'Where the clip ends. Must be after the start.' });
  const title = textField({ label: 'Title', value: clip.title || '', maxlength: 100, help: 'The title used when the clip is posted.' });
  const caps = textField({ label: 'Captions text', type: 'textarea', value: clip.captions || '',
    help: 'The words burned into the video. Fix spelling here; timing is kept.' });
  const preset = selectField({ label: 'Preset', value: clip.preset_id, help: 'How the clip looks: framing, captions style and size.',
    options: presets.length ? presets.map((p) => ({ value: p.id, label: p.name || p.id })) : [{ value: clip.preset_id || '', label: clip.preset_id || 'Default' }] });
  const err = h('p', { class: 'error', id: 'render-err', role: 'alert' });
  const renderBtn = h('button', { type: 'submit', class: 'btn primary', help: 'Make the video again with these changes. Your changes win over every format, rule and preset.' }, 'Render again');
  const editor = h('form', { class: 'panel', novalidate: true, 'aria-labelledby': 'ov-title' },
    h('h2', { id: 'ov-title' }, 'Change this clip'),
    h('p', { class: 'help', style: 'margin-bottom: 12px' }, 'Changes here apply to this clip only and override everything else.'),
    h('div', { class: 'inline-fields' }, tin.el, tout.el), title.el, caps.el, preset.el, err,
    h('div', { class: 'row', style: 'margin-top: 16px' }, renderBtn));
  editor.addEventListener('submit', busy(renderBtn, async (e) => {
    e.preventDefault();
    err.textContent = '';
    const a = Number(tin.input.value);
    const b = Number(tout.input.value);
    if (!(b > a)) { err.textContent = 'The end must be after the start.'; tout.input.focus(); return; }
    try {
      await post(`/clips/${encodeURIComponent(id)}/render`, { overrides: {
        trim_in: a, trim_out: b, title: title.input.value.trim(), captions: caps.input.value, preset_id: preset.input.value } });
      toast('Rendering again. This takes a minute or two.');
      statusSlot.replaceChildren(pill(CLIP_STATES, 'rendering'));
    } catch (ex) {
      err.textContent = ex instanceof ApiError ? Object.values(ex.fields).join(' ') || ex.message : ex.message;
    }
  }));

  async function loadHistory() {
    const rows = list(await get(`/publish/deliveries?clip_id=${encodeURIComponent(id)}`), 'deliveries');
    const used = new Set(rows.map((d) => d.destination_id));
    const allowed = dests.filter((d) => !used.has(d.id) && (d.enabled || d.test) && d.status === 'connected');
    patch(history, [
      rows.length ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Destination', 'Status', 'When', 'Result', ''].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, rows.map((d) => h('tr', {},
          h('td', {}, d.destination_label || d.destination), h('td', {}, pill(DELIVERY_STATES, d.status)),
          h('td', {}, fmtTime(d.updated_at || d.created_at)),
          h('td', {}, d.url ? h('a', { href: d.url, target: '_blank', rel: 'noopener noreferrer', help: 'Open the post on the site in a new tab.' }, 'Open post')
            : d.last_error ? h('span', { class: 'error' }, d.last_error) : '-'),
          h('td', {}, d.status === 'held' ? postButton(d, loadHistory)
            : ['failed', 'cancelled', 'paused'].includes(d.status) ? retryButton(d, loadHistory) : null))))))
        : h('p', { class: 'muted' }, 'Not posted anywhere yet.'),
      allowed.length ? h('div', { class: 'row', style: 'margin-top: 12px' }, h('span', { class: 'small muted' }, 'Also post to:'),
        allowed.map((d) => postNewButton(clip, d, loadHistory))) : null]);
  }

  const statusSlot = h('span', {}, pill(CLIP_STATES, clip.status));
  main.append(
    titleRow(clip.title || `Clip ${id}`, h('p', {}, `${clip.channel || ''} · ${fmtTime(clip.created_at)} · ${fmtDuration(clip.duration_s)} · `, statusSlot),
      h('a', { class: 'btn ghost', href: '#/clips', help: 'Back to the clip library.' }, 'All clips')),
    h('div', { id: 'clip-editor', class: 'grid-main' },
      h('div', { class: 'stack' }, h('div', {}, video, noVideo),
        clip.reason ? h('p', { class: 'small muted' }, `Why this clip: ${clip.reason}`) : null,
        h('section', { class: 'panel', 'aria-labelledby': 'dh-title' }, h('h2', { id: 'dh-title' }, 'Where it was posted'), history)),
      editor));
  await loadHistory();
  return poll(loadHistory, 8000);
}

function retryButton(d, refresh) {
  const b = h('button', { type: 'button', class: 'btn small', dataset: { key: `retry-${d.id}` }, help: 'Try posting this clip to this destination again.' }, 'Retry');
  b.addEventListener('click', busy(b, async () => {
    try { await post(`/publish/deliveries/${d.id}/retry`); toast('Queued again.'); await refresh(); } catch (e) { toast(e.message, true); }
  }));
  return b;
}

function postNewButton(clip, dest, refresh) {
  const b = h('button', { type: 'button', class: 'btn small', dataset: { key: `new-${dest.id}` },
    help: dest.test ? 'Send it to the test destination. Nothing is posted anywhere.' : `Post this clip to ${dest.label}.` }, dest.test ? 'Test destination' : dest.label);
  b.addEventListener('click', busy(b, async () => {
    try {
      await post('/publish/post', { clip_id: clip.id, destination_id: dest.id, language: clip.language || 'en' });
      toast('Sent for posting.');
      await refresh();
    } catch (e) { toast(e.message, true); }
  }));
  return b;
}

