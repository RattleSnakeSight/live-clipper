// Destinations: posting mode, step-by-step setup per destination (from GET /api/destinations requirements),
// connect/disconnect, rights acknowledgement and enable. The test destination posts nothing.

import { ApiError, get, list, post, put } from '../api.js';
import { busy, choiceGroup, clear, confirmDialog, h, selectField, setError, switchField, textField, titleRow, toast } from '../ui.js';

const STATUS = {
  connected: ['Connected', 'pos'], disconnected: ['Not connected', 'neutral'], needs_reconnect: ['Reconnect needed', 'warn'],
  error: ['Error', 'neg'], unverified: ['Checking', 'accent'],
};
const STEP_NAMES = {
  app_credentials: 'Developer app keys saved', connect: 'Account connected',
  rights_acknowledgement: 'Posting rights confirmed', enable: 'Turned on',
};
const CRED_FIELDS = {
  tiktok: [['client_key', 'Client key', false], ['client_secret', 'Client secret', true]],
  default: [['client_id', 'Client ID', false], ['client_secret', 'Client secret', true]],
};

// Settings the publish API accepts per provider (PUT /destinations/{id}/settings). kind: select | text | number |
// bool (a Default / On / Off select) | list (comma separated). Empty means "use the built-in default".
const REDIRECT = { key: 'redirect_uri', kind: 'text', label: 'Redirect address override',
  help: 'Only change this if the provider refuses the default loopback address. Use the exact address registered in your developer app.' };
const DAILY = { key: 'daily_limit', kind: 'number', label: 'Uploads per day',
  help: 'The most clips this app posts here per day. 0 turns the limit off. Empty keeps the provider default.' };
const SETTINGS = {
  youtube: [
    { key: 'privacy', kind: 'select', label: 'Who can watch uploads', options: [['public', 'Anyone (public)'], ['unlisted', 'Anyone with the link'], ['private', 'Only you']],
      help: 'Visibility set on every upload. YouTube can still keep videos private until your project passes its audit. Empty means public.' },
    { key: 'category_id', kind: 'text', label: 'YouTube category number', help: 'The category the videos are filed under. 20 is Gaming, which is the default.' },
    { key: 'tags', kind: 'list', label: 'Extra tags', help: 'Tags added to every upload, separated by commas.' },
    DAILY, REDIRECT],
  tiktok: [
    { key: 'mode', kind: 'select', label: 'How clips are sent', options: [['inbox', 'To your TikTok inbox as drafts'], ['direct', 'Post directly']],
      help: 'Inbox saves a draft you finish in the TikTok app. Direct posts without opening the app and needs Direct Post enabled for your app. Empty means inbox.' },
    { key: 'privacy_level', kind: 'select', label: 'Who can watch direct posts', options: [['PUBLIC_TO_EVERYONE', 'Anyone'], ['MUTUAL_FOLLOW_FRIENDS', 'Friends'], ['FOLLOWER_OF_CREATOR', 'Followers'], ['SELF_ONLY', 'Only you']],
      help: 'Visibility of direct posts. Apps TikTok has not audited can only post privately, so the app falls back to Only you. Empty means Only you, or Anyone once the audit switch below is on.' },
    { key: 'audited', kind: 'bool', label: 'My TikTok app has passed the audit', help: 'Turn on only after TikTok approved your app for public posting. Until then every post stays private.' },
    { key: 'disable_comment', kind: 'bool', label: 'Turn off comments', help: 'Direct posts only: viewers cannot comment. Empty leaves comments on.' },
    { key: 'disable_duet', kind: 'bool', label: 'Turn off duets', help: 'Direct posts only: viewers cannot make a duet with the clip. Empty leaves duets on.' },
    { key: 'disable_stitch', kind: 'bool', label: 'Turn off stitches', help: 'Direct posts only: viewers cannot stitch the clip into their own. Empty leaves stitches on.' },
    DAILY, REDIRECT],
  instagram: [
    { key: 'login', kind: 'select', label: 'Login type', options: [['instagram', 'Instagram Login'], ['facebook', 'Facebook Login for Business']],
      help: 'Must match how your Meta app signs in. Facebook Login uploads the file itself and needs a linked Facebook Page. Instagram Login needs the public video address below. Empty means Instagram Login.' },
    { key: 'video_base_url', kind: 'text', label: 'Public video folder address (https)',
      help: 'Only needed for Instagram Login. Instagram downloads each video from a public web address, so give the https folder that serves your clip files. Facebook Login does not use it.' },
    { key: 'share_to_feed', kind: 'bool', label: 'Also show the Reel in the profile feed', help: 'Empty keeps the default, which is on.' },
    { key: 'target_account_id', kind: 'text', label: 'Instagram account number', help: 'Pin uploads to one account when your login can reach several. Digits only. Empty uses the first one found.' },
    REDIRECT],
};
const TRI = [['', 'Default'], ['true', 'On'], ['false', 'Off']];
const opts = (rows) => rows.map(([value, label]) => ({ value, label }));

