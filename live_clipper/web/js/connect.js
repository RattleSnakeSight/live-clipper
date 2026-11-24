// Shared pieces of the two account pages: Sources ("Watch a stream") and Destinations ("Post my clips").
// The instructions themselves come from the API (live_clipper/sources/guide.py, publish/requirements.py).

import { get, list } from './api.js';
import { announce, clear, h, nextId, textField, toast } from './ui.js';

// An official page, opened in a new tab so the customer can come back to the same step. Only https addresses.
export function extLink(l) {
  return h('a', { href: /^https:\/\//.test(String(l.url)) ? l.url : '#', target: '_blank', rel: 'noopener noreferrer' },
    l.label, h('span', { class: 'sr-only' }, ' (opens in a new tab)'), h('span', { 'aria-hidden': 'true' }, ' ↗'));
}

// "1. Do this (link)" lists for a provider portal.
export function stepList(steps, label) {
  return h('ol', { class: 'steps-list', 'aria-label': label }, steps.map((s) => h('li', {}, s.text,
    s.link ? [' ', extLink(s.link)] : null,
    s.verified === false ? h('span', { class: 'small muted' }, ' (the provider\'s page may word this differently)') : null)));
}

// An address of this running installation that a provider needs, with Copy and the reason it is needed.
export function copyRow(address, { title, why }) {
  const code = h('code', {}, address);
  const btn = h('button', { type: 'button', class: 'btn small', 'aria-label': `Copy: ${title}`,
    help: 'Copy this address so you can paste it on the provider\'s page.' }, 'Copy');
  btn.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(address); toast('Address copied.'); } catch {
      const range = document.createRange();
      range.selectNodeContents(code);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast('The address is selected. Copy it with your keyboard or the copy menu.');
    }
  });
  return h('div', { class: 'callback' }, h('h4', {}, title), h('div', { class: 'copy-row' }, code, btn),
    h('p', { class: 'small muted' }, why));
}

// One credential field: Live Clipper's label, why it is asked, and where the value is on the provider's site.
// f: {name, label, provider_label, why, where, link, secret, required}
export function guidedField(f) {
  const id = nextId('cf');
  const fld = textField({ id, name: f.name, label: f.required === false ? `${f.label} (optional)` : f.label,
    help: f.why, type: f.secret ? 'password' : 'text', autocomplete: 'off', spellcheck: 'false' });
  const where = h('p', { class: 'small where', id: `${id}-where` },
    h('strong', {}, f.provider_label ? `On the provider's page it is called "${f.provider_label}". ` : ''),
    f.where, f.link ? [' ', extLink(f.link)] : null);
  fld.input.before(where);
  fld.input.setAttribute('aria-describedby', `${id}-help ${id}-where ${id}-err`);
  return fld;
}

// "client_id, client_secret" as the customer knows them: the labels of the fields that hold them.
export function savedNames(names, fields) {
  const label = Object.fromEntries(fields.map((f) => [f.name, f.label]));
  return names.map((n) => label[n] || n.replace(/_/g, ' ')).join(', ');
}

// Scopes, quotas and uncommon errors: one click away. sections: [{title, items: [string | {text, verified}]}]
export function moreDetails(summary, sections, extra = null) {
  const shown = sections.filter((s) => s && s.items && s.items.length);
  if (!shown.length && !extra) return null;
  return h('details', { class: 'more' },
    h('summary', { help: 'Show or hide the details most people never need.' }, summary),
    shown.map((s) => h('div', {}, h('h4', {}, s.title), h('ul', {}, s.items.map((i) => h('li', {},
      typeof i === 'string' ? i : i.text,
      i.verified === false ? h('span', { class: 'small muted' }, ' (not confirmed on the provider\'s pages)') : null))))),
    extra);
}

// What went wrong and what to do next. recover: [{when, action, match?, link?}]; detail: the app's own message.
export function nextAction(recover, detail) {
  const text = String(detail || '').toLowerCase();
  return (recover || []).find((r) => r.match && r.match.some((m) => text.includes(m.toLowerCase()))) || null;
}

export function recoverList(recover) {
  return h('dl', { class: 'recover' }, recover.map((r) => [h('dt', {}, r.when),
    h('dd', {}, r.action, r.link ? [' ', extLink(r.link)] : null)]));
}

