// First setup resumes at the next unanswered question. Later edits stay in a draft until Save setup.

import { ApiError, get, list, post, put } from '../api.js';
import { answerLabel, checkValue, effective, isVisible, questionField } from '../questions.js';
import { btn, clear, fmtBytes, h } from '../ui.js';
import { PLATFORMS } from '../labels.js';
import { agentPanel } from '../agent.js';
import { markSetupDone } from '../app.js';
import { maybeStartTour } from '../tour.js';

// The accounts behind the two answers "which platforms to watch" and "where to post": what each connection is
// for, whether it needs an account, and where it is set up once setup is finished. Nothing is connected here.
async function accountsBox(answers, questions) {
  const watch = answers['platforms.enabled'] || [];
  const post_ = (answers['posting.destinations'] || []).filter((p) => p !== 'none');
  const [sources, dests] = await Promise.all([
    get('/sources').then((d) => list(d, 'sources')).catch(() => []),
    get('/destinations').then((d) => list(d, 'destinations')).catch(() => [])]);
  const agent = (target) => {
    const slot = h('details', { class: 'more' }, h('summary', { help: 'Show or hide the agent helper for these keys.' }, 'Let my agent fill in the keys'));
    agentPanel(target, () => {}, 'after-setup', 'h5').then((p) => { if (p) slot.append(p); else slot.remove(); });
    return slot;
  };
  const item = (name, what, need, where, target) => h('li', {},
    h('div', { class: 'grow' }, h('div', { class: 'title' }, name), h('p', {}, what),
      h('p', { class: 'small muted' }, need), h('p', { class: 'small' }, where), agent(target)));
  const watchRows = watch.map((p) => {
    const s = sources.find((x) => x.platform === p) || {};
    const about = s.about || {};
    const name = PLATFORMS[p] || p;
    return item(name, about.what || '', about.without || '',
      s.connected ? 'Already connected.' : `After setup: on the Sources page, under ${name}. Or just add a channel on the Channels page.`, `source:${p}`);
  });
  const postRows = post_.map((p) => {
    const d = dests.find((x) => x.provider === p) || {};
    const r = d.requirements || {};
    return item(d.label || p, r.what || '', `Account: ${r.account || 'your own account'}`,
      d.status === 'connected' ? `Already connected${d.account ? ` as ${d.account}` : ''}.`
        : `After setup: on the Destinations page, under ${d.label || p}. Four steps, shown one at a time.`, `destination:${p}`);
  });
  if (!watchRows.length && !postRows.length) return null;
  // who posts to Instagram and TikTok, said once more when either was picked (the note of that question)
  const outside = post_.some((p) => p === 'instagram' || p === 'tiktok')
    ? (questions.find((q) => q.id === 'posting.destinations') || {}).note : '';
  return h('section', { id: 'wizard-accounts', class: 'panel', 'aria-labelledby': 'wa-title' },
    h('h3', { id: 'wa-title' }, 'Accounts for what you picked'),
    h('p', { class: 'small muted' }, 'Connecting can wait until setup is finished, and nothing is ever posted until you connect an account, '
      + 'confirm your rights and switch a destination on. Each connection is set up on its own page, with the steps next to the fields.'),
    watchRows.length ? [h('h4', {}, 'Watch a stream'), h('ul', { class: 'rows' }, watchRows)] : null,
    postRows.length ? [h('h4', {}, 'Post my clips'), h('ul', { class: 'rows' }, postRows)] : null,
    outside ? h('p', { class: 'note info', id: 'wizard-metricool' }, h('strong', {}, 'Who posts to Instagram and TikTok: '), outside) : null);
}

