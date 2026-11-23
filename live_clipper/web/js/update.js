// The update gate in the browser: every open asks the app to check for a newer release. While the answer is
// "pending", "unknown" or still "checking", this screen replaces the app. The server refuses the app's normal
// requests in those states anyway (423), so this is the explanation, not the lock.

import { get, post } from './api.js';
import { announce, clear, h, toast } from './ui.js';

const AGENT_NAMES = { claude: 'Claude Code', codex: 'Codex' };
const REASONS = {
  offline: 'there is no connection to the internet, or GitHub cannot be reached',
  timeout: 'the update source did not answer in time',
  rate_limited: 'GitHub is refusing requests for a moment',
  unavailable: 'the update source did not answer',
  bad_response: 'the answer could not be read',
  proxy: "this computer's proxy setting cannot be used for the check",
};

let timer = null;
let shown = null;

export const isGated = (u) => Boolean(u) && ['pending', 'unknown', 'checking'].includes(u.state);

// One open of the app = one check. Resolves to the update view, or null when the app gave no answer
// (then the normal app loads, and the server still refuses work if the copy is out of date).
export async function checkOnOpen() {
  try { return await post('/update/check', { kind: 'open' }); } catch { return null; }
}

export function gateShown() { return shown !== null; }

// The check came back current after the "checking" screen showed: hand the page back to the app.
export function hideGate() {
  clearInterval(timer);
  timer = null;
  shown = null;
  document.body.classList.remove('setup-mode', 'gated');
}

function copyRow(command, help) {
  const code = h('code', {}, command);
  const button = h('button', { type: 'button', class: 'btn small', help, 'aria-label': `Copy ${command}` }, 'Copy');
  button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(command); toast('Copied.'); } catch {
      const range = document.createRange();
      range.selectNodeContents(code);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast('Selected. Press Ctrl+C (Cmd+C on a Mac) to copy.');
    }
  });
  return h('div', { class: 'copy-row' }, code, button);
}

function updateSteps(u) {
  const agent = AGENT_NAMES[u.agent];
  const method = u.method === 'editable'
    ? h('li', {}, h('p', {}, 'This copy runs from a developer checkout. Get the new version there:'),
      copyRow('git pull', 'Copy the command that downloads the new version into the checkout.'),
      h('p', { class: 'help' }, 'Then stop and start Live Clipper (next step).'))
    : h('li', {}, h('p', {}, 'Open a terminal (PowerShell on Windows, Terminal on a Mac) and run:'),
      copyRow('live-clipper update', 'Copy the update command.'),
      h('p', { class: 'help' }, 'It stops Live Clipper, installs the new version and starts it again.'),
      u.method === 'pipx' ? h('p', { class: 'help' }, 'Installed with pipx, so ',
        h('code', {}, 'pipx upgrade live-clipper'), ' works as well. Run ', h('code', {}, 'live-clipper start'),
        ' after it.') : null,
      h('p', { class: 'help' }, 'If the terminal says the command is not found, run ',
        h('code', {}, 'python -m live_clipper update'), ' instead.'));
  return h('ol', { class: 'gate-steps' },
    method,
    h('li', {}, h('p', {}, agent
      ? `Or let ${agent}, the agent that installed Live Clipper, do it. Ask it:`
      : 'Or, if an AI agent such as Claude Code or Codex installed Live Clipper, ask it:'),
    h('blockquote', {}, 'Update Live Clipper to the newest version.')),
    h('li', {}, h('p', {}, 'Live Clipper starts again by itself and opens in a new browser tab. You can close this one. '
      + 'If it does not open, run:'),
    copyRow('live-clipper start', 'Copy the command that starts Live Clipper.')));
}

function versions(u) {
  return h('dl', { class: 'gate-versions' },
    h('div', {}, h('dt', {}, 'Installed'), h('dd', { class: 'num' }, u.installed)),
    h('div', { class: 'available' }, h('dt', {}, 'Available'), h('dd', { class: 'num' }, u.available)));
}

const kept = () => h('p', {}, 'Your clips, recordings, settings, saved keys and queued posts are kept. Nothing is '
  + 'deleted or posted while Live Clipper waits.');

