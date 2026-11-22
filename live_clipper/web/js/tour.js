// Guided tour: a spotlight over each screen with one plain line per step, plus a list of every control in
// the highlighted area (taken from the controls' own data-help text, so it never drifts from the UI).
// Shown once after setup; the result is stored in the app database (tour.state, tour.version).

import { appState, get, list, setAppState } from './api.js';
import { h } from './ui.js';

export const TOUR_VERSION = 1;

const first = async (path, key) => {
  try { const rows = list(await get(path), key); return rows.length ? rows[0].id : null; } catch { return null; }
};

// route may be a string or an async function returning a hash (or null when the screen has nothing to show).
export const STEPS = [
  { route: '#/dashboard', title: 'Welcome to Live Clipper',
    text: 'This tour shows every screen in about two minutes. Skip it any time and replay it from Settings.' },
  { route: '#/dashboard', target: '#channel-cards', title: 'Channel list',
    text: 'Each row shows whether a channel is offline, waiting, recording, reconnecting, needs action or disabled, and where that status comes from.' },
  { route: '#/dashboard', target: '.chan.needs', title: 'When a channel needs you',
    text: 'A channel marked Needs action says what went wrong and links straight to the fix.' },
  { route: '#/dashboard', target: '#rail', title: 'Clips and posting',
    text: 'The newest clips and what was posted in the last 24 hours. Clips that wait for you are listed above the channels.' },
  { route: '#/channels', target: '#channel-list', title: 'Channels',
    text: 'Start or stop watching a channel, edit it, or remove it.' },
  { route: '#/channels/new', target: '#channel-form', title: 'Add a channel',
    text: 'Paste a streamer link. The platform is detected for you; then pick when to watch and where clips may go.' },
  { route: '#/sources', target: '#source-list', title: 'Sources',
    text: 'Connecting a platform gives faster live status and chat. It never gives you the right to record or repost a stream.' },
  { route: '#/clips', target: '#review-queue', title: 'Review queue',
    text: 'In manual posting mode, finished clips wait here until you press Post for a destination. Clips a rule holds wait here too.' },
  { route: '#/clips', target: '#clip-library', title: 'Clip library',
    text: 'Every clip with its status: cutting, rendering, ready, held for review or failed. Filter by channel, status or title.' },
  { route: async () => { const id = await first('/clips', 'clips'); return id === null ? null : `#/clips/${id}`; },
    target: '#clip-editor', title: 'Fix a clip',
    text: 'Preview a clip, trim it, change its title, captions, framing or preset, render it again, approve it and see where it was posted.' },
  { route: '#/formats', target: '#format-list', title: 'Formats',
    text: 'A format decides which moments become clips. Duplicate the built-in one to make your own.' },
  { route: async () => { const id = await first('/formats', 'formats'); return id === null ? null : `#/formats/${encodeURIComponent(id)}`; },
    target: '#doc-editor', title: 'Edit a format',
    text: 'Change any field in the form, or edit the raw JSON. Errors show next to the field before anything is saved.' },
  { route: '#/formats', target: '#rule-list-panel', title: 'Rules',
    text: 'Rules change settings only for some clips, for example one platform, game or streamer. They run from top to bottom.' },
  { route: '#/formats', target: '#precedence', title: 'Which setting wins',
    text: 'Built-in values, then your format, then your rules in order, then changes you make to one clip.' },
  { route: '#/formats', target: '#preview-tool', title: 'Preview the result',
    text: 'Pick a channel and a situation to see every final value and which layer set it.' },
  { route: '#/presets', target: '#preset-list', title: 'Presets',
    text: 'A preset decides how a clip looks: size, framing, captions and loudness.' },
  { route: '#/destinations', target: '#posting-mode', title: 'Posting mode',
    text: 'Automatic posts every finished clip to the destinations you connected and turned on. Manual waits for you in the review queue.' },
  { route: '#/destinations', target: '#dest-youtube', title: 'Setting up a destination',
    text: 'Each destination lists the exact steps it needs and what fails without each one, including the address to register.' },
  { route: '#/destinations', target: '#dest-test', title: 'Test destination',
    text: 'Posts to a folder on this computer. Nothing is posted anywhere, so it is safe for trying things out.' },
  { route: '#/settings', target: '#settings-storage', title: 'Storage',
    text: 'Where clips are kept, how much disk the app may use and what is cleaned up first.' },
  { route: '#/settings', target: '#settings-processing', title: 'Processing',
    text: 'Whether speech recognition runs on the processor or a graphics card, and how many streams may be recorded at once.' },
  { route: '#/settings', target: '#settings-notify', title: 'Notifications and startup',
    text: 'Browser notifications, an optional webhook, and whether the app starts when you log in.' },
  { route: '#/settings', target: '#settings-help', title: 'Replay this tour',
    text: 'Replay the tutorial or run setup again from here. Version and update steps are here too.' },
  { route: '#/status', target: '#status-main', title: 'Status',
    text: 'Health of each part of the app, restarts, disk space and recent log lines with secrets hidden.' },
  { route: '#/guide', target: '#guide-main', title: 'Guide',
    text: 'The full guide, stored with the app, so it works without internet.' },
  { route: '#/dashboard', title: 'You are ready',
    text: 'Add your first channel to start. You can replay this tour from Settings at any time.' },
];

