// Sources ("Watch a stream"): connect Twitch, Kick and YouTube, and show exactly what each connection enables.
// GET /api/sources carries the instructions for each way to connect (live_clipper/sources/guide.py).

import { ApiError, del, get, list, post } from '../api.js';
import { PLATFORMS } from '../labels.js';
import { busy, choiceGroup, clear, confirmDialog, h, sentence, setError, textField, titleRow, toast } from '../ui.js';
import { agentPanel } from '../agent.js';
import { announceChanges, copyRow, focusCard, guidedField, intentSwitch, moreDetails, redraw, refreshOnReturn,
  rememberRoute, rememberedRoute, savedNames, signInNote, stepList, swapPanel } from '../connect.js';

const stateOf = (s) => ((s.live_status || {}).detail ? ['Needs attention', 'warn']
  : s.connected ? ['Connected', 'pos'] : ['No account connected', 'neutral']);

export async function render(main, params) {
  const box = h('div', { id: 'source-list', class: 'stack' });
  const captureNote = h('p', { class: 'small', style: 'margin-top: 4px' });
  const intents = h('div', {});
  main.append(titleRow('Sources', 'Watch a stream: connections to the streaming sites. Each one shows what it turns on.'),
    intents,
    h('div', { class: 'note', role: 'note', style: 'margin-bottom: var(--gap)' },
      h('p', {}, 'Connecting lets Live Clipper see when a stream is live and read its chat. It does not give you the right to record, '
        + 'edit or repost anyone\'s stream. Get the streamer\'s permission before clipping their content.'),
      captureNote),
    box);
  const chosen = await get('/setup/state').then((s) => (s.answers || {})['platforms.enabled'] || []).catch(() => []);
  const seen = new Map();
  let timer = null;
  let last = '';

  // force: the customer did something, so draw again. Otherwise only when the answer changed, so nothing they
  // opened or focused is disturbed by a look that found nothing new.
  async function load(force = true) {
    const rows = list(await get('/sources'), 'sources');
    clearTimeout(timer);
    if (rows.some((s) => s.pending && s.pending.state === 'waiting')) timer = setTimeout(() => load(false).catch(() => {}), 4000);
    const sig = JSON.stringify(rows);
    if (!force && sig === last) return;
    last = sig;
    const mine = rows.filter((s) => chosen.includes(s.platform));
    const other = rows.filter((s) => !chosen.includes(s.platform));
    // every platform is captured the same way, so that notice is shown once above instead of on every card
    const notes = new Set(rows.map((s) => (s.capture || {}).note || ''));
    const shared = notes.size === 1 ? [...notes][0] : '';
    captureNote.textContent = shared;
    captureNote.hidden = !shared;
    redraw(box, [mine.map((s) => card(s, load, shared, true)),
      mine.length && other.length ? h('h2', { class: 'group-label' }, 'Other platforms') : null,
      other.map((s) => card(s, load, shared, !mine.length))]);
    clear(intents, await intentSwitch('watch', { sources: rows }));
    announceChanges(seen, rows.map((s) => [PLATFORMS[s.platform] || s.platform, stateOf(s)[0]]));
  }
  await load();
  focusCard(params && params[0] ? `source-${params[0]}` : null);
  const stop = refreshOnReturn(box, load);
  return () => { clearTimeout(timer); stop(); };
}

function row(label, available, how, detail) {
  return h('li', {},
    h('div', { class: 'grow' }, h('div', { class: 'title' }, label), h('div', {}, how || ''),
      detail ? h('div', { class: 'small muted' }, detail) : null),
    h('span', { class: `pill ${available ? 'pos' : 'neutral'}` }, available ? 'Available' : 'Not available'));
}

