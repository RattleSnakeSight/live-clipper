// Clips: review queue, library with filters, and the per-clip editor (GET /api/clips, /api/clips/{id},
// POST /api/clips/{id}/render|review|publish, /api/publish/*).

import { ApiError, get, list, post } from '../api.js';
import { CLIP_STATES, DELIVERY_STATES, channelName, clipStatus, clipTitle, pill } from '../labels.js';
import { ago, busy, confirmDialog, fmtDuration, fmtTime, h, inputDialog, poll, patch, selectField, switchField, textField, titleRow, toast } from '../ui.js';
import { schemaHelp } from './docs.js';

const LANG_NAMES = { en: 'English', es: 'Spanish' };
const LAYOUTS = [['', 'Keep the current framing'], ['auto', 'Auto (facecam above gameplay when found)'], ['fit', 'Fit the whole frame'],
  ['crop', 'Crop the centre'], ['stack', 'Stack'], ['split', 'Split (needs facecam coordinates)']];
const duration = (c) => (c.files || {}).duration;
const langOf = (c) => c.source_lang || Object.keys((c.files || {}).final || {})[0] || 'en';
// the test destination's own label repeats that nothing is posted; the button's help line says it
const destName = (d) => (d.test ? 'Test destination' : d.destination_label || d.destination);

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
  const filters = h('form', { class: 'inline-fields filters', role: 'search', 'aria-label': 'Filter clips' },
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
    const meta = (c) => (c ? [names[c.channel_id], ago(c.created_at), fmtDuration(duration(c))].filter(Boolean).join(' · ') : '');
    patch(queue, [
      h('header', {}, h('h2', { id: 'rq-title' }, 'Waiting for your approval'),
        h('a', { class: 'end small act', href: '#/destinations', help: 'Switch between automatic and manual posting.' }, 'Change posting mode')),
      h('p', { class: 'small muted', style: 'margin-bottom: 8px' }, mode === 'manual'
        ? 'Manual posting: every finished clip waits here until you press Post.'
        : mode === 'auto' ? 'Posting is automatic. Only clips held by a rule or the gameplay check wait here.' : 'Posting is not available yet.'),
      entries.size === 0 ? h('p', { class: 'muted' }, 'Nothing is waiting.')
        : h('ul', { class: 'rows' }, [...entries].map(([clipId, ds]) => h('li', {},
          h('div', { class: 'grow' }, h('a', { class: 'rowlink', href: `#/clips/${clipId}`, help: 'Open this clip to preview or edit it before posting.' }, label(clipId)),
            h('div', { class: 'small muted' }, ds.length ? meta(byId[clipId]) : 'Held for review. Open it to approve or post it anyway.')),
          ds.length ? h('div', { class: 'row' }, ds.map((d) => postButton(d, loadQueue))) : null)))]);
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
      // on a phone the three middle columns move under the title, so the title and the status both fit
      h('thead', {}, h('tr', {}, ['Clip', 'Channel', 'Made', 'Length', 'Status'].map((t, i) => h('th',
        { scope: 'col', class: [i === 3 ? 'n' : '', i > 0 && i < 4 ? 'sm-hide' : ''].join(' ').trim() || null }, t)))),
      h('tbody', {}, rows.map((c) => h('tr', {},
        h('td', {}, h('a', { class: 'rowlink', href: `#/clips/${c.id}`, help: 'Open this clip to preview, edit or post it.' }, clipTitle(c)),
          h('div', { class: 'sm-only small muted' }, [names[c.channel_id], ago(c.created_at), fmtDuration(duration(c))].filter(Boolean).join(' · '))),
        h('td', { class: 'sm-hide' }, names[c.channel_id] || '-'), h('td', { class: 'sm-hide' }, ago(c.created_at)),
        h('td', { class: 'n sm-hide' }, fmtDuration(duration(c))),
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
  return actionButton(`Post to ${destName(d)}`,
    d.test ? 'Send it to the test destination. Nothing is posted anywhere.' : `Post this clip to ${destName(d)} now.`,
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
    out.push(actionButton('Confirm posted', 'You checked the account and the clip is there. Nothing is uploaded again. You can add the post address.',
      `confirm-posted-${d.id}`, async () => {
        const done = await inputDialog({ title: 'Mark as posted?', ok: 'Mark as posted',
          body: 'Only confirm after checking the account: the clip is there. Nothing is uploaded again.',
          label: 'Post address (optional)', help: 'Paste the link to the post so you can open it from this page later. Leave it empty if you do not have it.',
          submit: (url) => post(`/publish/deliveries/${d.id}/confirm-posted`, url ? { url } : {}) });
        if (done) { toast('Marked as posted.'); await refresh(); }
      }));
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
  const [clip, presets, dests, channels, presetSchema] = await Promise.all([
    get(`/clips/${enc}`),
    get('/presets').then((d) => list(d, 'presets')).catch(() => []),
    get('/destinations').then((d) => list(d, 'destinations')).catch(() => []),
    get('/channels').then((d) => list(d, 'channels')).catch(() => []),
    get('/presets/schema').catch(() => null)]);
  const chName = (channels.find((c) => c.id === clip.channel_id) && channelName(channels.find((c) => c.id === clip.channel_id))) || '';
  const status = clipStatus(clip);
  const history = h('div');
  const video = h('video', { controls: true, preload: 'metadata', src: `/api/clips/${enc}/media/final`,
    'aria-label': `Preview of ${clipTitle(clip)}` });
  const noVideo = h('p', { hidden: true }, 'The video is not ready yet. It appears here when rendering finishes.');
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
  // partial preset values for this clip only (sent as `overrides`); the form starts from the clip's effective values
  const H = schemaHelp(presetSchema);
  const v = clip.values || {};
  const at = (a, b, dflt) => ((v[a] || {})[b] ?? dflt);
  const ovr = {
    crf: textField({ label: 'Video quality (CRF)', type: 'number', min: 0, max: 51, step: 1, value: at('encode', 'render_crf', 21), help: H('encode.render_crf') }),
    height: textField({ label: 'Video height (pixels)', type: 'number', min: 240, max: 4320, step: 1, value: at('encode', 'final_height', 1920), help: H('encode.final_height') }),
    capSize: textField({ label: 'Caption size', type: 'number', min: 0.01, max: 0.3, step: 0.001, value: at('captions', 'size_frac', 0.068), help: H('captions.size_frac') }),
    capPos: selectField({ label: 'Caption position', value: String(at('captions', 'alignment', 2)), help: H('captions.alignment'),
      options: [['2', 'Bottom'], ['5', 'Middle'], ['8', 'Top']].map(([value, label]) => ({ value, label })) }),
  };
  const titleOn = switchField({ label: 'Show the title over the start of the clip', checked: at('title', 'duration_s', 2.8) > 0, help: H('title.duration_s') });
  const overrides = () => {
    const out = {};
    const put_ = (a, b, val) => { out[a] = { ...(out[a] || {}), [b]: val }; };
    const num = (f, a, b, dflt) => { if (f.input.value !== '' && Number(f.input.value) !== Number(at(a, b, dflt))) put_(a, b, Number(f.input.value)); };
    num(ovr.crf, 'encode', 'render_crf', 21);
    num(ovr.height, 'encode', 'final_height', 1920);
    num(ovr.capSize, 'captions', 'size_frac', 0.068);
    if (Number(ovr.capPos.input.value) !== Number(at('captions', 'alignment', 2))) put_('captions', 'alignment', Number(ovr.capPos.input.value));
    const was = at('title', 'duration_s', 2.8) > 0;
    if (titleOn.input.checked !== was) put_('title', 'duration_s', titleOn.input.checked ? (at('title', 'duration_s', 0) || 2.8) : 0);
    return out;
  };
  const lookBox = h('fieldset', { class: 'form-group', id: 'clip-look' },
    h('legend', {}, 'Look of this clip only'),
    h('p', { class: 'help', style: 'margin-bottom: 8px' }, 'These values replace the preset for this clip. Leave them as they are to keep the preset.'),
    h('div', { class: 'inline-fields' }, ovr.crf.el, ovr.height.el, ovr.capSize.el, ovr.capPos.el), titleOn.el);
  const err = h('p', { class: 'error', id: 'render-err', role: 'alert' });
  const part = (title, ...kids) => h('div', { class: 'form-group', role: 'group', 'aria-label': title }, h('h3', {}, title), kids);
  const renderBtn = h('button', { type: 'submit', class: 'btn primary', help: 'Make the video again with these changes. Your changes win over every format, rule and preset.' }, 'Render again');
  const editor = h('form', { class: 'panel', novalidate: true, 'aria-labelledby': 'ov-title' },
    h('h2', { id: 'ov-title' }, 'Change this clip'),
    h('p', { class: 'help' }, 'Changes here apply to this clip only and override everything else.'),
    part('Trim', h('div', { class: 'inline-fields' }, tin.el, tout.el)),
    titles.length || srts.length ? part('Title and captions', titles.map(([, f]) => f.el), srts.map(([, f]) => f.el)) : null,
    part('Framing and preset', layout.el, preset.el),
    lookBox, h('div', { class: 'row form-actions' }, renderBtn, err));
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
    const ov = overrides();
    if (Object.keys(ov).length) body.overrides = ov;
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
      reviewOut.textContent = approved ? (r.published && r.published.length ? 'Approved and sent to posting.' : 'Approved.')
        : 'Rejected.' + (r.media ? (r.media.removed ? ` Its files were deleted (${(r.media.freed_bytes / 1048576).toFixed(1)} MB freed).` : ` Its files stay: ${r.media.kept_because}.`) : '');
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
        // three columns fit beside the editor and on a phone: the time sits under the name, the result under the state
        h('thead', {}, h('tr', {}, ['Destination', 'Status', 'Actions'].map((x) => h('th', { scope: 'col' }, x)))),
        h('tbody', {}, rows.map((d) => h('tr', {},
          h('td', {}, destName(d), h('div', { class: 'small muted' }, fmtTime(d.updated_at || d.created_at))),
          h('td', {}, pill(DELIVERY_STATES, d.status),
            d.url ? h('div', {}, h('a', { class: 'act', href: d.url, target: '_blank', rel: 'noopener noreferrer', help: 'Open the post on the site in a new tab.' }, 'Open post'))
              : d.last_error ? h('div', { class: 'error' }, d.last_error) : null),
          h('td', {}, h('div', { class: 'row' }, deliveryActions(d, loadHistory))))))))
        : h('p', { class: 'muted' }, 'Not posted anywhere yet.'),
      allowed.length ? h('div', { class: 'row', style: 'margin-top: 12px' }, h('span', { class: 'small muted' }, 'Also post to:'),
        allowed.map((d) => postNewButton(clip, d, loadHistory))) : null]);
  }

  const statusSlot = h('span', {}, pill(CLIP_STATES, status));
  main.append(
    titleRow(clipTitle(clip), h('p', {}, `${chName} · ${fmtTime(clip.created_at)} · ${fmtDuration(len)} · `, statusSlot),
      h('a', { class: 'btn ghost', href: '#/clips', help: 'Back to the clip library.' }, 'All clips')),
    h('div', { id: 'clip-editor', class: 'grid-main wide-side' },
      h('div', { class: 'stack' }, h('div', { class: 'stage' }, video, noVideo),
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