function settingsForm(d, refresh) {
  const spec = SETTINGS[d.provider];
  if (!spec) return null;
  const cur = d.settings || {};
  const fields = spec.map((f) => {
    const val = cur[f.key];
    const base = { id: `st-${d.id}-${f.key}`, name: f.key, label: f.label, help: f.help };
    if (f.kind === 'select') return [f, selectField({ ...base, value: val ?? '', options: opts([['', 'Default'], ...f.options]) })];
    if (f.kind === 'bool') return [f, selectField({ ...base, value: val === undefined ? '' : String(val), options: opts(TRI) })];
    if (f.kind === 'number') return [f, textField({ ...base, type: 'number', min: 0, step: 1, value: val ?? '' })];
    if (f.kind === 'list') return [f, textField({ ...base, value: (val || []).join(', ') })];
    return [f, textField({ ...base, value: val ?? '', autocomplete: 'off', spellcheck: 'false' })];
  });
  const save = h('button', { type: 'submit', class: 'btn', help: 'Check and save these settings for this destination.' }, 'Save settings');
  const out = h('div', { role: 'status', 'aria-live': 'polite' });
  const form = h('form', { class: 'stack', novalidate: true, 'aria-label': `${d.label} settings`, dataset: { settings: d.provider } },
    h('h3', {}, 'Settings'), h('p', { class: 'small muted' }, 'Leave a field on Default or empty to use the built-in behaviour.'),
    h('div', { class: 'inline-fields' }, fields.map(([, f]) => f.el)), h('div', {}, save), out);
  form.addEventListener('submit', busy(save, async (e) => {
    e.preventDefault();
    clear(out);
    const body = {};
    for (const [f, fld] of fields) {
      setError(fld.el, '');
      const v = fld.input.value.trim();
      if (f.kind === 'bool') body[f.key] = v === '' ? '' : v === 'true';
      else if (f.kind === 'number') body[f.key] = v === '' ? '' : Number(v);
      else if (f.kind === 'list') body[f.key] = v === '' ? '' : v.split(',').map((t) => t.trim()).filter(Boolean);
      else body[f.key] = v;
    }
    try {
      await put(`/destinations/${d.id}/settings`, body);
      toast('Settings saved.');
      await refresh();
    } catch (ex) {
      const errs = ex instanceof ApiError ? ex.fields : {};
      let shown = false;
      for (const [f, fld] of fields) if (errs[f.key]) { setError(fld.el, errs[f.key]); shown = true; }
      if (!shown) clear(out, h('p', { class: 'error' }, ex.message));
    }
  }));
  return form;
}

