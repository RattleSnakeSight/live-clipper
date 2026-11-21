// Clips: review queue, library with filters, and the per-clip editor (GET /api/clips, /api/clips/{id},
// POST /api/clips/{id}/render|review|publish, /api/publish/*).

import { ApiError, get, list, post } from '../api.js';
import { CLIP_STATES, DELIVERY_STATES, channelName, clipStatus, clipTitle, pill } from '../labels.js';
import { ago, busy, confirmDialog, fmtDuration, fmtTime, h, poll, patch, selectField, textField, titleRow, toast } from '../ui.js';

const LANG_NAMES = { en: 'English', es: 'Spanish' };
const LAYOUTS = [['', 'Keep the current framing'], ['auto', 'Auto (facecam above gameplay when found)'], ['fit', 'Fit the whole frame'],
  ['crop', 'Crop the centre'], ['stack', 'Stack'], ['split', 'Split (needs facecam coordinates)']];
const duration = (c) => (c.files || {}).duration;
const langOf = (c) => c.source_lang || Object.keys((c.files || {}).final || {})[0] || 'en';

export async function render(main, params) {
  if (params[0]) return renderClip(main, params[0]);
  const queue = h('section', { id: 'review-queue', class: 'panel', 'aria-labelledby': 'rq-title' });
  const results = h('div');
  const [channels, mode] = await Promise.all([
    get('/channels').then((d) => list(d, 'channels')).catch(() => []),
    get('/publish/mode').then((d) => d.mode).catch(() => null)]);
  const names = Object.fromEntries(channels.map((c) => [c.id, channelName(c)]));
  const ch = selectField({ label: 'Channel', help: 'Show clips from one channel only.', value: '',
    options: [{ value: '', label: 'All channels' }, ...channels.map((c) => ({ value: c.id, label: channelName(c) }))] });
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
    const [held, clips] = await Promise.all([
      get('/publish/deliveries?status=held').then((d) => list(d, 'deliveries')).catch(() => []),
      get('/clips?limit=200').then((d) => list(d, 'clips')).catch(() => [])]);
    const byId = Object.fromEntries(clips.map((c) => [c.id, c]));
    const entries = new Map();
    for (const d of held) {
      if (!entries.has(d.clip_id)) entries.set(d.clip_id, []);
      entries.get(d.clip_id).push(d);
    }
    for (const c of clips) if (clipStatus(c) === 'held' && !entries.has(c.id)) entries.set(c.id, []);
    const label = (id) => (byId[id] ? clipTitle(byId[id]) : `Clip ${id}`);
    patch(queue, [
      h('header', {}, h('h2', { id: 'rq-title' }, 'Waiting for your approval'),
        h('a', { class: 'end small', href: '#/destinations', help: 'Switch between automatic and manual posting.' }, 'Change posting mode')),
      h('p', { class: 'small muted', style: 'margin-bottom: 8px' }, mode === 'manual'
        ? 'Manual posting: every finished clip waits here until you press Post.'
        : mode === 'auto' ? 'Posting is automatic. Only clips held by a rule or the gameplay check wait here.' : 'Posting is not available yet.'),
      entries.size === 0 ? h('p', { class: 'muted' }, 'Nothing is waiting.')
        : h('ul', { class: 'rows' }, [...entries].map(([clipId, ds]) => h('li', {},
          h('div', { class: 'grow' }, h('a', { href: `#/clips/${clipId}`, help: 'Open this clip to preview or edit it before posting.' }, label(clipId)),
            ds.length ? null : h('div', { class: 'small muted' }, 'Held for review. Open it to approve or post it anyway.')),
          ds.map((d) => postButton(d, loadQueue)))))]);
  }

  async function loadLibrary() {
    const qs = new URLSearchParams({ limit: '200' });
    if (ch.input.value) qs.set('channel_id', ch.input.value);
    let rows = list(await get(`/clips?${qs}`), 'clips');
    const words = q.input.value.trim().toLowerCase();
    if (st.input.value) rows = rows.filter((c) => clipStatus(c) === st.input.value);
    if (words) rows = rows.filter((c) => clipTitle(c).toLowerCase().includes(words));
    const filtered = Boolean(ch.input.value || st.input.value || words);
    patch(results, rows.length ? h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, ['Clip', 'Channel', 'Made', 'Length', 'Status'].map((t, i) => h('th', { scope: 'col', class: i === 3 ? 'n' : null }, t)))),
      h('tbody', {}, rows.map((c) => h('tr', {},
        h('td', {}, h('a', { href: `#/clips/${c.id}`, help: 'Open this clip to preview, edit or post it.' }, clipTitle(c))),
        h('td', {}, names[c.channel_id] || '-'), h('td', {}, ago(c.created_at)), h('td', { class: 'n' }, fmtDuration(duration(c))),
        h('td', {}, pill(CLIP_STATES, clipStatus(c))))))))
      : h('p', { class: 'muted' }, filtered ? 'No clips match these filters.' : 'No clips yet. They appear a minute or two after a moment is found.'));
  }

  await Promise.all([loadQueue(), loadLibrary()]);
  return poll(async () => { await loadQueue(); }, 8000);
}

