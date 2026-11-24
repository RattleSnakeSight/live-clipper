// Destinations ("Post my clips"): posting mode, then one card per destination that walks through its setup one
// step at a time (GET /api/destinations carries the instructions: live_clipper/publish/requirements.py).
// Connecting or checking an account never posts anything. The test destination posts nothing at all.

import { ApiError, get, list, post, put } from '../api.js';
import { busy, choiceGroup, clear, confirmDialog, h, selectField, setError, switchField, textField, titleRow, toast } from '../ui.js';
import { agentPanel } from '../agent.js';
import { announceChanges, copyRow, extLink, focusCard, guidedField, intentSwitch, moreDetails, nextAction, recoverList, redraw,
  refreshOnReturn, rememberRoute, rememberedRoute, savedNames, signInNote, stepList, swapPanel } from '../connect.js';

const STATUS = {
  connected: ['Connected', 'pos'], disconnected: ['Not connected', 'neutral'], needs_reconnect: ['Sign in again', 'warn'],
  error: ['Could not verify', 'neg'], unverified: ['Checking the account', 'accent'],
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
    h('p', { class: 'small muted' }, 'Leave a field on Default or empty to use the built-in behaviour.'),
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

export async function render(main, params) {
  const modeBox = h('section', { id: 'posting-mode', class: 'panel', 'aria-labelledby': 'pm-title' });
  const list_ = h('div', { class: 'stack', style: 'margin-top: var(--gap)' });
  const intents = h('div', {});
  main.append(titleRow('Destinations', 'Post my clips: the accounts clips are posted to. Nothing is posted until a destination is connected, confirmed and turned on.'),
    intents, modeBox, list_);

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
  const seen = new Map();
  let last = '';
  // force: the customer did something, so draw again. Otherwise only when the answer changed, so nothing they
  // opened or focused is disturbed by a look that found nothing new.
  async function load(force = true) {
    const [rows, test] = await Promise.all([
      get('/destinations').then((d) => list(d, 'destinations')),
      get('/publish/test-mode').catch(() => null)]);
    const sig = JSON.stringify([rows, test]);
    if (!force && sig === last) return;
    last = sig;
    const real = rows.filter((d) => !d.test);
    const mine = real.filter((d) => chosen.includes(d.provider));
    const other = real.filter((d) => !chosen.includes(d.provider));
    redraw(list_, [testCard(rows.find((d) => d.test), test, load),
      mine.length ? h('h2', { class: 'group-label' }, 'Picked during setup') : null, mine.map((d) => card(d, load)),
      mine.length && other.length ? h('h2', { class: 'group-label' }, 'Other destinations') : null, other.map((d) => card(d, load))]);
    clear(intents, await intentSwitch('post', { dests: rows }));
    announceChanges(seen, real.map((d) => [d.label, `${statusOf(d)[0]}${d.status === 'connected' && d.account ? ` as ${d.account}` : ''}, posting ${d.enabled ? 'on' : 'off'}`]));
  }
  await Promise.all([loadMode().catch((e) => clear(modeBox, h('p', { class: 'error' }, e.message))), load()]);
  focusCard(params && params[0] ? `dest-${params[0]}` : null);
  return refreshOnReturn(list_, load);
}

function testCard(dest, test, refresh) {
  const on = test ? test.enabled : Boolean(dest && dest.enabled);
  const sw = switchField({ label: 'Use the test destination', checked: on,
    help: 'Each post is only recorded on this computer. Nothing is sent anywhere.',
    onchange: async (e) => {
      try { await put('/publish/test-mode', { enabled: e.target.checked }); await refresh(); } catch (ex) { toast(ex.message, true); }
    } });
  return h('section', { id: 'dest-test', class: 'panel tinted', 'aria-labelledby': 'dt-title' },
    h('header', {}, h('h2', { id: 'dt-title' }, 'Test destination: nothing is posted'),
      h('span', { class: `pill ${on ? 'pos' : 'neutral'}` }, on ? 'On' : 'Off')),
    h('p', { style: 'margin-bottom: 12px' }, 'Use it to try the whole flow safely. It never counts as a real post and needs no account.'), sw.el);
}

// A saved key is not a connection: only "Connected" means the provider confirmed which account this is.
function statusOf(d) {
  if (d.status === 'disconnected' && (d.app_credentials_set || []).length) return ['Keys saved, not signed in', 'neutral'];
  return STATUS[d.status] || [d.status, 'neutral'];
}

// Who sends the clip to this destination. A route that is not part of this installation shows no fields and no
// button: there is nothing here to connect.
function routes(d) {
  const rs = (d.requirements || {}).routes || [];
  if (!rs.length) return null;
  return h('div', { class: 'routes', id: `routes-${d.provider}` },
    h('h3', {}, `Who posts to ${d.label}`),
    h('ul', { class: 'rows' }, rs.map((r) => h('li', { dataset: { route: r.id } },
      h('div', { class: 'grow' }, h('div', { class: 'title' }, r.title), h('p', {}, r.summary),
        (r.needs || []).length ? h('ul', { class: 'small' }, r.needs.map((n) => h('li', {}, n))) : null,
        r.guide ? h('p', { class: 'small' }, h('a', { href: `#/guide/${r.guide.page}`, help: `Open the guide page ${r.guide.label}.` }, r.guide.label),
          r.link ? [' · ', extLink(r.link)] : null) : null),
      h('span', { class: `pill ${r.available ? 'pos' : 'neutral'}` }, r.available ? 'Available here' : 'Not available in this installation')))));
}

function advanced(d) {
  const r = d.requirements || {};
  const scopes = Array.isArray(r.scopes) ? r.scopes : Object.entries(r.scopes || {}).flatMap(([k, v]) => v.map((s) => `${s} (${k.replace('_', ' ')})`));
  const fails = Object.entries(r.fails_without || {}).map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`);
  const quota = r.quotas ? [r.quotas.summary, d.quota ? `Used today: ${d.quota.used_today} of ${d.quota.limit}.` : null].filter(Boolean) : [];
  const docs = (r.docs || []).length ? h('div', {}, h('h4', {}, 'Official documentation'),
    h('ul', {}, r.docs.map((u) => h('li', {}, extLink({ url: u, label: u.replace(/^https?:\/\//, '') })))),
    r.as_of ? h('p', { class: 'small muted' }, `Checked against the provider's pages on ${r.as_of}.`) : null) : null;
  return moreDetails('Limits, permissions and audit', [
    { title: 'Limits', items: quota },
    { title: 'Permissions the sign-in asks for (scopes)', items: scopes },
    { title: 'Verification and audit', items: r.verification_audit || [] },
    { title: 'What fails without each step', items: fails },
    { title: 'Terms and rights', items: r.tos_rights ? [r.tos_rights] : [] },
    { title: 'Good to know', items: [...(r.notes || []), r.redirect_note].filter(Boolean) }], docs);
}

// One numbered step. Only the next step starts open; finished ones can be reopened to change them.
function step(n, total, title, state, body) {
  const tag = { done: ['Done', 'pos'], next: ['Do this next', 'accent'], later: ['Not yet', 'neutral'] }[state];
  return h('li', {}, h('h3', { class: 'sr-only' }, `Step ${n} of ${total}: ${title}`),
    h('details', { class: 'step', open: state === 'next', dataset: { step: String(n), state } },
      h('summary', { help: 'Show or hide this step.' },
        h('span', { class: 'grow' }, h('strong', {}, `${n}. ${title}`)), h('span', { class: 'sr-only' }, ', '),
        h('span', { class: `pill ${tag[1]}` }, tag[0])),
      h('div', { class: 'step-body stack' }, body)));
}

function card(d, refresh) {
  const r = d.requirements || {};
  const [label, tone] = statusOf(d);
  const connected = d.status === 'connected';
  const done = Object.fromEntries(((d.setup && d.setup.steps) || []).map((s) => [s.id, s.done]));
  const next = ['app_credentials', 'connect', 'rights_acknowledgement', 'enable'].find((id) => !done[id]);
  const state = (id) => (done[id] ? 'done' : id === next ? 'next' : 'later');
  const titles = r.step_titles || {};

  // step 1: only the fields the chosen way to connect needs
  const methods = r.methods || [];
  const methodBox = h('div', {});
  const showMethod = (id) => swapPanel(methodBox, () => methodPanel(d, methods.find((m) => m.id === id) || methods[0], refresh));
  let methodChoice = null;
  if (methods.length) {
    const picked = rememberedRoute(`dest.${d.provider}`, methods);
    if (methods.length > 1) {
      methodChoice = choiceGroup({ legend: `How do you want to connect ${d.label}?`, name: `method-${d.id}`, value: picked,
        options: methods.map((m) => ({ value: m.id, label: m.title, hint: m.pick })) });
      methodChoice.el.classList.add('way');
      methodChoice.inputs.forEach((i) => i.addEventListener('change', () => { rememberRoute(`dest.${d.provider}`, i.value); showMethod(i.value); }));
    }
    showMethod(picked);
  }
  const keysStep = [
    r.key_note ? h('p', { class: 'note info' }, r.key_note) : null,
    h('dl', { class: 'kv long facts' }, h('dt', {}, 'Account'), h('dd', {}, r.account || '-'),
      h('dt', {}, 'Cost'), h('dd', {}, r.cost || (r.free ? 'Free' : 'May cost money; check the provider.'))),
    (r.developer_app_steps || []).length ? [h('h4', {}, `On ${r.portal}`), stepList(r.developer_app_steps, `Steps on ${r.portal}`)] : null,
    methodChoice ? methodChoice.el : null, methodBox];

  // step 2: sign in. Nothing is uploaded: the app only asks the provider which account this is.
  const sign = r.sign_in || {};
  const out = h('div', { role: 'status', 'aria-live': 'polite' }); // right under the button it answers
  const connect = h('button', { type: 'button', class: connected ? 'btn' : 'btn primary',
    help: connected ? 'Ask the provider again which account this is. Nothing is posted.' : 'Opens the provider\'s sign-in page in a new tab. Nothing is posted.' },
  connected ? 'Check connection' : (sign.button || 'Connect'));
  connect.addEventListener('click', busy(connect, async () => {
    clear(out);
    try {
      const res = await post(`/destinations/${d.id}/connect`);
      if (res.authorize_url) {
        window.open(res.authorize_url, '_blank', 'noopener');
        clear(out, signInNote(res.authorize_url));
      } else {
        toast(res.ok === false ? (res.detail || 'Not connected.') : `Checked: connected${res.account ? ` as ${res.account}` : ''}. Nothing was posted.`, res.ok === false);
        await refresh();
      }
    } catch (ex) { clear(out, h('p', { class: 'error' }, ex.message)); }
  }));
  const signStep = [
    h('p', {}, sign.summary),
    (sign.asks || []).length ? [h('h4', {}, 'What the sign-in page asks you to allow'), h('ul', {}, sign.asks.map((a) => h('li', {}, a)))] : null,
    r.redirect_uri && !methods.some((m) => m.register_redirect) ? copyRow(r.redirect_uri, { title: 'Address the sign-in returns to', why: sign.callback_why }) : null,
    h('div', { class: 'row' }, connect), out,
    (r.recover || []).length ? h('details', { class: 'more' },
      h('summary', { help: 'Show or hide what to do when the sign-in page shows an error.' }, 'If the sign-in shows an error'),
      recoverList(r.recover)) : null];

  // step 3: the account the provider reported, before anything can be switched on
  const identity = r.identity || {};
  const change = identity.switch_label || 'Use a different account';
  const switchBtn = connected ? h('button', { type: 'button', class: 'btn', help: `Disconnect so you can sign in with another one. ${identity.change || ''}` }, change) : null;
  if (switchBtn) {
    switchBtn.addEventListener('click', async () => {
      if (!await confirmDialog({ title: `${change}: disconnect ${d.label}?`, body: `Posting to this account stops. Your keys, settings and rights confirmation are kept; after you connect again you switch posting on again yourself. ${identity.change || ''}`, ok: 'Disconnect', danger: true })) return;
      try { await post(`/destinations/${d.id}/disconnect`); await refresh(); } catch (ex) { toast(ex.message, true); }
    });
  }
  const ackId = `ack-${d.id}`;
  const ack = h('input', { type: 'checkbox', id: ackId, checked: d.rights_acknowledged, disabled: !connected && !d.rights_acknowledged,
    'aria-describedby': `${ackId}-help` });
  ack.dataset.help = 'Required before posting.';
  ack.addEventListener('change', async () => {
    try { await put(`/destinations/${d.id}/acknowledge`, { acknowledged: ack.checked }); await refresh(); } catch (ex) { toast(ex.message, true); ack.checked = !ack.checked; }
  });
  const rightsStep = [
    connected ? h('div', { class: 'identity' },
      h('p', {}, `${identity.noun || 'Account'} the provider reported: `, h('strong', {}, d.account || 'unnamed account'),
        d.account_url ? [' ', extLink({ url: d.account_url, label: identity.open || 'Open it to check' })] : null),
      h('p', { class: 'small muted' }, identity.check),
      h('div', { class: 'row', style: 'margin-top: 8px' }, switchBtn))
      : h('p', { class: 'small muted' }, 'After you sign in, the account the provider reports is shown here so you can check it before anything is switched on.'),
    h('label', { class: 'choice', for: ackId }, ack, h('span', {}, `I own these clips or have the streamer's permission to post them on ${connected && d.account ? d.account : 'this account'}.`)),
    h('p', { class: 'help', id: `${ackId}-help` }, 'Required before posting. A streamer\'s permission does not override a platform\'s terms.')];

  // step 4: on. Connecting never switches this on by itself.
  const why = !connected ? 'Connect the account first.' : !d.rights_acknowledged ? 'Confirm your posting rights first.' : null;
  const enable = switchField({ label: `Post to ${connected && d.account ? d.account : 'this destination'}`, checked: d.enabled, disabled: Boolean(why) && !d.enabled, disabledReason: why,
    help: 'When on, clips can be posted here, automatically or when you press Post. Connecting an account never switches this on.',
    onchange: async (e) => {
      try { await put(`/destinations/${d.id}/enabled`, { enabled: e.target.checked }); await refresh(); } catch (ex) { toast(ex.message, true); e.target.checked = !e.target.checked; }
    } });

  // what the provider or the app last reported, with the next thing to do. A lasting state, so not an alert:
  // the pill above already carries it and screen readers are told when it changes (announceChanges).
  const fix = nextAction(r.recover, d.status_detail);
  const calm = connected && !/different account/.test(d.status_detail || '');
  const problem = d.status_detail ? h('div', { class: calm ? 'note info' : 'note', role: 'note', dataset: { problem: calm ? 'no' : 'yes' } },
    h('p', {}, d.status_detail),
    fix ? h('p', {}, h('strong', {}, 'What to do: '), fix.action, fix.link ? [' ', extLink(fix.link)] : null) : null) : null;

  const settings = settingsForm(d, refresh);
  return h('section', { id: `dest-${d.provider}`, class: 'panel', 'aria-labelledby': `dn-${d.id}` },
    h('header', {}, h('h2', { id: `dn-${d.id}` }, d.label),
      h('span', { class: `pill ${tone}` }, label, connected && d.account ? ` as ${d.account}` : ''),
      h('span', { class: `pill ${d.enabled ? 'pos' : 'neutral'}` }, d.enabled ? 'Posting on' : 'Posting off')),
    h('p', {}, r.what), routes(d), problem,
    h('ol', { class: 'setup-steps', role: 'list', 'aria-label': `Setup steps for ${d.label}` },
      step(1, 4, titles.app_credentials || 'Save your developer app keys', state('app_credentials'), keysStep),
      step(2, 4, titles.connect || 'Sign in', state('connect'), signStep),
      step(3, 4, titles.rights_acknowledgement || 'Check the account and confirm your rights', state('rights_acknowledgement'), rightsStep),
      step(4, 4, titles.enable || 'Turn posting on', state('enable'), enable.el)),
    advanced(d),
    settings ? h('details', { class: 'more' }, h('summary', { help: 'Show or hide the optional settings for this destination.' }, 'Settings'), settings) : null);
}

// The fields of one way to connect. Keys are saved first; a token is checked with the provider at once.
function methodPanel(d, m, refresh) {
  const r = d.requirements || {};
  const fields = m.fields.map(guidedField);
  const save = h('button', { type: 'submit', class: 'btn primary', help: m.after }, m.button);
  const out = h('div', { role: 'status', 'aria-live': 'polite' });
  const saved = (d.app_credentials_set || []);
  const form = h('form', { class: 'stack', novalidate: true, 'aria-label': `${d.label}: ${m.title}`, dataset: { method: m.id } },
    h('h4', {}, 'Enter the values in Live Clipper'),
    m.submit === 'credentials' && saved.length ? h('p', { class: 'small muted' }, `Saved on this computer: ${savedNames(saved, m.fields)}. Values are never shown; enter new ones to replace them.`) : null,
    fields.map((f) => f.el), h('div', {}, save), h('p', { class: 'small muted' }, m.after), out);
  form.addEventListener('submit', busy(save, async (e) => {
    e.preventDefault();
    clear(out);
    fields.forEach((f) => setError(f.el, ''));
    const missing = fields.find((f, i) => m.fields[i].required !== false && !f.input.value.trim());
    if (missing) { setError(missing.el, 'This value is needed.'); missing.input.focus(); return; }
    // an optional field left empty is sent empty, which removes its saved value (an old secret must not linger)
    const body = Object.fromEntries(fields.map((f, i) => [f.input.name, f.input.value.trim(), m.fields[i]])
      .filter(([, v, def]) => v || def.required === false).map(([k, v]) => [k, v]));
    try {
      if (m.submit === 'token') await post('/destinations/instagram/token', { ...body, destination_id: d.id });
      else await put(`/destinations/${d.id}/credentials`, body);
    } catch (ex) {
      const errs = ex instanceof ApiError ? ex.fields : {};
      const named = fields.filter((f) => errs[f.input.name]);
      named.forEach((f) => setError(f.el, errs[f.input.name]));
      if (named.length) named[0].input.focus();
      else if (fields.length === 1) { setError(fields[0].el, ex.message); fields[0].input.focus(); } else clear(out, h('p', { class: 'error' }, ex.message));
      return;
    }
    fields.forEach((f) => { f.input.value = ''; delete f.input.dataset.dirty; }); // sent; never kept on the page
    toast(m.submit === 'token' ? `${d.label} connected. Nothing was posted.` : 'Keys saved. Next: sign in.');
    try { await refresh(); } catch (ex) { toast(ex.message, true); }
  }));
  const agent = h('details', { class: 'more' }, h('summary', { help: 'Show or hide the agent helper for these keys.' }, 'Or let my agent fill these in'));
  if (m.agent) agentPanel(`destination:${d.provider}`, refresh, 'below', 'h4').then((p) => { if (p) agent.append(p); else agent.remove(); });
  return h('div', { class: 'route-panel' },
    (m.steps || []).length ? stepList(m.steps, `Steps for ${m.title}`) : null,
    m.register_redirect && r.redirect_uri ? copyRow(r.redirect_uri, { title: 'Redirect address to register', why: m.redirect_why }) : null,
    form, m.agent ? agent : null);
}
