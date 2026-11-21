// Guide: local Markdown pages from GET /api/guide, rendered without network access.

import { api, get } from '../api.js';
import { h, titleRow } from '../ui.js';
import { renderMarkdown } from '../md.js';

export async function render(main, params) {
  const pages = (await get('/guide')).pages || [];
  const slug = params[0] || (pages[0] && pages[0].slug);
  const known = new Set(pages.map((p) => p.slug));
  const linkFor = (href) => {
    const m = href.match(/^(?:\.\/)?([\w-]+)\.md(#.*)?$/);
    if (m && known.has(m[1])) return `#/guide/${m[1]}`;
    if (href.startsWith('#/')) return href;
    return href.startsWith('#') ? `#/guide/${slug}` : '#/guide';
  };
  const article = h('article', { 'aria-live': 'polite' });
  main.append(titleRow('Guide', 'How Live Clipper works, stored with the app.'),
    h('div', { id: 'guide-main', class: 'guide-layout' },
      h('nav', { class: 'panel', 'aria-label': 'Guide pages' }, h('ul', {}, pages.map((p) => h('li', {},
        h('a', { href: `#/guide/${p.slug}`, 'aria-current': p.slug === slug ? 'page' : null, help: `Read the ${p.title} page.` }, p.title))))),
      h('div', { class: 'panel' }, article)));
  if (!slug) { article.append(h('p', { class: 'muted' }, 'No guide pages are installed.')); return; }
  try {
    article.append(renderMarkdown(await api(`/guide/${encodeURIComponent(slug)}`, { raw: true }), linkFor));
  } catch (e) {
    article.append(h('p', { class: 'error' }, `This page could not load: ${e.message}`));
  }
}
