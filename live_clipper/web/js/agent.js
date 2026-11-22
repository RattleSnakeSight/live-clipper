// "Ask my agent": the customer picks where a platform's keys are, and the paired helper of the AI agent that
// installed Live Clipper looks them up and saves them (GET/POST /api/agent*). Keys are never shown here.

import { ApiError, get, post } from './api.js';
import { ago, busy, choiceGroup, h, textField } from './ui.js';

const STATES = {
  pending: ['Waiting for your agent', 'warn'], claimed: ['Your agent is looking', 'accent'],
  saving: ['Saving', 'accent'], done: ['Saved', 'pos'], missing: ['Keys missing', 'warn'],
  invalid: ['Not accepted', 'neg'], failed: ['Could not finish', 'neg'], cancelled: ['Cancelled', 'neutral'],
  expired: ['No answer', 'neutral'],
};
const OPEN = ['pending', 'claimed', 'saving'];

// One fetch shared by the panels of a page; at most a few seconds old, so a pairing made meanwhile shows up.
let overview = null;
let fetchedAt = 0;
export async function agentOverview(fresh = false) {
  if (fresh || !overview || Date.now() - fetchedAt > 3000) {
    overview = get('/agent').catch(() => null);
    fetchedAt = Date.now();
  }
  return overview;
}

// What the helper can and cannot do, said once wherever the panel appears.
const ABOUT = 'The AI agent that installed Live Clipper (Claude Code or Codex) runs a small helper on this computer. '
  + 'When you ask, the helper reads only the keys named below, only from the place you choose, and saves them '
  + 'here. It cannot create a developer app, approve a sign-in or accept terms for you; those stay with you.';

// Where the customer types the keys instead: right below the panel (Sources, Destinations), on that page
// (Settings), or there once setup is finished (the wizard keeps its answers but cannot be left half done).
function manualHint(target, manual) {
  const [kind, platform] = target.split(':');
  const href = kind === 'source' ? '#/sources' : '#/destinations';
  const page = kind === 'source' ? 'Sources' : 'Destinations';
  if (manual === 'below') return h('p', { class: 'small muted' }, 'You can always enter the keys yourself below.');
  if (manual === 'after-setup') {
    return h('p', { class: 'small muted' }, `You can always enter the keys yourself on ${page} once setup is finished. `
      + 'Your setup answers are already saved.');
  }
  return h('p', { class: 'small muted' }, 'You can always ',
    h('a', { href: `${href}`, dataset: { platform }, help: `Open ${page}, where the keys can be typed in by hand.` },
      `enter the keys yourself on ${page}`), '.');
}