function actionButton(label, help, key, fn, cls = 'btn small') {
  const b = h('button', { type: 'button', class: cls, dataset: { key }, help }, label);
  b.addEventListener('click', busy(b, async () => { try { await fn(); } catch (e) { toast(e.message, true); } }));
  return b;
}

function postButton(d, refresh) {
  return actionButton(`Post to ${d.destination_label || d.destination}`,
    d.test ? 'Send it to the test destination. Nothing is posted anywhere.' : `Post this clip to ${d.destination_label} now.`,
    `post-${d.id}`, async () => {
      const r = await post(`/publish/deliveries/${d.id}/post`);
      toast(r.url ? `Posted: ${r.url}` : 'Sent for posting.');
      await refresh();
    }, 'btn small primary');
}

// buttons for one delivery row, by state
function deliveryActions(d, refresh) {
  const act = (label, help, path, body, msg) => actionButton(label, help, `${path}-${d.id}`, async () => {
    await post(`/publish/deliveries/${d.id}/${path}`, body);
    toast(msg);
    await refresh();
  });
  const out = [];
  if (d.status === 'held') out.push(postButton(d, refresh));
  if (['failed', 'cancelled', 'paused'].includes(d.status)) out.push(act('Retry', 'Try posting this clip to this destination again.', 'retry', {}, 'Queued again.'));
  if (d.status === 'uncertain') out.push(act('Check again', 'Ask the site again whether the upload landed.', 'recheck', {}, 'Checked.'));
  if (d.status === 'needs_confirmation') {
    out.push(act('Confirm posted', 'You checked the account and the clip is there. Nothing is uploaded again.', 'confirm-posted', {}, 'Marked as posted.'));
    out.push(actionButton('Confirm not posted', 'You checked the account and the clip is not there. The app uploads it again.',
      `confirm-not-posted-${d.id}`, async () => {
        if (!await confirmDialog({ title: 'Upload it again?', body: 'Only confirm after checking the account: the clip is uploaded again.', ok: 'Upload again' })) return;
        await post(`/publish/deliveries/${d.id}/confirm-not-posted`, { confirm: true });
        toast('Queued again.');
        await refresh();
      }));
  }
  if (['held', 'queued', 'waiting_language', 'paused', 'blocked'].includes(d.status)) {
    out.push(act('Cancel', 'Do not post this clip to this destination.', 'cancel', {}, 'Cancelled.'));
  }
  return out;
}

