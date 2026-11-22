// Channels: list with start/stop/edit/delete, and the add/edit form (GET/POST/PUT/DELETE /api/channels).

import { ApiError, del, get, list, post, put } from '../api.js';
import { CHANNEL_STATES, PLATFORMS, channelName, isTopicMode, pill, platformName } from '../labels.js';
import { busy, choiceGroup, confirmDialog, h, poll, patch, switchField, textField, titleRow, toast } from '../ui.js';

const PATTERNS = [
  ['twitch', /^(?:https?:\/\/)?(?:www\.|m\.)?twitch\.tv\/([A-Za-z0-9_]{1,25})\/?(?:[?#].*)?$/],
  ['kick', /^(?:https?:\/\/)?(?:www\.)?kick\.com\/([A-Za-z0-9_-]{1,40})\/?(?:[?#].*)?$/],
  ['youtube', /^(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/(@[A-Za-z0-9._-]{1,100}|channel\/UC[\w-]{22})\/?(?:[?#].*)?$/],
];
// a feed address the creator gave you (the server accepts these schemes too)
const FEED = /^(?:streamlink:\S+|(?:rtmps?|srt|rtsp):\/\/[^/\s]+\S*|https?:\/\/[^/\s]+\/\S*\.m3u8(?:\?\S*)?)$/i;

export function detectPlatform(url) {
  const s = String(url || '').trim();
  for (const [platform, rx] of PATTERNS) {
    const m = s.match(rx);
    if (m) return { platform, login: m[1] };
  }
  if (FEED.test(s)) return { platform: 'custom', login: s };
  return null;
}

const DEST_NAMES = { youtube: 'YouTube Shorts', tiktok: 'TikTok', instagram: 'Instagram Reels' };

export async function render(main, params) {
  if (params[0]) return renderForm(main, params[0] === 'new' ? null : params[0]);
  const box = h('div', { id: 'channel-list', class: 'panel' });
  main.append(titleRow('Channels', 'Every channel the app watches. Start or stop one at any time.',
    h('a', { class: 'btn primary', href: '#/channels/new', help: 'Paste a streamer link to start watching a channel.' }, 'Add channel')), box);

  async function load() {
    const rows = list(await get('/channels'), 'channels');
    patch(box, rows.length ? h('ul', { class: 'rows' }, rows.map((c) => row(c, load)))
      : h('div', { class: 'empty' }, h('strong', {}, 'No channels yet'), 'Add one to start watching. ',
        h('a', { href: '#/channels/new', help: 'Paste a streamer link to start watching a channel.' }, 'Add channel')));
  }
  await load();
  return poll(load, 5000);
}

function row(c, refresh) {
  const name = channelName(c);
  const armed = Boolean(c.enabled);
  const arm = h('button', { type: 'button', class: 'btn small', dataset: { key: `arm-${c.id}` },
    help: armed ? 'Stop watching this channel. Nothing is recorded until you start it again.' : 'Start watching this channel for its stream.' },
  armed ? 'Stop watching' : 'Start watching');
  arm.addEventListener('click', busy(arm, async () => {
    try { await post(`/channels/${c.id}/${armed ? 'disarm' : 'arm'}`); await refresh(); } catch (e) { toast(e.message, true); }
  }));
  const remove = h('button', { type: 'button', class: 'btn small danger', dataset: { key: `del-${c.id}` },
    help: 'Remove this channel. Clips already made are kept.' }, 'Delete');
  remove.addEventListener('click', async () => {
    const ok = await confirmDialog({ title: `Delete ${name}?`, body: 'The app stops watching it. Clips already made are kept.', ok: 'Delete channel', danger: true });
    if (!ok) return;
    try { await del(`/channels/${c.id}`); toast(`${name} deleted.`); await refresh(); } catch (e) { toast(e.message, true); }
  });
  return h('li', {},
    h('div', { class: 'grow' }, h('div', { class: 'title' }, name),
      h('div', { class: 'small muted' }, `${platformName(c.platform)} · `,
        isTopicMode(c) ? 'only when the title matches' : 'always watch',
        c.format_id ? ` · format ${c.format_id}` : '')),
    pill(CHANNEL_STATES, c.state),
    h('div', { class: 'row' }, arm,
      h('a', { class: 'btn small ghost', href: `#/channels/${c.id}`, help: `Change ${name}'s format, destinations or turn it off.` }, 'Edit'),
      remove));
}

async function renderForm(main, id) {
  const [existing, formats, dests] = await Promise.all([
    id ? get(`/channels/${encodeURIComponent(id)}`) : null,
    get('/formats').then((d) => list(d, 'formats')).catch(() => []),
    get('/destinations').then((d) => list(d, 'destinations')).catch(() => []),
  ]);
  const c = existing || { url: '', watch_mode: 'ungated', poll_seconds: 120, format_id: null, publish_platforms: [], enabled: true };
  const url = textField({ label: 'Streamer link', type: 'url', value: c.url, placeholder: 'https://www.twitch.tv/streamer_alpha',
    help: id ? 'The channel address. To watch a different address, add it as a new channel.'
      : 'Paste the channel address from Twitch, Kick or YouTube, or a feed address the creator gave you. The platform is detected from it.',
    autocomplete: 'off', readonly: Boolean(id) });
  const detected = h('p', { class: 'small', role: 'status', 'aria-live': 'polite' });
  const showDetected = () => {
    const d = detectPlatform(url.input.value);
    detected.textContent = !url.input.value.trim() ? '' : !d ? 'This does not look like a Twitch, Kick or YouTube channel link yet.'
      : d.platform === 'custom' ? 'Found: a creator-provided feed' : `Found: ${PLATFORMS[d.platform]} channel ${d.login}`;
  };
  url.input.addEventListener('input', showDetected);
  url.el.insertBefore(detected, url.el.querySelector('.error'));
  if (!id) showDetected();
  const urlErr = (msg) => {
    url.el.querySelector('.error').textContent = msg || '';
    if (msg) { url.input.setAttribute('aria-invalid', 'true'); url.input.focus(); } else url.input.removeAttribute('aria-invalid');
  };

  const display = textField({ label: 'Name shown in the app (optional)', value: c.display_name && c.display_name !== c.login ? c.display_name : '',
    maxlength: 100, help: 'A friendlier name for this channel. For a creator feed it is also the name rules use.' });
  const mode = choiceGroup({ legend: 'When to watch', value: isTopicMode(c) ? 'topic_gated' : 'ungated', options: [
    { value: 'ungated', label: 'Always watch', hint: 'Record whenever this channel is live.' },
    { value: 'topic_gated', label: 'Only when the title matches', hint: 'Record only while the stream title matches the format, for example a specific game.' }] });
  const pollSecs = textField({ label: 'Check the title every (seconds)', type: 'number', value: c.poll_seconds || 120, min: 60, max: 900, step: 1,
    help: 'How often the title is checked while it does not match. From 60 to 900 seconds.' });
  const syncPoll = () => { pollSecs.el.hidden = mode.value() !== 'topic_gated'; };
  mode.inputs.forEach((i) => i.addEventListener('change', syncPoll));
  syncPoll();
  const fmt = h('select', { id: 'ch-format', help: 'Decides which moments become clips for this channel.', 'aria-describedby': 'ch-format-help' },
    h('option', { value: '' }, 'The active format'), formats.map((f) => h('option', { value: f.id }, f.name || f.id)));
  fmt.value = c.format_id || '';
  const providers = [...new Set(dests.filter((d) => !d.test).map((d) => d.provider))];
  const destGroup = choiceGroup({ type: 'checkbox', legend: 'Where its clips may be posted', value: c.publish_platforms || [],
    help: 'Clips from this channel only go to the destinations ticked here. None ticked means every destination you turn on.',
    options: (providers.length ? providers : Object.keys(DEST_NAMES)).map((p) => ({ value: p, label: DEST_NAMES[p] || p })) });
  const enabled = switchField({ label: 'Watch this channel', checked: c.enabled !== false,
    help: 'When off, the channel is kept but nothing is watched or recorded.' });
  const formErr = h('p', { class: 'error', role: 'alert' });
  const save = h('button', { type: 'submit', class: 'btn primary', help: id ? 'Save the changes to this channel.' : 'Add this channel.' },
    id ? 'Save changes' : 'Add channel');
  const form = h('form', { id: 'channel-form', class: 'panel narrow', novalidate: true },
    url.el, display.el, mode.el, pollSecs.el,
    h('div', { class: 'field' }, h('label', { for: 'ch-format' }, 'Format'),
      h('p', { class: 'help', id: 'ch-format-help' }, 'Decides which moments become clips. Edit formats under Formats and rules.'), fmt),
    destGroup.el, h('div', { style: 'margin-top: 16px' }, enabled.el), formErr,
    h('div', { class: 'row', style: 'margin-top: 20px' }, save,
      h('a', { class: 'btn ghost', href: '#/channels', help: 'Leave without saving.' }, 'Cancel')));
  form.addEventListener('submit', busy(save, async (e) => {
    e.preventDefault();
    formErr.textContent = '';
    urlErr('');
    if (!id && !detectPlatform(url.input.value)) {
      urlErr('Paste a Twitch, Kick or YouTube channel link, for example https://www.twitch.tv/streamer_alpha.');
      return;
    }
    const body = { watch_mode: mode.value(), format_id: fmt.value || null, publish_platforms: destGroup.values(),
      enabled: enabled.input.checked, display_name: display.input.value.trim() };
    if (mode.value() === 'topic_gated') body.poll_seconds = Math.round(Number(pollSecs.input.value) || 120);
    if (!id) body.url = url.input.value.trim();
    try {
      const saved = id ? await put(`/channels/${encodeURIComponent(id)}`, body) : await post('/channels', body);
      toast(id ? 'Channel saved.' : `${channelName(saved)} added.`);
      location.hash = '#/channels';
    } catch (ex) {
      // the server explains a bad or duplicate link in plain text (422/409)
      if (!id && ex instanceof ApiError && [409, 422].includes(ex.status) && !/watch_mode/.test(ex.message)) urlErr(ex.message);
      else formErr.textContent = ex.message;
    }
  }));
  main.append(titleRow(id ? `Edit ${channelName(c)}` : 'Add a channel',
    'Live Clipper watches the channel and records only while it is live.'), form);
  (id ? display.input : url.input).focus();
}
