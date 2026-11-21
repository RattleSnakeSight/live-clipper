// Boot, hash router, health badge and first-run flow (wizard, then the guided tour).

import { appState, get, setUnauthorizedHandler } from './api.js';
import { clear, h, initHelp } from './ui.js';
import { maybeStartTour } from './tour.js';
import * as dashboard from './views/dashboard.js';
import * as channels from './views/channels.js';
import * as sources from './views/sources.js';
import * as clips from './views/clips.js';
import * as formats from './views/formats.js';
import * as presets from './views/presets.js';
import * as destinations from './views/destinations.js';
import * as settings from './views/settings.js';
import * as status from './views/status.js';
import * as guide from './views/guide.js';
import * as wizard from './views/wizard.js';

export const ROUTES = { dashboard, channels, sources, clips, formats, presets, destinations, settings, status, guide, setup: wizard };
const TITLES = {
  dashboard: 'Dashboard', channels: 'Channels', sources: 'Sources', clips: 'Clips', formats: 'Formats and rules',
  presets: 'Presets', destinations: 'Destinations', settings: 'Settings', status: 'Status', guide: 'Guide', setup: 'Setup',
};

const main = document.getElementById('main');
let cleanup = null;
let renderSeq = 0;
let setupDone = false;
let signedOut = false;

export function parseHash() {
  const parts = (location.hash.replace(/^#\/?/, '') || 'dashboard').split('/').map(decodeURIComponent);
  return { route: parts[0], params: parts.slice(1) };
}

export function navigate(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

export function markSetupDone() { setupDone = true; }

async function render() {
  if (signedOut) return;
  let { route, params } = parseHash();
  if (!setupDone && route !== 'setup') {
    location.replace('#/setup');
    return;
  }
  if (!ROUTES[route]) { route = 'dashboard'; params = []; }
  if (cleanup) { cleanup(); cleanup = null; }
  document.body.classList.toggle('setup-mode', route === 'setup');
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('menu-btn').setAttribute('aria-expanded', 'false');
  for (const a of document.querySelectorAll('.sidebar a')) {
    if (a.dataset.route === route) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  document.title = `${TITLES[route]} - Live Clipper`;
  // each render gets its own container, so a slow earlier render cannot write into the new page
  const view = h('div', { class: 'view' });
  clear(main, view);
  const seq = ++renderSeq;
  try {
    const done = (await ROUTES[route].render(view, params)) || null;
    if (seq !== renderSeq) { if (done) done(); return; }
    cleanup = done;
  } catch (e) {
    if (signedOut || seq !== renderSeq) return;
    clear(view, h('div', { class: 'title-row' }, h('div', {}, h('h1', {}, TITLES[route]))),
      h('p', { class: 'note bad', role: 'alert' }, `This page could not load: ${e.message}`),
      h('p', { class: 'small muted' }, 'Check the Status page, or reload this tab.'));
  }
  if (!document.querySelector('.tour-layer')) { // the tour keeps focus in its own dialog
    window.scrollTo(0, 0);
    main.focus({ preventScroll: true });
  }
  document.dispatchEvent(new CustomEvent('view:rendered', { detail: route }));
}

async function updateHealth() {
  const badge = document.getElementById('health-badge');
  try {
    const s = await get('/status');
    const errors = Object.entries(s.services || {}).filter(([, v]) => v && v.error);
    badge.className = `pill ${errors.length ? 'warn' : 'pos'}`;
    badge.textContent = errors.length ? `${errors.length} service${errors.length > 1 ? 's' : ''} with problems` : 'Running';
    badge.dataset.help = errors.length ? 'Open Status to see which part failed.' : 'All parts of the app are running.';
  } catch {
    badge.className = 'pill neg';
    badge.textContent = 'Not reachable';
  }
}

function showSignedOut() {
  signedOut = true;
  document.body.classList.add('setup-mode');
  clear(main, h('div', { class: 'wizard' },
    h('h1', {}, 'Open Live Clipper from its own link'),
    h('p', { class: 'help' }, 'This tab is not signed in. Run "live-clipper start" again: it opens a signed-in tab. '
      + 'The sign-in link only works on this computer.')));
}

async function boot() {
  initHelp();
  setUnauthorizedHandler(() => { if (!signedOut) showSignedOut(); });
  document.getElementById('menu-btn').addEventListener('click', (e) => {
    const open = document.getElementById('sidebar').classList.toggle('open');
    e.currentTarget.setAttribute('aria-expanded', String(open));
  });
  try {
    setupDone = (await appState('setup.completed')) === true;
  } catch {
    if (signedOut) return;
  }
  window.addEventListener('hashchange', render);
  await render();
  updateHealth();
  setInterval(() => { if (document.visibilityState === 'visible' && !signedOut) updateHealth(); }, 5000);
  if (setupDone) maybeStartTour();
  watchNotifications();
}

// Shows new app notifications as browser notifications while this tab is open (if the user allowed them).
async function watchNotifications() {
  let since = null;
  const tick = async () => {
    if (signedOut) return;
    try {
      const r = await get(`/notifications?since=${since ?? 0}`);
      if (since !== null && 'Notification' in window && Notification.permission === 'granted') {
        for (const n of r.items || []) new Notification(n.title, { body: n.body, tag: `lc-${n.id}` });
      }
      since = r.latest ?? since ?? 0;
    } catch { /* notifications service not available */ }
  };
  await tick();
  setInterval(tick, 15000);
}

boot();
