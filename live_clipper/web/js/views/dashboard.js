// Dashboard: what waits on you, then monitoring (every channel's state) beside clip work (recent clips, posting).

import { get, list, post } from '../api.js';
import {
  CHANNEL_STATES, CLIP_STATES, channelName, clipStatus, clipTitle, isTopicMode, pill, platformName, statusSource, svcError,
} from '../labels.js';
import { ago, busy, h, patch, poll, sentence, titleRow, toast } from '../ui.js';

// channels that need a look come first
const ORDER = ['needs_action', 'reconnecting', 'recording', 'waiting', 'offline', 'disabled'];
const rank = (c) => (ORDER.indexOf(c.state) + 1) || ORDER.length + 1;
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function channelRow(c, refresh) {
  const needs = c.state === 'needs_action';
  const name = channelName(c);
  const armed = Boolean(c.enabled); // a channel is watched exactly while it is armed
  const arm = armed
    ? h('button', { type: 'button', class: 'btn small', dataset: { key: `arm-${c.id}` },
      help: 'Stop watching this channel. Nothing is recorded until you start it again.' }, 'Stop watching')
    : h('button', { type: 'button', class: 'btn small', dataset: { key: `arm-${c.id}` },
      help: 'Start watching this channel for its stream.' }, 'Start watching');
  arm.addEventListener('click', busy(arm, async () => {
    try {
      await post(`/channels/${c.id}/${armed ? 'disarm' : 'arm'}`);
      await refresh();
    } catch (e) { toast(e.message, true); }
  }));
  return h('article', { class: `chan${needs ? ' needs' : ''}`, 'aria-labelledby': `ch-${c.id}`, dataset: { id: c.id } },
    h('div', { class: 'chan-main' },
      h('div', { class: 'grow' }, h('div', { class: 'name', id: `ch-${c.id}` }, name),
        h('div', { class: 'meta' }, `${platformName(c.platform)} · ${statusSource(c)}`),
        // recording whenever the channel is live is the default and needs no line
        isTopicMode(c) ? h('div', { class: 'meta' }, 'Records only while the stream title matches its format') : null,
        !needs && c.detail && c.state !== 'disabled' ? h('div', { class: 'meta' }, sentence(c.detail)) : null),
      pill(CHANNEL_STATES, c.state),
      h('div', { class: 'row' }, arm,
        h('a', { class: 'btn small ghost', href: `#/channels/${c.id}`, help: `Change ${name}'s link, format, destinations or turn it off.` }, 'Edit'))),
    needs ? h('div', { class: 'note', role: 'status' }, h('p', {}, c.detail || 'This channel needs your attention.'),
      c.fix ? h('p', { class: 'small' }, `To fix: ${c.fix}`) : null,
      c.fix_link ? h('a', { class: 'act', href: c.fix_link, help: 'Open the screen where this can be fixed.' }, 'Fix this') : null) : null);
}

function finding(channels) {
  const by = (s) => channels.filter((c) => c.state === s).length;
  const parts = [];
  if (by('needs_action')) parts.push(`${by('needs_action')} need${by('needs_action') > 1 ? '' : 's'} action`);
  if (by('recording')) parts.push(`${by('recording')} recording`);
  if (by('reconnecting')) parts.push(`${by('reconnecting')} reconnecting`);
  if (by('waiting')) parts.push(`${by('waiting')} waiting for a stream`);
  if (!channels.length) return 'No channels yet. Add one to start clipping.';
  return parts.length ? `${plural(channels.length, 'channel')}: ${parts.join(', ')}.` : 'All channels are offline.';
}

const head = (id, title, link) => h('div', { class: 'group-head' }, h('h2', { id }, title), link || null);