export async function render(main) {
  const modeBox = h('section', { id: 'posting-mode', class: 'panel', 'aria-labelledby': 'pm-title' });
  const list_ = h('div', { class: 'stack', style: 'margin-top: var(--gap)' });
  main.append(titleRow('Destinations', 'The accounts clips are posted to. Nothing is posted until a destination is connected, confirmed and turned on.'),
    modeBox, list_);

  async function loadMode() {
    const m = await get('/publish/mode');
    const group = choiceGroup({ legend: 'Posting mode', value: m.mode, options: [
      { value: 'auto', label: 'Automatic', hint: 'Finished clips go to every connected, turned-on destination the channel allows. Nothing is posted before a destination is connected, confirmed and turned on.' },
      { value: 'manual', label: 'Manual', hint: 'Every clip waits in the review queue on the Clips page until you press Post.' }] });
    group.inputs.forEach((i) => i.addEventListener('change', async () => {
      try {
        await put('/publish/mode', { mode: i.value });
        toast(i.value === 'auto' ? 'Clips now post automatically.' : 'Clips now wait for your approval.');
      } catch (e) { toast(e.message, true); await loadMode(); }
    }));
    clear(modeBox, h('h2', { id: 'pm-title', class: 'sr-only' }, 'Posting mode'), group.el);
  }

  // the destinations picked during setup come first
  const chosen = await get('/setup/state').then((s) => (s.answers || {})['posting.destinations'] || []).catch(() => []);
  async function load() {
    const [rows, test] = await Promise.all([
      get('/destinations').then((d) => list(d, 'destinations')),
      get('/publish/test-mode').catch(() => null)]);
    const real = rows.filter((d) => !d.test);
    const mine = real.filter((d) => chosen.includes(d.provider));
    const other = real.filter((d) => !chosen.includes(d.provider));
    // the long requirements start open only for a destination picked in setup that is not connected yet
    clear(list_, testCard(rows.find((d) => d.test), test, load),
      mine.length ? h('h2', { class: 'group-label' }, 'Picked during setup') : null, mine.map((d) => card(d, load, true)),
      mine.length && other.length ? h('h2', { class: 'group-label' }, 'Other destinations') : null, other.map((d) => card(d, load, false)));
  }
  await Promise.all([loadMode().catch((e) => clear(modeBox, h('p', { class: 'error' }, e.message))), load()]);
}

function testCard(dest, test, refresh) {
  const on = test ? test.enabled : Boolean(dest && dest.enabled);
  const sw = switchField({ label: 'Use the test destination', checked: on,
    help: 'Clips go to a folder on this computer. Nothing is posted anywhere.',
    onchange: async (e) => {
      try { await put('/publish/test-mode', { enabled: e.target.checked }); await refresh(); } catch (ex) { toast(ex.message, true); }
    } });
  return h('section', { id: 'dest-test', class: 'panel tinted', 'aria-labelledby': 'dt-title' },
    h('header', {}, h('h2', { id: 'dt-title' }, 'Test destination: nothing is posted'),
      h('span', { class: `pill ${on ? 'pos' : 'neutral'}` }, on ? 'On' : 'Off')),
    h('p', { style: 'margin-bottom: 12px' }, 'Use it to try the whole flow safely. It never counts as a real post and needs no account.'), sw.el);
}

function list2(items) {
  return h('ul', {}, items.map((s) => h('li', {}, typeof s === 'string' ? s : s.text,
    s.verified === false ? h('span', { class: 'small muted' }, ' (not confirmed by the provider\'s docs)') : null)));
}