// Shown after a provider's sign-in page was opened: what to do there, and a way in if no tab appeared.
export function signInNote(url) {
  return h('p', { class: 'note info' }, 'Finish in the new tab, then come back. This page updates when you return. '
    + 'If you close that tab or press Cancel there, nothing changes and you can press the button again. ',
  h('a', { href: /^https:\/\//.test(String(url)) ? url : '#', target: '_blank', rel: 'noopener noreferrer' },
    'No new tab? Open the sign-in page here.'));
}

// The two reasons to connect an account, shown on both pages so each connection is visible from the other.
// known: rows the page already fetched ({sources} or {dests}).
export async function intentSwitch(current, known = {}) {
  const [sources, dests] = await Promise.all([
    known.sources || get('/sources').then((d) => list(d, 'sources')).catch(() => null),
    known.dests || get('/destinations').then((d) => list(d, 'destinations')).catch(() => null)]);
  const count = (rows, ok) => {
    if (!rows) return null;
    const n = rows.filter(ok).length;
    return h('span', { class: `pill ${n ? 'pos' : 'neutral'}` }, n ? `${n} of ${rows.length} connected` : 'Nothing connected yet');
  };
  const item = (id, href, page, title, sub, pill) => h('a', { href, class: 'intent', 'aria-current': current === id ? 'page' : null,
    help: `Open the ${page} page.` }, h('strong', {}, title), h('span', { class: 'small' }, `${page}: ${sub}`), pill);
  return h('nav', { class: 'intents', 'aria-label': 'Account pages' },
    item('watch', '#/sources', 'Sources', 'Watch a stream', 'Twitch, Kick and YouTube Live. Works without an account; connecting one gives faster live detection and chat.',
      count(sources, (s) => s.connected)),
    item('post', '#/destinations', 'Destinations', 'Post my clips', 'YouTube Shorts, TikTok and Instagram Reels. Optional: without it, clips are kept as files.',
      count(dests && dests.filter((d) => !d.test), (d) => d.status === 'connected')));
}

// A route the customer picked is remembered in this browser, so coming back from a provider's site resumes there.
export function rememberedRoute(key, routes, current = '') {
  let saved = null;
  try { saved = localStorage.getItem(`lc.route.${key}`); } catch { /* storage unavailable: use the default */ }
  const r = routes.find((x) => x.id === saved) || routes.find((x) => x.id === current) || routes.find((x) => x.recommended) || routes[0];
  return r ? r.id : '';
}

export function rememberRoute(key, id) {
  try { localStorage.setItem(`lc.route.${key}`, id); } catch { /* storage unavailable */ }
}

// Another way to connect was chosen: draw its panel and keep what was typed into fields both ways share.
export function swapPanel(panel, build) {
  const typed = Object.fromEntries([...panel.querySelectorAll('.route-panel > form input')].map((i) => [i.name, i.value]));
  clear(panel, build());
  for (const i of panel.querySelectorAll('.route-panel > form input')) if (typed[i.name]) i.value = typed[i.name];
}

// #/sources/kick or #/destinations/youtube opens on that card. The router resets scroll and focus after a page
// is drawn, so this waits for it.
export function focusCard(id) {
  if (!id) return;
  document.addEventListener('view:rendered', () => {
    const el = document.getElementById(id);
    if (!el) return;
    const connect = el.querySelector('details.connect');
    if (connect) connect.open = true;
    const head = el.querySelector('h2');
    head.tabIndex = -1;
    head.focus({ preventScroll: true });
    el.scrollIntoView({ block: 'start' });
  }, { once: true });
}

const sectionKey = (d) => `${(d.closest('section[id]') || {}).id}|${d.querySelector('summary').textContent}`;

// Draw the cards again without losing the customer's place: sections they opened stay open (the numbered steps
// follow the new state instead), and keyboard focus goes back into the card it was in.
export function redraw(box, children) {
  const open = new Set([...box.querySelectorAll('details[open]:not(.step)')].map(sectionKey));
  const card = box.contains(document.activeElement) ? document.activeElement.closest('section[id]') : null;
  clear(box, children);
  for (const d of box.querySelectorAll('details:not(.step)')) if (open.has(sectionKey(d))) d.open = true;
  const again = card && document.getElementById(card.id);
  const to = again && (again.querySelector('details.step[data-state="next"] > summary') || again.querySelector('h2'));
  if (to) {
    if (to.tagName === 'H2') to.tabIndex = -1;
    to.focus({ preventScroll: true });
  }
}

// Say a connection's new state out loud when it changed since the last look (a sign-in finished in another tab).
export function announceChanges(seen, now) {
  for (const [name, label] of now) {
    if (seen.has(name) && seen.get(name) !== label) announce(`${name}: ${label}`);
    seen.set(name, label);
  }
}

// Look again when the customer comes back to this tab (from a provider's site), unless something they typed or
// chose is not saved yet. Picking a way to connect is not unsaved work.
export function refreshOnReturn(root, load) {
  root.addEventListener('input', (e) => { if (!e.target.closest('.way')) e.target.dataset.dirty = '1'; });
  const onFocus = () => { if (root.isConnected && !root.querySelector('[data-dirty]')) load(false).catch(() => {}); };
  window.addEventListener('focus', onFocus);
  return () => window.removeEventListener('focus', onFocus);
}