export async function render(main) {
  const sub = h('p', { class: 'finding', role: 'status' });
  const attention = h('section', { id: 'attention', class: 'group attention', 'aria-labelledby': 'at-title', hidden: true });
  const rows = h('div', { class: 'group', id: 'channel-cards' });
  const clipsBox = h('div', { class: 'group' });
  const postBox = h('div', { class: 'group' });
  const healthBox = h('div', { class: 'group' });
  main.append(
    titleRow('Dashboard', sub, h('a', { class: 'btn primary', href: '#/channels/new', help: 'Paste a streamer link to start watching a channel.' }, 'Add channel')),
    attention,
    h('div', { class: 'grid-main' },
      h('div', {},
        h('section', { 'aria-labelledby': 'ch-title' }, head('ch-title', 'Channels'), rows),
        h('section', { 'aria-labelledby': 'hl-title' },
          head('hl-title', 'App', h('a', { class: 'end small act', href: '#/status', help: 'Open the Status page for details and logs.' }, 'Details')),
          healthBox)),
      h('aside', { id: 'rail', 'aria-label': 'Clips and posting' },
        h('section', { 'aria-labelledby': 'rc-title' },
          head('rc-title', 'Recent clips', h('a', { class: 'end small act', href: '#/clips', help: 'Open the full clip library.' }, 'View all')),
          clipsBox),
        h('section', { 'aria-labelledby': 'ps-title' }, head('ps-title', 'Posting, last 24 hours'), postBox))));

  // what waits on the user, gathered from the loads below: clips to post, failed posts, parts that did not start
  const todo = { held: 0, failed: [], bad: 0 };
  function drawAttention() {
    const items = [];
    if (todo.held) {
      items.push(h('li', {}, h('div', { class: 'grow title' }, `${plural(todo.held, 'clip')} waiting for you to post`),
        h('a', { class: 'btn primary', href: '#/clips', dataset: { key: 'todo-held' }, help: 'Open the review queue to post waiting clips.' }, 'Review clips')));
    }
    if (todo.failed.length) {
      items.push(h('li', {}, h('div', { class: 'grow title' }, `${plural(todo.failed.length, 'post')} failed in the last 24 hours`),
        h('a', { class: 'btn', href: `#/clips/${todo.failed[0].clip_id}`, dataset: { key: 'todo-failed' },
          help: 'Open the clip whose post failed to see the error and try again.' }, 'See what failed')));
    }
    if (todo.bad) {
      items.push(h('li', {}, h('div', { class: 'grow title' }, `${plural(todo.bad, 'part')} of the app failed to start`),
        h('a', { class: 'btn', href: '#/status', dataset: { key: 'todo-bad' }, help: 'Open the Status page to see which part failed.' }, 'Open status')));
    }
    attention.hidden = !items.length;
    patch(attention, [h('h2', { id: 'at-title', class: 'sr-only' }, 'Waiting on you'), h('ul', { class: 'rows' }, items)]);
  }

  let names = {};
  async function loadChannels() {
    const channels = list(await get('/channels'), 'channels').sort((a, b) => rank(a) - rank(b));
    names = Object.fromEntries(channels.map((c) => [c.id, channelName(c)]));
    sub.textContent = finding(channels);
    patch(rows, channels.length ? channels.map((c) => channelRow(c, loadChannels))
      : h('div', { class: 'empty' }, h('strong', {}, 'No channels yet'),
        'Add a Twitch, Kick or YouTube channel and the app starts watching for its stream. ',
        h('a', { href: '#/channels/new', help: 'Paste a streamer link to start watching a channel.' }, 'Add your first channel')));
  }

  async function loadHealth() {
    const s = await get('/status');
    const bad = Object.entries(s.services || {}).filter(([, v]) => svcError(v));
    todo.bad = bad.length;
    drawAttention();
    patch(healthBox, h('div', {},
      h('p', {}, bad.length ? `${plural(bad.length, 'part')} of the app failed to start.` : 'All parts of the app are running.'),
      h('p', { class: 'small muted num' }, `Version ${s.version} · running for ${Math.round((s.uptime || 0) / 60)} min`),
      bad.length ? h('ul', { class: 'small' }, bad.map(([n, v]) => h('li', {}, `${n}: ${svcError(v)}`))) : null));
  }

  async function loadClips() {
    const [clips, dels] = await Promise.all([
      get('/clips?limit=5').then((d) => list(d, 'clips')).catch(() => null),
      get('/publish/deliveries').then((d) => list(d, 'deliveries')).catch(() => null)]);
    patch(clipsBox, clips === null ? h('p', { class: 'muted' }, 'Clips are not available yet.')
      : clips.length ? h('ul', { class: 'rows' }, clips.slice(0, 5).map((c) => h('li', {},
        h('div', { class: 'grow' }, h('a', { class: 'truncate rowlink', href: `#/clips/${c.id}`,
          help: 'Open this clip to preview, edit or post it.' }, clipTitle(c)),
        h('div', { class: 'small muted' }, `${names[c.channel_id] || ''} · ${ago(c.created_at)}`)),
        pill(CLIP_STATES, clipStatus(c)))))
        : h('p', { class: 'muted' }, 'No clips yet. They appear here a minute or two after a moment is found.'));
    if (dels === null) {
      todo.held = 0;
      todo.failed = [];
      drawAttention();
      patch(postBox, h('p', { class: 'muted' }, 'Posting is not available yet.'));
      return;
    }
    const day = Date.now() / 1000 - 86400;
    const recent = dels.filter((d) => (d.created_at || 0) >= day && !d.test);
    const count = (...s) => recent.filter((d) => s.includes(d.status)).length;
    const held = dels.filter((d) => d.status === 'held');
    todo.held = new Set(held.map((d) => d.clip_id)).size;
    todo.failed = recent.filter((d) => d.status === 'failed');
    drawAttention();
    const stat = (label, n) => h('div', {}, h('dt', {}, label), h('dd', {}, String(n)));
    patch(postBox, [
      h('dl', { class: 'stat' },
        stat('Posted', count('published', 'private', 'draft')),
        stat('In progress', count('queued', 'uploading', 'processing', 'waiting_language', 'uncertain')),
        stat('Failed', count('failed')),
        stat('Waiting for you', held.length)),
      h('p', { class: 'small muted' }, 'Each destination counts once. Finished test destination posts are left out.')]);
  }

  await loadChannels();
  await Promise.all([loadHealth().catch(() => {}), loadClips()]);
  const stops = [poll(loadChannels, 4000), poll(loadHealth, 4000), poll(loadClips, 15000)];
  return () => stops.forEach((s) => s());
}