function lastChecked(u) {
  if (!u.checked_at) return '';
  const t = new Date(u.checked_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return `Last checked at ${t}.`;
}

function render(main, u, onRetry) {
  const title = { pending: 'Update pending', unknown: "Can't check for updates", checking: 'Checking for updates' }[u.state];
  document.title = `${title} - Live Clipper`;
  const status = h('p', { class: 'small muted gate-status', role: 'status', 'aria-atomic': 'true' },
    u.state === 'pending' ? lastChecked(u) : '');
  const retry = h('button', { type: 'button', class: 'btn primary', help: 'Ask again now whether a newer version is out.' },
    'Check again');
  retry.addEventListener('click', () => onRetry(retry, status));
  const heading = h('h1', { id: 'gate-title', tabindex: '-1' }, title);
  let body;
  if (u.state === 'pending') {
    body = [
      h('p', { class: 'lead' }, 'A newer version of Live Clipper is out. Until you install it, Live Clipper does not '
        + 'watch channels, make clips or post.'),
      versions(u), kept(),
      h('h2', {}, 'How to update'), updateSteps(u),
    ];
  } else if (u.state === 'unknown') {
    const reason = REASONS[u.error] || 'the check failed';
    // the app waits at most 5 minutes between tries
    const wait = u.next_check ? Math.min(300, Math.max(0, Math.round(u.next_check - Date.now() / 1000))) : null;
    body = [
      h('p', { class: 'lead' }, 'Live Clipper makes sure it is the newest version before it watches, clips or posts. '
        + `It could not find out this time: ${reason}.`),
      h('p', {}, wait !== null ? `It tries again by itself in about ${wait < 60 ? `${wait} seconds` : `${Math.round(wait / 60)} minutes`}, `
        + 'and starts as soon as a check works. If you are offline, connect and press Check again.'
        : 'Press Check again once you are connected.'),
      kept(),
      h('details', { class: 'gate-more' }, h('summary', {}, 'Update without waiting'), updateSteps(u)),
    ];
  } else {
    body = [h('p', { class: 'lead' }, 'Live Clipper is making sure it is the newest version. This takes a few seconds.')];
  }
  clear(main, h('section', { class: 'gate', 'aria-labelledby': 'gate-title', 'aria-busy': String(u.state === 'checking'),
    dataset: { state: u.state } },
  heading, body,
  u.state === 'checking' ? null : h('div', { class: 'gate-actions' }, retry, status),
  h('p', { class: 'small muted gate-why' }, 'Why: Live Clipper only runs when it is up to date. At every start and every '
    + 'time you open it, it reads the newest version number from the project on GitHub. It sends nothing about you or '
    + 'this computer.')));
  return heading;
}

// Replace the app with the gate screen and keep it current: polls the app, follows a restart, and reloads into the
// normal app once the copy is up to date.
export function showGate(u) {
  if (!isGated(u)) return;
  const main = document.getElementById('main');
  document.body.classList.add('setup-mode', 'gated');
  const first = shown === null;
  const changed = !shown || shown.state !== u.state || shown.available !== u.available;
  shown = u;
  if (!changed) return;
  const heading = render(main, u, retry);
  heading.focus({ preventScroll: true });
  if (!first) announce(heading.textContent);
  if (!timer) timer = setInterval(poll, 5000);
}

async function poll() {
  let u;
  try { u = await get('/update'); } catch (e) {
    if (e.status === 401) { clearInterval(timer); timer = null; return; } // restarted: the tab shows how to reopen
    const status = document.querySelector('.gate-status');
    if (status && e.status !== 401) status.textContent = 'Live Clipper is not answering. It may be restarting.';
    return;
  }
  if (u.state === 'current') { location.reload(); return; }
  showGate(u);
}

async function retry(button, status) {
  button.disabled = true;
  status.textContent = 'Checking…';
  try {
    const u = await post('/update/check', { kind: 'retry' });
    if (u.state === 'current') { status.textContent = 'Up to date. Opening Live Clipper…'; location.reload(); return; }
    shown = null; // render again, so the time and the reason are fresh
    showGate(u);
    document.querySelector('.gate-actions .btn')?.focus(); // keep the keyboard where it was
    const s = document.querySelector('.gate-status');
    if (s) s.textContent = u.state === 'pending' ? `Still pending. ${lastChecked(u)}` : 'Still could not check.';
  } catch {
    status.textContent = 'Live Clipper is not answering. It may be restarting.';
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}
