// Sources: connect Twitch, Kick and YouTube, and show exactly what each connection enables (GET /api/sources).

import { ApiError, del, get, list, post } from '../api.js';
import { PLATFORMS } from '../labels.js';
import { busy, clear, confirmDialog, h, sentence, textField, titleRow, toast } from '../ui.js';
import { agentPanel } from '../agent.js';

const F = {
  client_id: ['client_id', 'Client ID', false, 'From your developer app on the platform\'s developer site.'],
  client_secret: ['client_secret', 'Client secret', true, 'Stored encrypted on this computer and never shown again.'],
  client_secret_opt: ['client_secret', 'Client secret (if the app has one)', true, 'Stored encrypted on this computer. Leave empty for a public client.'],
  api_key: ['api_key', 'API key', true, 'A YouTube Data API v3 key from the Google Cloud console. Stored encrypted.'],
  webhook: ['webhook_public_url', 'Public https address for chat', false, 'An https address that forwards to the local Kick webhook listener. Leave empty to turn Kick chat off.'],
};

// Ways to connect each platform (POST /api/sources/{platform}/connect {method, ...fields}).
const METHODS = {
  twitch: [
    { method: 'credentials', title: 'App credentials', fields: [F.client_id, F.client_secret], button: 'Save Twitch keys',
      help: 'The app asks Twitch for an app token. No sign-in page and no redirect address.' },
    { method: 'device', title: 'Sign in with a code', fields: [F.client_id], button: 'Get a Twitch code',
      help: 'For a public client: shows a code to enter on Twitch. Also lets the app read chat as you. Nothing is posted.' }],
  kick: [
    { method: 'credentials', title: 'App credentials', fields: [F.client_id, F.client_secret], button: 'Save Kick keys',
      help: 'The app asks Kick for an app token. Nothing to approve.' },
    { method: 'oauth', title: 'Sign in with Kick', fields: [F.client_id, F.client_secret], button: 'Sign in to Kick', redirect: true,
      help: 'Opens Kick\'s sign-in page in a new tab. Register the redirect address below in your Kick app first.' }],
  youtube: [
    { method: 'credentials', title: 'API key', fields: [F.api_key], button: 'Save YouTube key',
      help: 'Checks the key with one cheap request, then stores it encrypted on this computer.' },
    { method: 'oauth', title: 'Sign in with Google', fields: [F.client_id, F.client_secret_opt], button: 'Sign in to Google', redirect: true,
      help: 'Opens Google\'s sign-in page in a new tab. Use an OAuth client of type Desktop app.' }],
};

export async function render(main) {
  const box = h('div', { id: 'source-list', class: 'stack' });
  const captureNote = h('p', { class: 'small', style: 'margin-top: 4px' });
  main.append(titleRow('Sources', 'Connections to the streaming sites. Each one shows what it turns on.'),
    h('div', { class: 'note', role: 'note', style: 'margin-bottom: var(--gap)' },
      h('p', {}, 'Connecting lets Live Clipper see when a stream is live and read its chat. It does not give you the right to record, '
        + 'edit or repost anyone\'s stream. Get the streamer\'s permission before clipping their content.'),
      captureNote),
    box);
  const chosen = await get('/setup/state').then((s) => (s.answers || {})['platforms.enabled'] || []).catch(() => []);
  let timer = null;

  async function load() {
    const rows = list(await get('/sources'), 'sources');
    const mine = rows.filter((s) => chosen.includes(s.platform));
    const other = rows.filter((s) => !chosen.includes(s.platform));
    // every platform is captured the same way, so that notice is shown once above instead of on every card
    const notes = new Set(rows.map((s) => (s.capture || {}).note || ''));
    const shared = notes.size === 1 ? [...notes][0] : '';
    captureNote.textContent = shared;
    captureNote.hidden = !shared;
    clear(box, mine.map((s) => card(s, load, shared)),
      mine.length && other.length ? h('h2', { class: 'group-label' }, 'Other platforms') : null,
      other.map((s) => card(s, load, shared)));
    clearTimeout(timer);
    if (rows.some((s) => s.pending && s.pending.kind === 'device' && s.pending.state === 'waiting')) timer = setTimeout(load, 4000);
  }
  await load();
  return () => clearTimeout(timer);
}

function row(label, available, how, detail) {
  return h('li', {},
    h('div', { class: 'grow' }, h('div', { class: 'title' }, label), h('div', {}, how || ''),
      detail ? h('div', { class: 'small muted' }, detail) : null),
    h('span', { class: `pill ${available ? 'pos' : 'neutral'}` }, available ? 'Available' : 'Not available'));
}