// target: "source:twitch" | "destination:tiktok" ...; onDone runs after keys were saved.
// manual: 'below' | 'link' | 'after-setup' (see manualHint).
export async function agentPanel(target, onDone = () => {}, manual = 'below') {
  const box = h('section', { class: 'agent-panel', 'aria-label': 'Ask my agent', dataset: { target } });
  const info = await agentOverview();
  const spec = info && info.targets.find((t) => t.target === target);
  if (!info || !spec) return null;
  box.append(h('h3', {}, 'Let your agent set this up'), h('p', { class: 'small muted' }, ABOUT));
  if (!info.paired) {
    box.append(h('p', { class: 'note info' }, 'No agent is paired with this Live Clipper yet. Ask the agent that '
      + 'installed it to pair (`live-clipper agent pair`).'), manualHint(target, manual));
    return box;
  }
  const who = info.paired.agent_label;
  const online = info.helper.online;
  box.append(h('p', { class: 'small' }, `Paired with ${who}. `, online ? 'Its helper is running.'
    : `Its helper is not running right now: ask ${who} to start it (\`live-clipper agent serve\`). A request waits 10 minutes.`));
  const names = spec.fields.map((f) => f.env).join(', ');
  const src = choiceGroup({ legend: 'Where are the keys?', name: `src-${target.replace(':', '-')}`, value: 'env',
    help: 'The helper looks only here, and only for the names listed.',
    options: [{ value: 'env', label: 'Environment variables', hint: names },
      { value: 'file', label: 'A file I choose', hint: `KEY=VALUE lines or JSON with ${names}` },
      { value: 'keyring', label: 'A credential manager entry', hint: 'Needs credential manager support on this computer' }] });
  const where = textField({ label: 'File or entry name', autocomplete: 'off', spellcheck: 'false',
    help: 'For a file, its full path. For a credential manager entry, its service name. Not a key itself.' });
  const syncWhere = () => { where.el.hidden = src.value() === 'env'; };
  src.inputs.forEach((i) => i.addEventListener('change', syncWhere));
  syncWhere();
  const askBtn = h('button', { type: 'submit', class: 'btn primary',
    help: `Ask ${who} to look up the ${spec.label} keys in the place chosen above and save them here.` }, 'Ask my agent');
  const status = h('div', { role: 'status', 'aria-live': 'polite', class: 'agent-status' });
  const form = h('form', { novalidate: true }, src.el, where.el, h('div', { class: 'row', style: 'margin-top: 12px' }, askBtn));
  box.append(form, status);

  let timer = null;
  let lastDone = null;
  let shown = false;
  async function show() {
    clearTimeout(timer);
    if (box.isConnected) shown = true;
    else if (shown) return; // the page moved on: stop polling
    const list = (await get(`/agent/requests?target=${encodeURIComponent(target)}`).catch(() => ({ requests: [] }))).requests;
    const r = list[0];
    status.replaceChildren();
    if (!r) { timer = setTimeout(show, 4000); return; }
    const [label, tone] = STATES[r.status] || [r.status, 'neutral'];
    const cancel = OPEN.includes(r.status) && r.status !== 'saving'
      ? h('button', { type: 'button', class: 'btn small', help: 'Withdraw this request. Nothing is saved after you cancel.' }, 'Cancel') : null;
    if (cancel) cancel.addEventListener('click', busy(cancel, async () => { await post(`/agent/requests/${r.id}/cancel`); await show(); }));
    status.append(h('div', { class: 'row' }, h('span', { class: `pill ${tone}` }, h('span', { class: 'dot', 'aria-hidden': 'true' }), label),
      h('span', { class: 'small muted' }, `${spec.label}, asked ${ago(r.created_at)}`), cancel),
    r.detail ? h('p', { class: 'small' }, r.detail) : null,
    r.saved.length ? h('p', { class: 'small' }, `Saved: ${r.saved.join(', ')}. The values are stored encrypted and not shown.`) : null,
    r.missing.length ? h('p', { class: 'small' }, `Not found: ${r.missing.join(', ')}. Add them to that place and ask again.`) : null,
    !OPEN.includes(r.status) && r.status !== 'done' ? manualHint(target, manual) : null);
    askBtn.disabled = OPEN.includes(r.status);
    if (r.status === 'done' && lastDone !== r.id) { lastDone = r.id; onDone(); }
    timer = setTimeout(show, OPEN.includes(r.status) ? 1500 : 6000);
  }
  form.addEventListener('submit', busy(askBtn, async (e) => {
    e.preventDefault();
    where.el.querySelector('.error').textContent = '';
    try {
      await post('/agent/requests', { target, source: { kind: src.value(), ref: src.value() === 'env' ? '' : where.input.value.trim() } });
      await show();
    } catch (ex) {
      where.el.hidden = false;
      where.el.querySelector('.error').textContent = ex instanceof ApiError ? ex.message : String(ex);
    }
  }));
  const r0 = (await get(`/agent/requests?target=${encodeURIComponent(target)}`).catch(() => ({ requests: [] }))).requests[0];
  if (r0 && r0.status === 'done') lastDone = r0.id; // an old success does not refresh the page again
  show();
  return box;
}
