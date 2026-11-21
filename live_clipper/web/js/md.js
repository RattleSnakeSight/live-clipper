// Small Markdown renderer for the local guide. Builds DOM nodes directly (no innerHTML), so page text can
// never inject markup. Supports headings, paragraphs, lists, fenced code, blockquotes, tables, rules,
// inline code, bold, italic and links.

import { h } from './ui.js';

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(\[[^\]]+\]\([^)\s]+\))/;

// linkFor(href) maps guide-relative links ("monitoring.md", "#/guide/x") to app routes; unsafe schemes drop.
export function safeHref(href, linkFor) {
  const s = href.trim();
  if (/^(https?:|mailto:)/i.test(s)) return { href: s, external: true };
  if (/^[a-z][a-z0-9+.-]*:/i.test(s)) return null; // javascript:, data:, file: and friends
  return { href: linkFor ? linkFor(s) : s, external: false };
}

export function inline(text, linkFor) {
  const out = [];
  let rest = text;
  while (rest) {
    const m = rest.match(INLINE);
    if (!m) { out.push(rest); break; }
    if (m.index) out.push(rest.slice(0, m.index));
    const t = m[0];
    if (m[1]) out.push(h('code', {}, t.slice(1, -1)));
    else if (m[2]) out.push(h('strong', {}, inline(t.slice(2, -2), linkFor)));
    else if (m[3]) out.push(h('em', {}, inline(t.slice(1, -1), linkFor)));
    else {
      const [, label, href] = t.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      const link = safeHref(href, linkFor);
      if (!link) out.push(label);
      else if (link.external) {
        out.push(h('a', { href: link.href, target: '_blank', rel: 'noopener noreferrer', help: `Opens ${link.href} in a new tab.` }, inline(label, linkFor)));
      } else out.push(h('a', { href: link.href, help: 'Opens this page of the guide.' }, inline(label, linkFor)));
    }
    rest = rest.slice(m.index + t.length);
  }
  return out;
}

const LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

function parseList(lines, i, linkFor) {
  const indent = lines[i].match(LIST)[1].length;
  const ordered = /\d/.test(lines[i].match(LIST)[2]);
  const list = h(ordered ? 'ol' : 'ul');
  while (i < lines.length) {
    const m = lines[i].match(LIST);
    if (!m || m[1].length < indent) break;
    if (m[1].length > indent) {
      const [sub, next] = parseList(lines, i, linkFor);
      (list.lastElementChild || list.appendChild(h('li'))).append(sub);
      i = next;
      continue;
    }
    list.append(h('li', {}, inline(m[3], linkFor)));
    i++;
    while (i < lines.length && lines[i].trim() && !LIST.test(lines[i]) && /^\s{2,}/.test(lines[i])) {
      list.lastElementChild.append(' ', ...inline(lines[i].trim(), linkFor));
      i++;
    }
  }
  return [list, i];
}

const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());

export function renderMarkdown(src, linkFor) {
  const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
  const root = h('div', { class: 'md' });
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const fence = line.match(/^\s*(```|~~~)/);
    if (fence) {
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i++;
      root.append(h('pre', {}, h('code', {}, body.join('\n'))));
      continue;
    }
    const head = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (head) {
      root.append(h(`h${Math.min(head[1].length, 4)}`, {}, inline(head[2], linkFor)));
      i++;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { root.append(h('hr')); i++; continue; }
    if (LIST.test(line)) {
      const [list, next] = parseList(lines, i, linkFor);
      root.append(list);
      i = next;
      continue;
    }
    if (/^\s*>/.test(line)) {
      const body = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ''));
      root.append(h('blockquote', {}, renderMarkdown(body.join('\n'), linkFor).childNodes));
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      const header = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]));
      root.append(h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, header.map((c) => h('th', { scope: 'col' }, inline(c, linkFor))))),
        h('tbody', {}, rows.map((r) => h('tr', {}, r.map((c) => h('td', {}, inline(c, linkFor)))))))));
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*```|\s*~~~|\s*>)/.test(lines[i]) && !LIST.test(lines[i])) {
      para.push(lines[i++].trim());
    }
    if (!para.length) para.push(lines[i++].trim());
    root.append(h('p', {}, inline(para.join(' '), linkFor)));
  }
  return root;
}
