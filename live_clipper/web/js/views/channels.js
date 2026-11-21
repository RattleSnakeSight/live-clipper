// Channels: list with start/stop/edit/delete, and the add/edit form.

import { ApiError, del, get, list, post, put } from '../api.js';
import { CHANNEL_STATES, PLATFORMS, channelName, pill } from '../labels.js';
import { busy, choiceGroup, confirmDialog, h, poll, patch, switchField, textField, titleRow, toast } from '../ui.js';

const PATTERNS = [
  ['twitch', /^(?:https?:\/\/)?(?:www\.|m\.)?twitch\.tv\/([A-Za-z0-9_]{3,25})\/?(?:[?#].*)?$/],
  ['kick', /^(?:https?:\/\/)?(?:www\.)?kick\.com\/([A-Za-z0-9_-]{2,40})\/?(?:[?#].*)?$/],
  ['youtube', /^(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/(?:@|channel\/|c\/)([A-Za-z0-9_.-]+)\/?(?:[?#].*)?$/],
];

export function detectPlatform(url) {
  const s = String(url || '').trim();
  for (const [platform, rx] of PATTERNS) {
    const m = s.match(rx);
    if (m) return { platform, login: m[1] };
  }
  return null;
}

const DEST_NAMES = { youtube: 'YouTube Shorts', tiktok: 'TikTok', instagram: 'Instagram Reels', test: 'Test destination (nothing is posted)' };

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
  const arm = h('button', { type: 'button', class: 'btn small', dataset: { key: `arm-${c.id}` }, disabled: c.enabled === false,
    help: c.armed ? 'Stop watching this channel. Nothing is recorded until you start it again.'
      : c.enabled === false ? 'Turn the channel on in Edit first.' : 'Start watching this channel for its stream.' },
  c.armed ? 'Stop watching' : 'Start watching');
  arm.addEventListener('click', busy(arm, async () => {
    try { await post(`/channels/${c.id}/${c.armed ? 'disarm' : 'arm'}`); await refresh(); } catch (e) { toast(e.message, true); }
  }));
  const remove = h('button', { type: 'button', class: 'btn small danger', dataset: { key: `del-${c.id}` },
    help: 'Remove this channel. Clips already made are kept.' }, 'Delete');
  remove.addEventListener('click', async () => {
    const ok = await confirmDialog({ title: `Delete ${name}?`, body: 'The app stops watching it. Clips already made are kept.', ok: 'Delete channel', danger: true });
    if (!ok) return;
    try { await del(`/channels/${c.id}`); toast(`${name} deleted.`); await refresh(); } catch (e) { toast(e.message, true); }
  });
  return h('li', {},
    h('div', { class: 'grow' }, h('div', { style: 'font-weight:600' }, name),
      h('div', { class: 'small muted' }, `${PLATFORMS[c.platform] || c.platform} · `,
        c.watch_mode === 'topic' ? 'only when the topic matches' : 'always watch',
        c.format_id ? ` · format ${c.format_id}` : '')),
    pill(CHANNEL_STATES, c.state), arm,
    h('a', { class: 'btn small ghost', href: `#/channels/${c.id}`, help: `Change ${name}'s link, format, destinations or turn it off.` }, 'Edit'),
    remove);
}

async function renderForm(main, id) {
  const [existing, formats, dests] = await Promise.all([
    id ? get(`/channels/${encodeURIComponent(id)}`) : null,
    get('/formats').then((d) => list(d, 'formats')).catch(() => []),
    get('/destinations').then((d) => list(d, 'destinations')).catch(() => []),
  ]);
  const c = existing || { url: '', watch_mode: 'always', format_id: (formats[0] || {}).id, publish_platforms: [], enabled: true };
  const url = textField({ label: 'Streamer link', type: 'url', value: c.url, placeholder: 'https://www.twitch.tv/streamer_alpha',
    help: 'Paste the channel address from Twitch, Kick or YouTube. The platform is detected from it.', autocomplete: 'off' });
  const detected = h('p', { class: 'small', role: 'status', 'aria-live': 'polite' });
  const showDetected = () => {
    const d = detectPlatform(url.input.value);
    detected.textContent = !url.input.value.trim() ? '' : d ? `Found: ${PLATFORMS[d.platform]} channel ${d.login}`
      : 'This does not look like a Twitch, Kick or YouTube channel link yet.';
  };
  url.input.addEventListener('input', showDetected);
  url.el.insertBefore(detected, url.el.querySelector('.error'));
  showDetected();

  const mode = choiceGroup({ legend: 'When to watch', value: c.watch_mode, options: [
    { value: 'always', label: 'Always watch', hint: 'Record whenever this channel is live.' },
    { value: 'topic', label: 'Only when the topic matches', hint: 'Record only while the stream title or category matches the chosen format, for example a specific game.' }] });
  const fmt = h('select', { id: 'ch-format', help: 'Decides which moments become clips for this channel.', 'aria-describedby': 'ch-format-help' },
    formats.map((f) => h('option', { value: f.id }, f.name || f.id)));
  if (c.format_id) fmt.value = c.format_id;
  const providers = [...new Set(dests.map((d) => d.provider))];
  const destGroup = choiceGroup({ type: 'checkbox', legend: 'Where its clips may be posted', value: c.publish_platforms || [],
    help: 'Clips from this channel only go to the destinations ticked here, in automatic and manual mode.',
    options: (providers.length ? providers : Object.keys(DEST_NAMES)).map((p) => ({ value: p, label: DEST_NAMES[p] || p })) });
  const enabled = switchField({ label: 'Enabled', checked: c.enabled !== false,
    help: 'When off, the channel is kept but nothing is watched or recorded.' });
  const formErr = h('p', { class: 'error', role: 'alert' });
  const save = h('button', { type: 'submit', class: 'btn primary', help: id ? 'Save the changes to this channel.' : 'Add this channel and start watching it.' },
    id ? 'Save changes' : 'Add channel');
  const form = h('form', { id: 'channel-form', class: 'panel', novalidate: true },
    url.el, mode.el,
    h('div', { class: 'field' }, h('label', { for: 'ch-format' }, 'Format'),
      h('p', { class: 'help', id: 'ch-format-help' }, 'Decides which moments become clips. Edit formats under Formats and rules.'), fmt),
    destGroup.el, h('div', { style: 'margin-top: 16px' }, enabled.el), formErr,
    h('div', { class: 'row', style: 'margin-top: 20px' }, save,
      h('a', { class: 'btn ghost', href: '#/channels', help: 'Leave without saving.' }, 'Cancel')));
  form.addEventListener('submit', busy(save, async (e) => {
    e.preventDefault();
    formErr.textContent = '';
    url.input.removeAttribute('aria-invalid');
    url.el.querySelector('.error').textContent = '';
    if (!detectPlatform(url.input.value)) {
      url.el.querySelector('.error').textContent = 'Paste a Twitch, Kick or YouTube channel link, for example https://www.twitch.tv/streamer_alpha.';
      url.input.setAttribute('aria-invalid', 'true');
      url.input.focus();
      return;
    }
    const body = { url: url.input.value.trim(), watch_mode: mode.value(), format_id: fmt.value || null,
      publish_platforms: destGroup.values(), enabled: enabled.input.checked };
    try {
      const saved = id ? await put(`/channels/${encodeURIComponent(id)}`, body) : await post('/channels', body);
      toast(id ? 'Channel saved.' : `${channelName(saved)} added.`);
      location.hash = '#/channels';
    } catch (ex) {
      const f = ex instanceof ApiError ? ex.fields : {};
      if (f.url) { url.el.querySelector('.error').textContent = f.url; url.input.setAttribute('aria-invalid', 'true'); url.input.focus(); }
      formErr.textContent = f.url ? '' : ex.message;
    }
  }));
  main.append(titleRow(id ? `Edit ${channelName(c)}` : 'Add a channel',
    'Live Clipper watches the channel and records only while it is live.'), form);
  url.input.focus();
}