async function renderClip(main, id) {
  const enc = encodeURIComponent(id);
  const [clip, presets, dests, channels] = await Promise.all([
    get(`/clips/${enc}`),
    get('/presets').then((d) => list(d, 'presets')).catch(() => []),
    get('/destinations').then((d) => list(d, 'destinations')).catch(() => []),
    get('/channels').then((d) => list(d, 'channels')).catch(() => [])]);
  const chName = (channels.find((c) => c.id === clip.channel_id) && channelName(channels.find((c) => c.id === clip.channel_id))) || '';
  const status = clipStatus(clip);
  const history = h('div');
  const video = h('video', { controls: true, preload: 'metadata', src: `/api/clips/${enc}/media/final`,
    'aria-label': `Preview of ${clipTitle(clip)}` });
  const noVideo = h('p', { class: 'muted', hidden: true }, 'The video is not ready yet. It appears here when rendering finishes.');
  video.addEventListener('error', () => {
    if (clip.media && clip.media.preview && !video.dataset.fallback) { video.dataset.fallback = '1'; video.src = clip.media.preview; return; }
    video.hidden = true;
    noVideo.hidden = false;
  });

  // the per-clip override: trim (seconds from the current clip start), titles and captions per language,
  // framing, preset. Only changed values are sent.
  const len = duration(clip);
  const tin = textField({ label: 'Start (seconds)', type: 'number', value: 0, step: '0.1',
    help: 'Move the start, in seconds from the current start. Negative values reach back into the recording.' });
  const tout = textField({ label: 'End (seconds)', type: 'number', value: len ?? '', step: '0.1',
    help: 'Where the clip ends, in seconds from the current start. Must be at least 2 seconds after the start.' });
  const caps = clip.captions || {};
  const langs = Object.keys(caps).filter((l) => LANG_NAMES[l] && caps[l] && caps[l].status === 'ready');
  const titles = langs.map((l) => [l, textField({ label: `Title (${LANG_NAMES[l]})`, value: caps[l].title || '', maxlength: 100,
    help: 'The hook text shown at the start of the clip and used when it is posted.' })]);
  const srts = Object.keys(clip.srt || {}).map((l) => [l, textField({ label: `Captions (${LANG_NAMES[l] || l}, SRT)`, type: 'textarea',
    value: clip.srt[l], spellcheck: 'false', help: 'The burned-in captions as SRT text. Fix the words; keep the timing lines.' })]);
  const layout = selectField({ label: 'Framing', value: '', help: 'How the stream is framed in the vertical video.',
    options: LAYOUTS.map(([value, label]) => ({ value, label })) });
  const preset = selectField({ label: 'Preset', value: clip.preset_id, help: 'How the clip looks: size, captions style and loudness.',
    options: presets.length ? presets.map((p) => ({ value: p.id, label: p.name || p.id })) : [{ value: clip.preset_id || 'default', label: clip.preset_id || 'Default' }] });
  const err = h('p', { class: 'error', id: 'render-err', role: 'alert' });
  const renderBtn = h('button', { type: 'submit', class: 'btn primary', help: 'Make the video again with these changes. Your changes win over every format, rule and preset.' }, 'Render again');
  const editor = h('form', { class: 'panel', novalidate: true, 'aria-labelledby': 'ov-title' },
    h('h2', { id: 'ov-title' }, 'Change this clip'),
    h('p', { class: 'help', style: 'margin-bottom: 12px' }, 'Changes here apply to this clip only and override everything else.'),
    h('div', { class: 'inline-fields' }, tin.el, tout.el), titles.map(([, f]) => f.el), srts.map(([, f]) => f.el),
    layout.el, preset.el, err, h('div', { class: 'row', style: 'margin-top: 16px' }, renderBtn));
  editor.addEventListener('submit', busy(renderBtn, async (e) => {
    e.preventDefault();
    err.textContent = '';
    const a = Number(tin.input.value);
    const b = Number(tout.input.value);
    const body = {};
    if (tin.input.value !== '0' || (len !== undefined && b !== len)) {
      if (!(b - a >= 2)) { err.textContent = 'The end must be at least 2 seconds after the start.'; tout.input.focus(); return; }
      body.trim = [a, b];
    }
    const changed = (pairs, base) => Object.fromEntries(pairs.filter(([l, f]) => f.input.value !== base(l)).map(([l, f]) => [l, f.input.value]));
    const t = changed(titles, (l) => caps[l].title || '');
    const s = changed(srts, (l) => clip.srt[l]);
    if (Object.keys(t).length) body.title = t;
    if (Object.keys(s).length) body.srt = s;
    if (layout.input.value) body.layout = { mode: layout.input.value };
    if (preset.input.value && preset.input.value !== clip.preset_id) body.preset_id = preset.input.value;
    if (!Object.keys(body).length) { err.textContent = 'Nothing changed yet.'; return; }
    try {
      const r = await post(`/clips/${enc}/render`, body);
      toast(r.status === 'already_rendered' ? 'Already up to date.' : 'Rendering again. This takes a minute or two.');
      statusSlot.replaceChildren(pill(CLIP_STATES, 'rendering'));
    } catch (ex) {
      err.textContent = ex instanceof ApiError ? Object.values(ex.fields).join(' ') || ex.message : ex.message;
    }
  }));

  // review: the gameplay check and selection can hold a clip; the user decides
  const reviewOut = h('p', { class: 'small', role: 'status', 'aria-live': 'polite' });
  const review = (approved) => actionButton(approved ? 'Approve' : 'Reject',
    approved ? 'Approve this clip. If it is finished it goes to posting.' : 'Mark this clip as not suitable. It is not posted.',
    `review-${approved}`, async () => {
      const r = await post(`/clips/${enc}/review`, { approved });
      reviewOut.textContent = approved ? (r.published && r.published.length ? 'Approved and sent to posting.' : 'Approved.') : 'Rejected.';
      await loadHistory();
    });
  const postAnyway = actionButton('Post anyway', 'Send the finished clip to posting whatever the format or rules decided.', 'publish', async () => {
    await post(`/clips/${enc}/publish`, {});
    reviewOut.textContent = 'Sent to posting.';
    await loadHistory();
  });
  const reasons = Object.entries((clip.trigger && clip.trigger.selection) || {})
    .map(([l, s]) => `${LANG_NAMES[l] || l}: ${s.decision}${(s.reasons || []).length ? ` (${s.reasons.join('; ')})` : ''}`);

  async function loadHistory() {
    const rows = list(await get(`/publish/deliveries?clip_id=${enc}`), 'deliveries');
    const used = new Set(rows.map((d) => d.destination_id));
    const allowed = clip.status === 'ready' ? dests.filter((d) => !used.has(d.id) && (d.enabled || d.test) && d.status === 'connected') : [];
    patch(history, [
      rows.length ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Destination', 'Status', 'When', 'Result', ''].map((x) => h('th', { scope: 'col' }, x)))),
        h('tbody', {}, rows.map((d) => h('tr', {},
          h('td', {}, d.destination_label || d.destination), h('td', {}, pill(DELIVERY_STATES, d.status)),
          h('td', {}, fmtTime(d.updated_at || d.created_at)),
          h('td', {}, d.url ? h('a', { href: d.url, target: '_blank', rel: 'noopener noreferrer', help: 'Open the post on the site in a new tab.' }, 'Open post')
            : d.last_error ? h('span', { class: 'error' }, d.last_error) : '-'),
          h('td', {}, h('div', { class: 'row' }, deliveryActions(d, loadHistory))))))))
        : h('p', { class: 'muted' }, 'Not posted anywhere yet.'),
      allowed.length ? h('div', { class: 'row', style: 'margin-top: 12px' }, h('span', { class: 'small muted' }, 'Also post to:'),
        allowed.map((d) => postNewButton(clip, d, loadHistory))) : null]);
  }

  const statusSlot = h('span', {}, pill(CLIP_STATES, status));
  main.append(
    titleRow(clipTitle(clip), h('p', {}, `${chName} · ${fmtTime(clip.created_at)} · ${fmtDuration(len)} · `, statusSlot),
      h('a', { class: 'btn ghost', href: '#/clips', help: 'Back to the clip library.' }, 'All clips')),
    h('div', { id: 'clip-editor', class: 'grid-main' },
      h('div', { class: 'stack' }, h('div', {}, video, noVideo),
        clip.error ? h('p', { class: 'note bad' }, clip.error) : null,
        h('section', { class: 'panel', 'aria-labelledby': 'rv-title' }, h('h2', { id: 'rv-title' }, 'Decision'),
          reasons.length ? h('ul', { class: 'small' }, reasons.map((r) => h('li', {}, r))) : h('p', { class: 'small muted' }, 'No posting decision yet.'),
          clip.status === 'ready' ? h('div', { class: 'row', style: 'margin-top: 8px' }, review(true), review(false), postAnyway) : null, reviewOut),
        h('section', { class: 'panel', 'aria-labelledby': 'dh-title' }, h('h2', { id: 'dh-title' }, 'Where it was posted'), history)),
      editor));
  await loadHistory();
  return poll(loadHistory, 8000);
}

function postNewButton(clip, dest, refresh) {
  return actionButton(dest.test ? 'Test destination' : dest.label,
    dest.test ? 'Send it to the test destination. Nothing is posted anywhere.' : `Post this clip to ${dest.label}.`,
    `new-${dest.id}`, async () => {
      await post('/publish/post', { clip_id: clip.id, destination_id: dest.id, language: langOf(clip) });
      toast('Sent for posting.');
      await refresh();
    });
}