function requirements(d) {
  const r = d.requirements || {};
  const scopes = Array.isArray(r.scopes) ? r.scopes : Object.entries(r.scopes || {}).flatMap(([k, v]) => v.map((s) => `${s} (${k.replace('_', ' ')})`));
  const copy = h('button', { type: 'button', class: 'btn small', help: 'Copy this address to paste into the developer app settings.' }, 'Copy');
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(r.redirect_uri); toast('Address copied.'); } catch {
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(copy.previousElementSibling);
      sel.removeAllRanges();
      sel.addRange(range);
      toast('Press Ctrl+C (or Cmd+C) to copy the selected address.');
    }
  });
  const fails = Object.entries(r.fails_without || {});
  return h('div', { class: 'split', style: 'margin: 12px 0' },
    h('div', { class: 'stack' },
      h('div', {}, h('h3', {}, 'Account you need'), h('p', {}, r.account || '-')),
      h('div', {}, h('h3', {}, 'Cost'), h('p', {}, r.free ? 'Free' : 'May cost money; check the provider.')),
      (r.developer_app_steps || []).length ? h('div', {}, h('h3', {}, 'Set up your developer app'),
        h('ol', { class: 'steps-list' }, r.developer_app_steps.map((s) => h('li', {}, s.text,
          s.verified === false ? h('span', { class: 'small muted' }, ' (not confirmed by the provider\'s docs)') : null)))) : null,
      r.redirect_uri ? h('div', {}, h('h3', {}, 'Redirect address to register'),
        h('div', { class: 'copy-row' }, h('code', {}, r.redirect_uri), copy),
        r.redirect_note ? h('p', { class: 'small muted', style: 'margin-top: 4px' }, r.redirect_note) : null) : null,
      scopes.length ? h('div', {}, h('h3', {}, 'Permissions (scopes) to request'), list2(scopes)) : null),
    h('div', { class: 'stack' },
      (r.verification_audit || []).length ? h('div', {}, h('h3', {}, 'Verification and audit'), list2(r.verification_audit)) : null,
      r.quotas ? h('div', {}, h('h3', {}, 'Limits'), h('p', {}, r.quotas.summary),
        d.quota ? h('p', { class: 'small muted num' }, `Used today: ${d.quota.used_today} of ${d.quota.limit}`) : null) : null,
      fails.length ? h('div', {}, h('h3', {}, 'What fails without each step'),
        h('dl', { class: 'kv long' }, fails.map(([k, v]) => [h('dt', {}, k.replace(/_/g, ' ')), h('dd', {}, v)]))) : null,
      r.tos_rights ? h('div', {}, h('h3', {}, 'Terms and rights'), h('p', {}, r.tos_rights)) : null,
      (r.notes || []).length ? h('div', {}, h('h3', {}, 'Good to know'), list2(r.notes)) : null,
      (r.docs || []).length ? h('div', {}, h('h3', {}, 'Official documentation'),
        h('ul', {}, r.docs.map((u) => h('li', {}, h('a', { href: u, target: '_blank', rel: 'noopener noreferrer',
          help: 'Opens the provider\'s documentation in a new tab.' }, u.replace(/^https?:\/\//, '')))))) : null,
      r.as_of ? h('p', { class: 'small muted' }, `Checked against the provider docs on ${r.as_of}.`) : null));
}

function card(d, refresh, picked) {
  const [label, tone] = STATUS[d.status] || [d.status, 'neutral'];
  const out = h('div', { role: 'status', 'aria-live': 'polite' });
  const connected = d.status === 'connected';
  const steps = (d.setup && d.setup.steps) || [];

  // developer app keys
  const fields = (CRED_FIELDS[d.provider] || CRED_FIELDS.default).map(([name, lab, secret]) => textField({
    label: lab, name, type: secret ? 'password' : 'text', autocomplete: 'off',
    help: secret ? 'Stored encrypted on this computer and never shown again.' : 'Copy it from your developer app\'s credentials page.' }));
  const saveKeys = h('button', { type: 'submit', class: 'btn', help: 'Save these keys in the encrypted store on this computer.' }, 'Save keys');
  const keys = h('form', { class: 'stack', novalidate: true, 'aria-label': `${d.label} developer app keys` },
    h('h3', {}, 'Developer app keys'),
    (d.app_credentials_set || []).length ? h('p', { class: 'small muted' }, `Saved: ${d.app_credentials_set.join(', ')}. Enter new values to replace them.`) : null,
    h('div', { class: 'inline-fields' }, fields.map((f) => f.el)), h('div', {}, saveKeys));
  keys.addEventListener('submit', busy(saveKeys, async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(fields.map((f) => [f.input.name, f.input.value.trim()]).filter(([, v]) => v));
    if (!Object.keys(body).length) { clear(out, h('p', { class: 'error' }, 'Enter at least one key.')); return; }
    try { await put(`/destinations/${d.id}/credentials`, body); toast('Keys saved.'); await refresh(); } catch (ex) { clear(out, h('p', { class: 'error' }, ex.message)); }
  }));

  // instagram token
  let tokenForm = null;
  if (d.provider === 'instagram') {
    const tok = textField({ label: 'Long-lived access token', type: 'password', autocomplete: 'off',
      help: 'From the Meta app dashboard, Instagram API setup, Generate token. Stored encrypted on this computer.' });
    const send = h('button', { type: 'submit', class: 'btn', help: 'Check the token and connect the account.' }, 'Connect with token');
    tokenForm = h('form', { class: 'stack', novalidate: true, 'aria-label': 'Instagram access token' }, h('h3', {}, 'Or paste a token'), tok.el, h('div', {}, send));
    tokenForm.addEventListener('submit', busy(send, async (e) => {
      e.preventDefault();
      try { await post('/destinations/instagram/token', { access_token: tok.input.value.trim(), destination_id: d.id }); toast('Instagram connected.'); await refresh(); } catch (ex) {
        tok.el.querySelector('.error').textContent = ex.message;
      }
    }));
  }

  const connect = h('button', { type: 'button', class: connected ? 'btn' : 'btn primary',
    help: connected ? 'Check the connection again.' : 'Opens the provider\'s sign-in page in a new tab. Nothing is posted.' }, connected ? 'Check connection' : 'Connect');
  connect.addEventListener('click', busy(connect, async () => {
    try {
      const r = await post(`/destinations/${d.id}/connect`);
      if (r.authorize_url) {
        window.open(r.authorize_url, '_blank', 'noopener');
        clear(out, h('p', { class: 'note info' }, 'Approve access in the new tab, then come back. This page updates by itself.'));
        setTimeout(refresh, 4000);
      } else {
        toast(r.ok === false ? (r.detail || 'Not connected.') : `Connected${r.account ? ` as ${r.account}` : ''}.`, r.ok === false);
        await refresh();
      }
    } catch (ex) { clear(out, h('p', { class: 'error' }, ex.message)); }
  }));
  const disconnect = connected ? h('button', { type: 'button', class: 'btn danger', help: 'Forget this account. Posting to it stops.' }, 'Disconnect') : null;
  if (disconnect) {
    disconnect.addEventListener('click', async () => {
      if (!await confirmDialog({ title: `Disconnect ${d.label}?`, body: 'Posting to this account stops until you connect again.', ok: 'Disconnect', danger: true })) return;
      try { await post(`/destinations/${d.id}/disconnect`); await refresh(); } catch (ex) { toast(ex.message, true); }
    });
  }

  const ackId = `ack-${d.id}`;
  const ack = h('input', { type: 'checkbox', id: ackId, checked: d.rights_acknowledged,
    help: 'Required before posting: you own the clips or have the streamer\'s permission to post them here.' });
  ack.addEventListener('change', async () => {
    try { await put(`/destinations/${d.id}/acknowledge`, { acknowledged: ack.checked }); await refresh(); } catch (ex) { toast(ex.message, true); ack.checked = !ack.checked; }
  });
  const why = !connected ? 'Connect the account first.' : !d.rights_acknowledged ? 'Confirm your posting rights first.' : null;
  const enable = switchField({ label: 'Post to this destination', checked: d.enabled, disabled: Boolean(why) && !d.enabled, disabledReason: why,
    help: 'When on, clips can be posted here, automatically or when you press Post.',
    onchange: async (e) => {
      try { await put(`/destinations/${d.id}/enabled`, { enabled: e.target.checked }); await refresh(); } catch (ex) { toast(ex.message, true); e.target.checked = !e.target.checked; }
    } });

  return h('section', { id: `dest-${d.provider}`, class: 'panel', 'aria-labelledby': `dn-${d.id}` },
    h('header', {}, h('h2', { id: `dn-${d.id}` }, d.label),
      h('span', { class: `pill ${tone}` }, label, d.account ? ` as ${d.account}` : ''),
      d.enabled ? h('span', { class: 'pill pos' }, 'On') : null),
    d.status_detail ? h('p', { class: 'note', style: 'margin-bottom: 8px' }, d.status_detail) : null,
    steps.length ? h('ol', { class: 'row', 'aria-label': 'Setup progress', style: 'list-style: none; padding: 0; margin: 0 0 8px' },
      steps.map((s) => h('li', {}, h('span', { class: `pill ${s.done ? 'pos' : 'neutral'}` }, `${s.done ? 'Done' : 'To do'}: ${STEP_NAMES[s.id] || s.id}`)))) : null,
    h('details', { open: picked && !connected },
      h('summary', { help: 'Show or hide what this destination needs.' }, h('strong', {}, 'What you need and how to set it up')),
      requirements(d)),
    // in the order the setup steps above are done: keys, connect, rights, on. Optional settings come last.
    h('div', { class: 'split', style: 'margin-top: 12px' }, keys, tokenForm),
    h('div', { class: 'row', style: 'margin-top: 16px' }, connect, disconnect),
    out,
    h('div', { class: 'stack', style: 'margin-top: 16px' },
      h('label', { class: 'choice', for: ackId }, ack, h('span', {}, 'I own these clips or have the streamer\'s permission to post them on this account.')),
      enable.el),
    h('div', { style: 'margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border)' }, settingsForm(d, refresh)));
}

