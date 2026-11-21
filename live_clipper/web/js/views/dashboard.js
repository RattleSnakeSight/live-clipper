// Dashboard: which channels need attention right now, what was clipped and what was posted.

import { get, list, post } from '../api.js';
import { CHANNEL_STATES, CLIP_STATES, PLATFORMS, channelName, pill, statusSource } from '../labels.js';
import { ago, busy, h, patch, poll, titleRow, toast } from '../ui.js';

export function channelCard(c, refresh) {
  const needs = c.state === 'needs_action';
  const name = channelName(c);
  const arm = c.armed
    ? h('button', { type: 'button', class: 'btn small', dataset: { key: `arm-${c.id}` },
      help: 'Stop watching this channel. Nothing is recorded until you start it again.' }, 'Stop watching')
    : h('button', { type: 'button', class: 'btn small', dataset: { key: `arm-${c.id}` }, disabled: c.enabled === false,
      help: c.enabled === false ? 'Turn the channel on in Edit first.' : 'Start watching this channel for its stream.' }, 'Start watching');
  arm.addEventListener('click', busy(arm, async () => {
    try {
      await post(`/channels/${c.id}/${c.armed ? 'disarm' : 'arm'}`);
      await refresh();
    } catch (e) { toast(e.message, true); }
  }));
  return h('article', { class: `card${needs ? ' needs' : ''}`, 'aria-labelledby': `ch-${c.id}`, dataset: { id: c.id } },
    h('header', {},
      h('div', {}, h('div', { class: 'name', id: `ch-${c.id}` }, name),
        h('div', { class: 'meta' }, PLATFORMS[c.platform] || c.platform)),
      pill(CHANNEL_STATES, c.state)),
    h('p', { class: 'meta' }, statusSource(c)),
    needs ? h('div', { class: 'note', role: 'status' }, h('p', {}, c.detail || 'This channel needs your attention.'),
      c.fix_url ? h('a', { href: c.fix_url, help: 'Open the screen where this can be fixed.' }, 'Fix this') : null)
      : c.detail ? h('p', { class: 'small' }, c.detail) : null,
    h('p', { class: 'meta' }, c.watch_mode === 'topic' ? 'Watches only when the stream topic matches its format'
      : 'Watches every stream'),
    h('div', { class: 'actions' }, arm,
      h('a', { class: 'btn small ghost', href: `#/channels/${c.id}`, help: `Change ${name}'s link, format, destinations or turn it off.` }, 'Edit')));
}

function finding(channels) {
  const by = (s) => channels.filter((c) => c.state === s).length;
  const parts = [];
  if (by('needs_action')) parts.push(`${by('needs_action')} need${by('needs_action') > 1 ? '' : 's'} action`);
  if (by('recording')) parts.push(`${by('recording')} recording`);
  if (by('reconnecting')) parts.push(`${by('reconnecting')} reconnecting`);
  if (by('waiting')) parts.push(`${by('waiting')} waiting for a stream`);
  if (!channels.length) return 'No channels yet. Add one to start clipping.';
  return parts.length ? `${channels.length} channel${channels.length > 1 ? 's' : ''}: ${parts.join(', ')}.` : 'All channels are offline.';
}