const CONTROLS = 'button, input:not([type=hidden]), select, textarea, a.btn';
let active = null;

function visible(el) {
  return el.offsetParent !== null || el.getClientRects().length > 0;
}

function controlsIn(target) {
  const seen = new Set();
  const out = [];
  for (const el of target.querySelectorAll(CONTROLS)) {
    if (!el.dataset.help || !visible(el)) continue;
    let label = el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].textContent)
      || (el.tagName === 'INPUT' ? '' : el.textContent);
    if (!label && (el.type === 'checkbox' || el.type === 'radio')) label = (el.closest('label') || {}).textContent || label;
    label = (label || '').replace(/\s+/g, ' ').trim();
    // one line per kind of control: four channel cards have four Edit buttons that do the same thing
    if (!label || seen.has(label)) continue;
    seen.add(label);
    out.push([label, el.dataset.help]);
  }
  return out.slice(0, 14);
}

async function waitFor(selector, ms = 800) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const el = document.querySelector(selector);
    if (el && visible(el)) return el;
    await new Promise((r) => setTimeout(r, 60));
  }
  return null;
}

async function go(hash) {
  if (location.hash === hash) return;
  const rendered = new Promise((r) => document.addEventListener('view:rendered', r, { once: true }));
  location.hash = hash;
  await Promise.race([rendered, new Promise((r) => setTimeout(r, 3000))]);
}

export async function maybeStartTour() {
  if (active) return;
  try {
    const state = await appState('tour.state');
    if (state === null || state === undefined) startTour();
  } catch { /* server not reachable: try again next launch */ }
}