function connectForm(s, m, out, refresh) {
  const name = PLATFORMS[s.platform] || s.platform;
  const inputs = m.fields.map(([field, label, secret, help]) => textField({ label, name: field, help,
    type: secret ? 'password' : 'text', autocomplete: 'off' }));
  const go = h('button', { type: 'submit', class: 'btn primary', help: m.help }, m.button);
  const form = h('form', { class: 'stack', novalidate: true, 'aria-label': `${name}: ${m.title}` },
    h('h3', {}, m.title), h('p', { class: 'small muted' }, m.help),
    m.redirect && s.redirect_uri ? h('div', {}, h('p', { class: 'small' }, 'Redirect address to register:'),
      h('div', { class: 'copy-row' }, h('code', {}, s.redirect_uri))) : null,
    inputs.map((i) => i.el), h('div', {}, go));
  form.addEventListener('submit', busy(go, async (e) => {
    e.preventDefault();
    const body = { method: m.method };
    inputs.forEach((i) => { body[i.input.name] = i.input.value.trim(); });
    inputs.forEach((i) => { i.el.querySelector('.error').textContent = ''; });
    try {
      const r = await post(`/sources/${s.platform}/connect`, body);
      if (r.kind === 'device') {
        await refresh();
      } else if (r.authorize_url) {
        window.open(r.authorize_url, '_blank', 'noopener');
        clear(out, h('p', { class: 'note info' }, 'Finish signing in in the new tab, then come back here.'));
        setTimeout(refresh, 4000);
      } else {
        toast(`${name} connected.`);
        await refresh();
      }
    } catch (ex) {
      const msg = ex instanceof ApiError ? ex.message : String(ex);
      const bad = inputs.find((i) => msg.includes(i.input.name));
      if (bad) { bad.el.querySelector('.error').textContent = msg; bad.input.focus(); } else clear(out, h('p', { class: 'error' }, msg));
    }
  }));
  return form;
}

function webhookForm(s, out, refresh) {
  const [field, label, , help] = F.webhook;
  const input = textField({ label, name: field, help, type: 'url', autocomplete: 'off', placeholder: 'https://example.com/kick' });
  const go = h('button', { type: 'submit', class: 'btn', help: 'Save the address. Kick chat starts after the app restarts.' }, 'Save chat address');
  const form = h('form', { class: 'stack', novalidate: true, 'aria-label': 'Kick chat webhook address' },
    h('h3', {}, 'Kick chat (optional)'), input.el, h('div', {}, go));
  form.addEventListener('submit', busy(go, async (e) => {
    e.preventDefault();
    input.el.querySelector('.error').textContent = '';
    try {
      await post('/sources/kick/connect', { method: 'credentials', webhook_public_url: input.input.value.trim() });
      toast('Kick chat address saved.');
      await refresh();
    } catch (ex) { input.el.querySelector('.error').textContent = ex.message; }
  }));
  return form;
}

// the panel loads on its own, so the card can draw at once
function agentSlot(target, refresh) {
  const slot = h('div', { class: 'agent-slot' });
  agentPanel(target, refresh).then((p) => p && slot.append(p));
  return slot;
}

function card(s, refresh, sharedNote) {
  const name = PLATFORMS[s.platform] || s.platform;
  const out = h('div', { role: 'status', 'aria-live': 'polite' });
  const live = s.live_status || {};
  const chat = s.chat || {};
  const cap = s.capture || {};
  const enables = h('ul', { class: 'rows', 'aria-label': `What the ${name} connection enables` },
    row('Live status', true, live.available ? `Official API: ${live.how}` : sentence(live.how),
      [live.detail, live.fix].filter(Boolean).join('. ')),
    row('Chat', chat.available, chat.how, chat.detail),
    row('Capture', true, cap.tool, cap.note === sharedNote ? '' : cap.note));
  const pend = s.pending && s.pending.kind === 'device' ? s.pending : null;
  const pending = pend ? h('div', { class: 'note info' },
    pend.state === 'waiting' ? [
      h('p', {}, 'Open ', h('a', { href: pend.verification_uri, target: '_blank', rel: 'noopener noreferrer',
        help: 'Opens the site in a new tab to enter the code.' }, pend.verification_uri), ' and enter this code:'),
      h('p', { style: 'font-size: 22px; font-weight: 700; letter-spacing: 0' }, h('code', {}, pend.user_code)),
      h('p', { class: 'small' }, 'This page updates when you are done.')]
      : h('p', {}, pend.detail || 'The sign-in did not finish. Start it again.')) : null;
  const saved = (s.fields || []).filter((f) => f !== 'scope');
  let disconnect = null;
  if (s.connected || saved.length) {
    disconnect = h('button', { type: 'button', class: 'btn', help: `Remove the saved ${name} keys and sign-in. Channels fall back to the stream probe.` }, 'Disconnect');
    disconnect.addEventListener('click', async () => {
      const ok = await confirmDialog({ title: `Disconnect ${name}?`, body: 'Live status falls back to the slower stream probe.', ok: 'Disconnect' });
      if (!ok) return;
      try { await del(`/sources/${s.platform}`); toast(`${name} disconnected.`); await refresh(); } catch (e) { toast(e.message, true); }
    });
  }
  const forms = (METHODS[s.platform] || []).map((m) => connectForm(s, m, out, refresh));
  if (s.platform === 'kick') forms.push(webhookForm(s, out, refresh));
  return h('section', { class: 'panel', 'aria-labelledby': `src-${s.platform}`, id: `source-${s.platform}` },
    h('header', {}, h('h2', { id: `src-${s.platform}` }, name),
      h('span', { class: `pill ${s.connected ? 'pos' : 'neutral'}` }, s.connected ? 'Connected' : 'Not connected')),
    saved.length ? h('p', { class: 'small muted', style: 'margin-bottom: 8px' }, `Saved: ${saved.join(', ')}.`) : null,
    h('div', { style: 'margin-bottom: 12px' }, enables), pending,
    h('details', { open: !s.connected },
      h('summary', { help: `Show or hide the ways to connect ${name}.` }, h('strong', {}, s.connected ? 'Connect again or change keys' : 'Connect')),
      agentSlot(`source:${s.platform}`, refresh),
      h('div', { class: 'split', style: 'margin-top: 12px' }, forms)),
    disconnect ? h('div', { class: 'row', style: 'margin-top: 12px' }, disconnect) : null, out);
}