export async function render(main) {
  const sub = h('p', { class: 'finding', role: 'status' });
  const cards = h('div', { class: 'cards', id: 'channel-cards' });
  const clipsBox = h('div');
  const postBox = h('div');
  const healthBox = h('div');
  main.append(
    titleRow('Dashboard', sub, h('a', { class: 'btn primary', href: '#/channels/new', help: 'Paste a streamer link to start watching a channel.' }, 'Add channel')),
    h('div', { class: 'grid-main' },
      h('section', { 'aria-labelledby': 'ch-title' }, h('h2', { id: 'ch-title', class: 'sr-only' }, 'Channels'), cards),
      h('aside', { id: 'rail', class: 'stack', 'aria-label': 'Clips, posting and health' },
        h('section', { class: 'panel', 'aria-labelledby': 'rc-title' },
          h('header', {}, h('h2', { id: 'rc-title' }, 'Recent clips'),
            h('a', { class: 'end small', href: '#/clips', help: 'Open the full clip library.' }, 'View all')), clipsBox),
        h('section', { class: 'panel', 'aria-labelledby': 'ps-title' }, h('h2', { id: 'ps-title' }, 'Posting, last 24 hours'), postBox),
        h('section', { class: 'panel', 'aria-labelledby': 'hl-title' },
          h('header', {}, h('h2', { id: 'hl-title' }, 'Health'),
            h('a', { class: 'end small', href: '#/status', help: 'Open the Status page for details and logs.' }, 'Details')), healthBox))));

  async function loadChannels() {
    const channels = list(await get('/channels'), 'channels');
    sub.textContent = finding(channels);
    patch(cards, channels.length ? channels.map((c) => channelCard(c, loadChannels))
      : h('div', { class: 'panel empty' }, h('strong', {}, 'No channels yet'),
        'Add a Twitch, Kick or YouTube channel and the app starts watching for its stream. ',
        h('a', { href: '#/channels/new', help: 'Paste a streamer link to start watching a channel.' }, 'Add your first channel')));
  }

  async function loadHealth() {
    const s = await get('/status');
    const rows = Object.entries(s.services || {});
    const bad = rows.filter(([, v]) => v && v.error);
    patch(healthBox, [
      h('p', {}, bad.length ? `${bad.length} part${bad.length > 1 ? 's' : ''} of the app failed to start.` : 'All parts of the app are running.'),
      h('p', { class: 'small muted num' }, `Version ${s.version} · running for ${Math.round((s.uptime || 0) / 60)} min`),
      bad.length ? h('ul', { class: 'small' }, bad.map(([n, v]) => h('li', {}, `${n}: ${v.error}`))) : null]);
  }

  async function loadClips() {
    const [clips, dels] = await Promise.all([
      get('/clips').then((d) => list(d, 'clips')).catch(() => null),
      get('/publish/deliveries').then((d) => list(d, 'deliveries')).catch(() => null)]);
    patch(clipsBox, clips === null ? h('p', { class: 'muted' }, 'Clips are not available yet.')
      : clips.length ? h('ul', { class: 'rows' }, clips.slice(0, 5).map((c) => h('li', {},
        h('div', { class: 'grow' }, h('a', { class: 'truncate', style: 'display:block', href: `#/clips/${c.id}`,
          help: 'Open this clip to preview, edit or post it.' }, c.title || `Clip ${c.id}`),
        h('span', { class: 'small muted' }, `${c.channel || ''} · ${ago(c.created_at)}`)),
        pill(CLIP_STATES, c.status))))
        : h('p', { class: 'muted' }, 'No clips yet. They appear here a minute or two after a moment is found.'));
    if (dels === null) { patch(postBox, h('p', { class: 'muted' }, 'Posting is not available yet.')); return; }
    const day = Date.now() / 1000 - 86400;
    const recent = dels.filter((d) => (d.created_at || 0) >= day && !d.test);
    const count = (...s) => recent.filter((d) => s.includes(d.status)).length;
    const held = dels.filter((d) => d.status === 'held').length;
    patch(postBox, [
      h('dl', { class: 'kv' },
        h('dt', {}, 'Posted'), h('dd', {}, String(count('published', 'private', 'draft'))),
        h('dt', {}, 'In progress'), h('dd', {}, String(count('queued', 'uploading', 'processing', 'waiting_language', 'uncertain'))),
        h('dt', {}, 'Failed'), h('dd', {}, String(count('failed'))),
        h('dt', {}, 'Waiting for you'), h('dd', {}, String(held))),
      held ? h('a', { class: 'small', href: '#/clips', help: 'Open the review queue to post waiting clips.' }, 'Review waiting clips') : null,
      h('p', { class: 'small muted', style: 'margin-top: 8px' }, 'Test destination posts are not counted.')]);
  }

  await Promise.all([loadChannels(), loadHealth().catch(() => {}), loadClips()]);
  const stops = [poll(loadChannels, 4000), poll(loadHealth, 4000), poll(loadClips, 15000)];
  return () => stops.forEach((s) => s());
}