export function startTour() {
  if (active) return;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const returnTo = location.hash && !location.hash.startsWith('#/setup') ? location.hash : '#/dashboard';
  const layer = h('div', { class: 'tour-layer' });
  const shade = h('div', { class: 'tour-shade' });
  const spot = h('div', { class: 'tour-spot', hidden: true });
  const card = h('div', { class: 'tour-card', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'tour-title',
    'aria-describedby': 'tour-text' });
  if (reduce) spot.style.transition = 'none';
  layer.append(shade, spot, card);
  document.body.append(layer);
  let index = 0;
  let seq = 0;

  async function finish(state) {
    active = null;
    layer.remove();
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', place);
    try {
      await setAppState('tour.state', state);
      await setAppState('tour.version', TOUR_VERSION);
    } catch { /* stays null and shows again next time */ }
    await go(returnTo);
    document.getElementById('main').focus({ preventScroll: true });
  }

  let target = null;
  let targetSel = null;
  function place() {
    const pad = 6;
    if (target && !document.contains(target) && targetSel) target = document.querySelector(targetSel); // re-rendered by polling
    if (target && visible(target)) {
      const r = target.getBoundingClientRect();
      const top = Math.max(4, r.top - pad);
      const bottom = Math.min(innerHeight - 4, r.bottom + pad);
      Object.assign(spot.style, { top: `${top}px`, left: `${Math.max(4, r.left - pad)}px`,
        width: `${Math.min(innerWidth - 8, r.width + pad * 2)}px`, height: `${Math.max(24, bottom - top)}px` });
      spot.hidden = false;
      shade.hidden = true;
      // below the target, else above it, else beside it (narrower if needed), so the card does not cover what it
      // explains. Only when none of those fit does it sit over the bottom of the screen.
      card.style.width = '';
      let cw = card.offsetWidth;
      const ch = card.offsetHeight;
      const phone = innerWidth < 600;
      const side = Math.max(innerWidth - r.right - 24, r.left - 24);
      let cy = bottom + 12;
      let cx = Math.min(Math.max(8, r.left), innerWidth - cw - 8);
      if (phone) {
        cy = Math.max(8, innerHeight - ch - 12);
        cx = (innerWidth - cw) / 2;
      } else if (cy + ch > innerHeight - 8) {
        if (top - ch - 12 >= 8) cy = top - ch - 12;
        else if (side >= 300) {
          cw = Math.min(cw, side);
          card.style.width = `${cw}px`;
          cx = innerWidth - r.right - 24 >= r.left - 24 ? r.right + 12 : r.left - cw - 12;
          cy = Math.max(8, Math.min(top, innerHeight - card.offsetHeight - 12));
        } else cy = Math.max(8, innerHeight - ch - 12);
      }
      Object.assign(card.style, { top: `${cy}px`, left: `${cx}px` });
    } else {
      card.style.width = ''; // a card narrowed to fit beside the last target gets its full width back
      spot.hidden = true;
      shade.hidden = false;
      Object.assign(card.style, { top: `${Math.max(8, (innerHeight - card.offsetHeight) / 2)}px`,
        left: `${(innerWidth - card.offsetWidth) / 2}px` });
    }
  }

  async function show(i) {
    const my = ++seq;
    index = i;
    const step = STEPS[i];
    const hash = typeof step.route === 'function' ? await step.route() : step.route;
    if (my !== seq) return;
    if (hash) await go(hash);
    targetSel = step.target || null;
    target = step.target && hash ? await waitFor(step.target) : null;
    if (my !== seq) return;
    // a target taller than half the screen starts just under the top bar, so the card below hides less of it
    if (target) {
      const r = target.getBoundingClientRect();
      const above = r.height > innerHeight / 2 ? 72 : (innerHeight - r.height) / 2;
      window.scrollTo({ top: scrollY + r.top - above, behavior: reduce ? 'auto' : 'smooth' });
    }
    const items = target ? controlsIn(target) : [];
    const last = i === STEPS.length - 1;
    card.replaceChildren(
      h('p', { class: 'count' }, `Step ${i + 1} of ${STEPS.length}`),
      h('h2', { id: 'tour-title' }, step.title),
      h('p', { id: 'tour-text' }, step.text),
      items.length ? h('ul', { 'aria-label': 'Controls here' },
        items.map(([label, help]) => h('li', {}, h('strong', {}, label), `: ${help}`))) : null,
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn ghost', help: 'Close the tour. It will not open again unless you replay it from Settings.',
          onclick: () => finish('skipped') }, 'Skip tour'),
        i > 0 ? h('button', { type: 'button', class: 'btn', help: 'Go back one step.', onclick: () => show(i - 1) }, 'Back') : null,
        h('button', { type: 'button', class: 'btn primary next', help: last ? 'Finish the tour.' : 'Go to the next step.',
          onclick: () => (last ? finish('completed') : show(i + 1)) }, last ? 'Finish' : 'Next')));
    requestAnimationFrame(place);
    setTimeout(place, reduce ? 0 : 350); // after smooth scrolling settles
    card.querySelector('.next').focus({ preventScroll: true });
  }

  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); finish('skipped'); return; }
    if (e.key === 'Tab') {
      const f = [...card.querySelectorAll('button')];
      if (!f.length) return;
      const at = f.indexOf(document.activeElement);
      e.preventDefault();
      f[(at + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
    }
  }
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', place);
  active = { show };
  show(0);
}

export const tourActive = () => Boolean(active);
