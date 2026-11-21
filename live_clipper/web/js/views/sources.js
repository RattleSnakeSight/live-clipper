// Sources: connect Twitch, Kick and YouTube, and show exactly what each connection enables.

import { ApiError, del, get, list, post } from '../api.js';
import { busy, clear, confirmDialog, h, textField, titleRow, toast } from '../ui.js';

export async function render(main) {
  const box = h('div', { id: 'source-list', class: 'stack' });
  main.append(titleRow('Sources', 'Connections to the streaming sites. Each one shows what it turns on.'),
    h('p', { class: 'note', role: 'note', style: 'margin-bottom: 20px' },
      'Connecting lets Live Clipper see when a stream is live and read its chat. It does not give you the right to record, '
      + 'edit or repost anyone\'s stream. Get the streamer\'s permission before clipping their content.'),
    box);

  async function load() {
    const rows = list(await get('/sources'), 'sources');
    clear(box, rows.map((s) => card(s, load)));
  }
  await load();
}

function card(s, refresh) {
  const out = h('div', { role: 'status', 'aria-live': 'polite' });
  const enables = h('table', {},
    h('caption', { class: 'sr-only' }, `What the ${s.name} connection enables`),
    h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Feature'), h('th', { scope: 'col' }, 'Available'), h('th', { scope: 'col' }, 'How'))),
    h('tbody', {}, (s.enables || []).map((e) => h('tr', {},
      h('th', { scope: 'row', style: 'color: var(--text); font-size: inherit' }, e.label),
      h('td', {}, h('span', { class: `pill ${e.available ? 'pos' : 'neutral'}` }, e.available ? 'Yes' : 'No')),
      h('td', {}, e.detail || '')))));
  let action;
  if (s.connected) {
    action = h('button', { type: 'button', class: 'btn', help: `Remove the ${s.name} connection. Channels fall back to a slower stream check.` }, 'Disconnect');
    action.addEventListener('click', async () => {
      const ok = await confirmDialog({ title: `Disconnect ${s.name}?`, body: 'Live status falls back to a slower stream check and chat stops.', ok: 'Disconnect' });
      if (!ok) return;
      try { await del(`/sources/${s.platform}`); toast(`${s.name} disconnected.`); await refresh(); } catch (e) { toast(e.message, true); }
    });
  } else {
    const fields = (s.connect && s.connect.fields) || [];
    const inputs = fields.map((f) => textField({ label: f.label, type: f.secret ? 'password' : 'text', help: f.help || `Enter the ${f.label}.`,
      autocomplete: 'off', name: f.name }));
    const kind = (s.connect && s.connect.kind) || 'credentials';
    const label = kind === 'device' ? `Connect ${s.name} with a code` : kind === 'oauth' ? `Sign in to ${s.name}` : `Save ${s.name} key`;
    const go = h('button', { type: 'submit', class: 'btn primary',
      help: kind === 'device' ? 'Shows a code to enter on the site. Nothing is posted.'
        : kind === 'oauth' ? 'Opens the site\'s sign-in page in a new tab.' : 'Save the key, stored encrypted on this computer.' }, label);
    action = h('form', { class: 'stack', novalidate: true }, inputs.map((i) => i.el), go);
    action.addEventListener('submit', busy(go, async (e) => {
      e.preventDefault();
      const body = { fields: Object.fromEntries(fields.map((f, i) => [f.name, inputs[i].input.value.trim()])) };
      try {
        const r = await post(`/sources/${s.platform}/connect`, body);
        if (r.device) {
          clear(out, h('div', { class: 'note info' },
            h('p', {}, 'Open ', h('a', { href: r.device.verification_uri, target: '_blank', rel: 'noopener noreferrer',
              help: 'Opens the site in a new tab to enter the code.' }, r.device.verification_uri),
            ' and enter this code:'),
            h('p', { style: 'font-size: 22px; font-weight: 700; letter-spacing: 0' }, h('code', {}, r.device.user_code)),
            h('p', { class: 'small' }, 'This page updates when you are done.')));
          setTimeout(refresh, 4000);
        } else if (r.authorize_url) {
          window.open(r.authorize_url, '_blank', 'noopener');
          clear(out, h('p', { class: 'note info' }, 'Finish signing in in the new tab, then come back here.'));
          setTimeout(refresh, 4000);
        } else {
          toast(`${s.name} connected.`);
          await refresh();
        }
      } catch (ex) {
        const f = ex instanceof ApiError ? ex.fields : {};
        fields.forEach((fd, i) => { inputs[i].el.querySelector('.error').textContent = f[fd.name] || ''; });
        if (!Object.keys(f).length) clear(out, h('p', { class: 'error' }, ex.message));
      }
    }));
  }
  return h('section', { class: 'panel', 'aria-labelledby': `src-${s.platform}`, id: `source-${s.platform}` },
    h('header', {}, h('h2', { id: `src-${s.platform}` }, s.name),
      h('span', { class: `pill ${s.connected ? 'pos' : 'neutral'}` }, s.connected ? `Connected${s.account ? ` as ${s.account}` : ''}` : 'Not connected')),
    s.note ? h('p', { class: 'muted', style: 'margin-bottom: 12px' }, s.note) : null,
    h('div', { class: 'table-wrap', style: 'margin-bottom: 16px' }, enables), action, out);
}
