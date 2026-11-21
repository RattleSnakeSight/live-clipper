// Presets: how clips look. Same list and editor as formats.

import { titleRow } from '../ui.js';
import { renderEditor, renderList } from './docs.js';

export async function render(main, params) {
  if (params[0]) return renderEditor(main, 'presets', params[0], 'presets');
  main.append(titleRow('Presets', 'A preset decides how a clip looks: size, framing, captions and loudness.'));
  await renderList(main, 'presets', { listId: 'preset-list',
    intro: 'Duplicate the built-in preset to make your own, or import one from a file. Rules can pick a preset for some clips.' });
}