export async function render(main, params) {
  const [qd, st, hw] = await Promise.all([
    get('/setup/questions'), get('/setup/state'), get('/setup/hardware').catch(() => null)]);
  const questions = qd.questions || [];
  const saved = { ...st.answers };
  const answers = effective(questions, saved);
  let tryLocal = false;
  const cancel = () => st.completed ? btn('Cancel', 'Discard this setup draft and return to Settings.', () => {
    location.hash = '#/settings';
  }) : null;
  const visible = () => questions.filter((q) => isVisible(q, answers));
  const root = h('div', { class: 'wizard' });
  main.append(root);

  const firstRemaining = () => {
    const rem = new Set(st.remaining || []);
    const i = visible().findIndex((q) => rem.has(q.id));
    return i;
  };

  function header(step, total) {
    const pct = total ? Math.min(1, step / total) : 1;
    return [
      h('h1', {}, st.completed ? 'Change your setup' : 'Set up Live Clipper'),
      h('p', { class: 'help' }, st.completed ? 'Changes take effect when you choose Save setup. Cancel discards this draft.' : 'A few questions so the app fits this computer. You can change these answers later.'),
      h('div', { class: 'progress', role: 'progressbar', 'aria-label': 'Setup progress', 'aria-valuemin': 0,
        'aria-valuemax': total, 'aria-valuenow': Math.min(step, total) },
      h('span', { style: `transform: scaleX(${pct})` })),
    ];
  }

  function hardwareSummary() {
    if (!hw) return null;
    const gpu = hw.gpu && hw.gpu.kind && hw.gpu.kind !== 'none' ? hw.gpu.name || hw.gpu.kind : 'None found';
    const ff = hw.ffmpeg && hw.ffmpeg.found;
    return h('section', { class: 'panel tinted', 'aria-labelledby': 'hw-title', style: 'margin-bottom: 24px' },
      h('h2', { id: 'hw-title' }, 'Found on this computer'),
      h('p', { class: 'help', style: 'margin-bottom: 8px' }, 'The suggested answers below are based on this.'),
      h('dl', { class: 'kv' },
        h('dt', {}, 'Processor'), h('dd', {}, `${hw.cpu.model} (${hw.cpu.cores} threads)`),
        h('dt', {}, 'Memory'), h('dd', {}, fmtBytes(hw.ram_bytes)),
        h('dt', {}, 'Graphics card'), h('dd', {}, gpu),
        h('dt', {}, 'Free disk space'), h('dd', {}, fmtBytes(hw.disk && hw.disk.free_bytes)),
        h('dt', {}, 'FFmpeg'), h('dd', {}, ff ? `Installed (${hw.ffmpeg.version || 'version unknown'})`
          : 'Not found. Clips cannot be made until FFmpeg is installed; the Guide explains how.')));
  }

  function showQuestion(i) {
    const vis = visible();
    if (i >= vis.length) return showReview();
    const q = vis[i];
    const field = questionField(q, answers[q.id], { big: true });
    const next = btn(i === vis.length - 1 ? 'Review answers' : 'Next', st.completed ? 'Keep this answer in the draft and continue.' : 'Save this answer and continue.', null, 'primary next');
    const form = h('form', { novalidate: true, onsubmit: async (e) => {
      e.preventDefault();
      const value = field.read();
      const err = checkValue(q, value);
      if (err) { field.setError(err); field.focus(); return; }
      next.disabled = true;
      try {
        if (!st.completed && (!(q.id in saved) || JSON.stringify(saved[q.id]) !== JSON.stringify(value))) {
          await put('/setup/answers', { answers: { [q.id]: value } });
          saved[q.id] = value;
        }
        answers[q.id] = value;
        showQuestion(i + 1);
      } catch (ex) {
        next.disabled = false;
        field.setError((ex instanceof ApiError && ex.fields[q.id]) || ex.message);
        field.focus();
      }
    } },
    field.el,
    h('nav', { 'aria-label': 'Setup steps' },
      i > 0 ? btn('Back', 'Go back to the previous question. This answer is not saved.', () => showQuestion(i - 1)) : null,
      cancel(), next));
    next.type = 'submit';
    clear(root, header(i, vis.length + 1), i === 0 ? hardwareSummary() : null,
      h('p', { class: 'small muted', style: 'margin-bottom: 8px' }, `Question ${i + 1} of ${vis.length}`), form);
    field.focus();
  }

  function showReview(errors = {}) {
    const vis = visible();
    const localTrial = !st.completed && !(answers['posting.destinations'] || []).some((p) => p !== 'none');
    const trial = localTrial ? h('label', { class: 'choice' }, h('input', {
      type: 'checkbox', help: 'Hold clips for review and enable the local test destination.', checked: tryLocal, onchange: (e) => { tryLocal = e.target.checked; },
    }), 'Try Manual review with the local test destination. No account is needed.') : null;
    const finish = btn(st.completed ? 'Save setup' : 'Finish setup', 'Save every answer and open the dashboard.', null, 'primary next');
    finish.addEventListener('click', async () => {
      finish.disabled = true;
      try {
        const changes = Object.fromEntries(vis.filter((q) => st.completed &&
          JSON.stringify(answers[q.id]) !== JSON.stringify(saved[q.id])).map((q) => [q.id, answers[q.id]]));
        if (localTrial && tryLocal) changes['posting.mode'] = 'manual';
        await post('/setup/complete', { answers: changes });
        if (localTrial && tryLocal) {
          try { await put('/publish/test-mode', { enabled: true }); }
          catch (err) {
            markSetupDone();
            clear(root, h('h1', {}, 'Setup saved'), h('p', { role: 'alert' },
              'Manual mode is saved. The test destination could not be enabled. ' + err.message),
              h('a', { href: '#/destinations' }, 'Open Destinations to retry'));
            return;
          }
        }
        markSetupDone();
        location.hash = '#/dashboard';
        maybeStartTour();
      } catch (ex) {
        finish.disabled = false;
        showReview(ex instanceof ApiError && Object.keys(ex.fields).length ? ex.fields : { _: ex.message });
      }
    });
    const list = h('dl', { class: 'review' }, vis.map((q, i) => [
      h('dt', {}, q.title),
      h('dd', {}, h('span', {}, answerLabel(q, answers[q.id])),
        btn('Change', `Go back and change your answer to: ${q.title}`, () => showQuestion(i), 'small ghost'),
        errors[q.id] ? h('span', { class: 'error' }, errors[q.id]) : null),
    ]));
    const errCount = Object.keys(errors).length;
    const accounts = h('div', {});
    accountsBox(answers, questions).then((box) => { if (box) accounts.append(box); }).catch(() => {});
    clear(root, header(vis.length + 1, vis.length + 1),
      h('h2', { tabindex: -1, id: 'review-title' }, 'Check your answers'),
      errCount ? h('p', { class: 'note bad', role: 'alert' },
        errors._ || `${errCount} answer${errCount > 1 ? 's need' : ' needs'} a change before setup can finish.`) : null,
      accounts,
      list, trial,
      h('nav', { 'aria-label': 'Setup steps' },
        btn('Back', 'Go back to the last question.', () => showQuestion(vis.length - 1)), cancel(), finish));
    root.querySelector('#review-title').focus();
  }

  if (params[0] === 'review') showReview();
  else if (st.completed) showQuestion(0);
  else {
    const i = firstRemaining();
    if (i === -1) showReview(); else showQuestion(i);
  }
}