// One way to connect: the portal steps, only the fields this way needs, the address to register, one action.
function routePanel(s, r, refresh) {
  const name = PLATFORMS[s.platform] || s.platform;
  const head = [h('h3', {}, r.title), h('p', {}, r.summary),
    h('dl', { class: 'kv long facts' }, h('dt', {}, 'Account'), h('dd', {}, r.needs), h('dt', {}, 'Cost'), h('dd', {}, r.cost))];
  if (!r.method) { // nothing to connect: the customer adds the channel instead
    return h('div', { class: 'route-panel', dataset: { route: r.id } }, head,
      h('p', {}, h('a', { class: 'btn', href: '#/channels/new', help: 'Open Channels to paste the channel link. No key is needed.' }, r.button)),
      moreDetails('Limits and details', r.more || []));
  }
  const inputs = r.fields.map(guidedField);
  const go = h('button', { type: 'submit', class: 'btn primary', help: r.after }, r.button);
  const out = h('div', { role: 'status', 'aria-live': 'polite' }); // right under the button it answers
  const agent = h('details', { class: 'more' }, h('summary', { help: 'Show or hide the agent helper for these keys.' }, 'Or let my agent fill these in'));
  if (r.agent) agentPanel(`source:${s.platform}`, refresh, 'below', 'h4').then((p) => { if (p) agent.append(p); else agent.remove(); });
  const form = h('form', { class: 'stack', novalidate: true, 'aria-label': `${name}: ${r.title}` },
    h('h4', {}, 'Enter the values in Live Clipper'), inputs.map((i) => i.el), h('div', {}, go),
    h('p', { class: 'small muted' }, r.after), out);
  form.addEventListener('submit', busy(go, async (e) => {
    e.preventDefault();
    const body = { method: r.method };
    inputs.forEach((i) => { body[i.input.name] = i.input.value.trim(); setError(i.el, ''); });
    clear(out);
    let res;
    try { res = await post(`/sources/${s.platform}/connect`, body); } catch (ex) {
      const msg = ex instanceof ApiError ? ex.message : String(ex);
      const bad = inputs.find((i) => msg.includes(i.input.name)) || (inputs.length === 1 ? inputs[0] : null);
      if (bad) { setError(bad.el, msg); bad.input.focus(); } else clear(out, h('p', { class: 'error' }, msg));
      return;
    }
    inputs.forEach((i) => { i.input.value = ''; delete i.input.dataset.dirty; }); // sent; never kept on the page
    if (res.authorize_url) {
      window.open(res.authorize_url, '_blank', 'noopener');
      clear(out, signInNote(res.authorize_url));
      return;
    }
    if (res.kind !== 'device') toast(`${name} connected.`);
    try { await refresh(); } catch (ex) { toast(ex.message, true); }
    if (res.kind === 'device') { // the code to type is now on the card: take the customer to it
      const code = document.querySelector(`#source-${s.platform} .pending`);
      if (code) code.focus();
    }
  }));
  return h('div', { class: 'route-panel', dataset: { route: r.id } }, head,
    h('h4', {}, `On ${r.portal}`), stepList(r.steps, `Steps on ${r.portal}`),
    r.callback && s.redirect_uri ? copyRow(s.redirect_uri, r.callback) : null,
    form, r.agent ? agent : null,
    moreDetails('Limits, permissions and uncommon errors', r.more || []));
}

// Kick chat is an optional extra on top of either way to connect: Kick only delivers chat to a public address.
function kickChat(s, refresh) {
  const c = s.chat_setup;
  const input = textField({ label: 'Public https address for chat', name: 'webhook_public_url', type: 'url', autocomplete: 'off',
    placeholder: 'https://example.com/webhooks/kick', help: c.field_help });
  const go = h('button', { type: 'submit', class: 'btn', help: 'Save the address. Kick chat starts after Live Clipper restarts.' }, 'Save chat address');
  const form = h('form', { class: 'stack', novalidate: true, 'aria-label': 'Kick chat webhook address' }, input.el, h('div', {}, go));
  form.addEventListener('submit', busy(go, async (e) => {
    e.preventDefault();
    setError(input.el, '');
    try {
      await post('/sources/kick/connect', { method: 'credentials', webhook_public_url: input.input.value.trim() });
      delete input.input.dataset.dirty;
      toast('Kick chat address saved. Kick chat starts after Live Clipper restarts.');
      await refresh();
    } catch (ex) { setError(input.el, ex.message); }
  }));
  return h('details', { class: 'more', id: 'kick-chat' },
    h('summary', { help: 'Show or hide the optional Kick chat setup.' }, 'Kick chat (optional)'),
    h('h3', { class: 'sr-only' }, 'Kick chat'),
    h('p', {}, c.summary), stepList(c.steps, 'Steps for Kick chat'),
    c.listener ? copyRow(c.listener, { title: 'Local address your tunnel forwards to', why: c.listener_why }) : null,
    form);
}

