// Status: health of each part of the app, restarts, disk and recent log lines (already redacted by the server).

import { get } from '../api.js';
import { clear, fmtBytes, fmtDuration, fmtTime, h, poll, titleRow } from '../ui.js';

export async function render(main) {
  const box = h('div', { id: 'status-main', class: 'stack' });
  const refresh = h('button', { type: 'button', class: 'btn', help: 'Load the latest status and log lines.' }, 'Refresh');
  main.append(titleRow('Status', 'What is running, what restarted and the latest log lines. Secrets are hidden.', refresh), box);

  async function load() {
    const d = await get('/diagnostics');
    const services = Object.entries(d.services || {});
    const bad = services.filter(([, v]) => v && v.error);
    const tool = (t) => (t && t.found ? `Found${t.version ? ` (${t.version})` : ''}` : 'Not found');
    clear(box,
      h('section', { class: 'panel', 'aria-labelledby': 'st-health' },
        h('h2', { id: 'st-health' }, bad.length ? `${bad.length} part${bad.length > 1 ? 's' : ''} need attention` : 'Everything is running'),
        h('dl', { class: 'kv', style: 'margin-top: 8px' },
          h('dt', {}, 'Version'), h('dd', {}, d.version || '-'),
          h('dt', {}, 'Running for'), h('dd', {}, fmtDuration(d.uptime)),
          h('dt', {}, 'Free disk'), h('dd', {}, d.disk ? `${fmtBytes(d.disk.free_bytes)} of ${fmtBytes(d.disk.total_bytes)}` : '-'),
          h('dt', {}, 'FFmpeg'), h('dd', {}, tool(d.ffmpeg)),
          h('dt', {}, 'FFprobe'), h('dd', {}, tool(d.ffprobe)))),
      h('section', { class: 'panel', 'aria-labelledby': 'st-svc' }, h('h2', { id: 'st-svc' }, 'Parts of the app'),
        services.length ? h('div', { class: 'table-wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Part'), h('th', { scope: 'col' }, 'State'), h('th', { scope: 'col' }, 'Details'))),
          h('tbody', {}, services.map(([name, v]) => h('tr', {}, h('td', {}, name),
            h('td', {}, h('span', { class: `pill ${v && v.error ? 'neg' : 'pos'}` }, v && v.error ? 'Failed' : 'Running')),
            h('td', { class: 'small' }, v && v.error ? v.error : summarize(v)))))))
          : h('p', { class: 'muted' }, 'No parts reported yet.')),
      h('section', { class: 'panel', 'aria-labelledby': 'st-rs' }, h('h2', { id: 'st-rs' }, 'Restarts'),
        h('p', {}, d.restarts && d.restarts.count ? `The app restarted itself ${d.restarts.count} time${d.restarts.count > 1 ? 's' : ''} after a problem.` : 'No restarts.'),
        d.restarts && d.restarts.recent && d.restarts.recent.length ? h('ul', { class: 'small' },
          d.restarts.recent.slice(0, 10).map((r) => h('li', {}, `${fmtTime(r.ts)}: ${r.message}`))) : null),
      h('section', { class: 'panel', 'aria-labelledby': 'st-log' }, h('h2', { id: 'st-log' }, 'Recent log lines'),
        h('p', { class: 'small muted', style: 'margin-bottom: 8px' }, 'Tokens and passwords are replaced with [redacted] before they reach this page.'),
        (d.log_tail || []).length ? h('pre', { class: 'log', tabindex: 0, 'aria-label': 'Recent log lines' }, d.log_tail.join('\n'))
          : h('p', { class: 'muted' }, 'The log is empty.')));
  }
  refresh.addEventListener('click', () => load());
  await load();
  return poll(load, 10000);
}

function summarize(v) {
  if (!v || typeof v !== 'object') return String(v ?? '');
  return Object.entries(v).filter(([, x]) => x === null || typeof x !== 'object')
    .map(([k, x]) => `${k.replace(/_/g, ' ')}: ${x}`).join(' · ');
}