function card(s, refresh, sharedNote, open) {
  const name = PLATFORMS[s.platform] || s.platform;
  const live = s.live_status || {};
  const chat = s.chat || {};
  const cap = s.capture || {};
  const about = s.about || {};
  const routes = s.routes || [];
  const enables = h('ul', { class: 'rows', 'aria-label': `What the ${name} connection enables` },
    row('Live status', true, live.available ? `Official API: ${live.how}` : sentence(live.how), live.auth || ''),
    row('Chat', chat.available, chat.how, chat.detail),
    row('Capture', true, cap.tool, cap.note === sharedNote ? '' : cap.note));
  const pend = s.pending && s.pending.kind === 'device' ? s.pending : null;
  const site = pend && /^https:\/\//.test(String(pend.verification_uri)) ? pend.verification_uri : null;
  const pending = pend ? h('div', { class: 'note info pending', tabindex: -1 },
    pend.state === 'waiting' ? [
      h('p', {}, 'Open ', site ? h('a', { href: site, target: '_blank', rel: 'noopener noreferrer' }, site,
        h('span', { class: 'sr-only' }, ' (opens in a new tab)')) : String(pend.verification_uri), ' and enter this code:'),
      h('p', { style: 'font-size: 22px; font-weight: 700; letter-spacing: 0' }, h('code', {}, pend.user_code)),
      h('p', { class: 'small' }, 'This page updates when you are done.')]
      : h('p', {}, pend.detail || 'The sign-in did not finish. Start it again.')) : null;
  const saved = (s.fields || []).filter((f) => !/scope|token|expires/.test(f)); // the values typed in, not sign-in state
  let disconnect = null;
  if (s.connected || saved.length) {
    disconnect = h('button', { type: 'button', class: 'btn', help: `Remove the saved ${name} keys and sign-in. Channels fall back to the stream probe.` }, 'Disconnect');
    disconnect.addEventListener('click', async () => {
      const ok = await confirmDialog({ title: `Disconnect ${name}?`, body: 'Live status falls back to the slower stream probe.', ok: 'Disconnect' });
      if (!ok) return;
      try { await del(`/sources/${s.platform}`); toast(`${name} disconnected.`); await refresh(); } catch (e) { toast(e.message, true); }
    });
  }
  // keys the platform accepted and keys it now refuses are different states; the pill says which
  const [label, tone] = stateOf(s);
  const state = live.detail
    ? h('p', { class: 'note', role: 'note' }, `${sentence(live.detail)}. `, h('strong', {}, 'What to do: '), `${live.fix}.`)
    : h('p', { class: 'small' }, s.connected ? s.how : about.without);

  const panel = h('div', {});
  const show = (id) => swapPanel(panel, () => routePanel(s, routes.find((r) => r.id === id) || routes[0], refresh));
  const picked = rememberedRoute(`source.${s.platform}`, routes, s.route);
  const choice = choiceGroup({ legend: `How do you want to watch ${name}?`, name: `route-${s.platform}`, value: picked,
    options: routes.map((r) => ({ value: r.id, label: r.title, hint: r.pick })) });
  choice.el.classList.add('way');
  choice.inputs.forEach((i) => i.addEventListener('change', () => { rememberRoute(`source.${s.platform}`, i.value); show(i.value); }));
  show(picked);

  return h('section', { class: 'panel', 'aria-labelledby': `src-${s.platform}`, id: `source-${s.platform}` },
    h('header', {}, h('h2', { id: `src-${s.platform}` }, name), h('span', { class: `pill ${tone}` }, label)),
    h('p', {}, about.what), state,
    saved.length ? h('p', { class: 'small muted' }, `Saved on this computer: ${savedNames(saved, routes.flatMap((r) => r.fields))}. Values are never shown.`) : null,
    h('div', { style: 'margin: 12px 0' }, enables), pending,
    h('details', { open: open && !s.connected, class: 'connect' },
      h('summary', { help: `Show or hide the ways to connect ${name}.` }, h('strong', {}, s.connected ? 'Connect again or change keys' : `Connect ${name}`)),
      choice.el, panel,
      s.chat_setup ? kickChat(s, refresh) : null),
    disconnect ? h('div', { class: 'row', style: 'margin-top: 12px' }, disconnect) : null);
}
