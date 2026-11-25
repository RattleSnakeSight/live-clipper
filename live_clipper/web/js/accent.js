import { appState, setAppState } from './api.js';
import { h as el, toast } from './ui.js';

const DEFAULT_HUE = 200;
const PRESETS = [200, 160, 140, 250, 25, 55, 330]; // teal (default), mint, green, blue, coral, amber, pink

// ---- colour maths: OKLCH -> sRGB -> WCAG contrast, to pick readable text on the accent ----
function oklchToRgb(L, C, h) {
  const a = C * Math.cos((h * Math.PI) / 180);
  const b = C * Math.sin((h * Math.PI) / 180);
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
  return [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s].map((v) => Math.min(1, Math.max(0, v)));
}
const lum = ([r, g, b]) => 0.2126 * r + 0.7152 * g + 0.0722 * b; // linear values already
const contrast = (x, y) => (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
function toHex(rgbLinear) {
  return '#' + rgbLinear.map((v) => {
    const s = v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055;
    return Math.round(Math.min(1, Math.max(0, s)) * 255).toString(16).padStart(2, '0');
  }).join('');
}
function hexToHue(hex) { // sRGB hex -> OKLCH hue
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(m[1].slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const mm = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const A = 1.9779984951 * l - 2.4285922050 * mm + 0.4505937099 * s;
  const B = 0.0259040371 * l + 0.7827717662 * mm - 0.8086757660 * s;
  return Math.round(((Math.atan2(B, A) * 180) / Math.PI + 360) % 360);
}

function accentL() { return document.documentElement.getAttribute('data-theme') === 'light' ? 0.50 : 0.56; }

export function applyAccent(hue) {
  const h = ((Math.round(hue) % 360) + 360) % 360;
  const root = document.documentElement;
  root.style.setProperty('--h', h);
  const acc = oklchToRgb(accentL(), accentL() === 0.5 ? 0.11 : 0.105, h);
  const white = contrast(1, lum(acc));
  const black = contrast(0, lum(acc));
  // Use the higher-contrast of white and black on the rendered accent.
  root.style.setProperty('--on-accent', white >= black ? '#ffffff' : '#000000');
  root.dataset.accent = String(h);
  return { hue: h, hex: toHex(acc), ratio: Math.max(white, black).toFixed(1), ink: white >= black ? 'white' : 'dark' };
}

let savedHue = DEFAULT_HUE;
let serial = Promise.resolve();
let revision = 0;
export async function syncAccent() {
  const at = revision;
  try {
    const value = await appState('ui.accent');
    if (at === revision) {
      savedHue = applyAccent(Number.isFinite(value?.hue) ? value.hue : DEFAULT_HUE).hue;
      document.getElementById('settings-appearance')?.dispatchEvent(new Event('accentloaded'));
    }
  } catch { /* Keep this browser's cached preference while disconnected. */ }
}

function storeAccent(hue, report) {
  const at = ++revision;
  serial = serial.then(async () => {
    if (at !== revision) return;
    try {
      await setAppState('ui.accent', { hue });
      savedHue = hue;
    } catch (err) {
      if (at === revision) {
        report(savedHue, false);
        toast('Accent could not be saved. ' + err.message, true);
      }
    }
  });
}

export function mountWheel(panel) {
  if (panel.querySelector('.accent-picker')) return;
  let hue = Number(document.documentElement.dataset.accent ?? DEFAULT_HUE);
  const knob = el('div', { class: 'wheel-knob' });
  const wrap = el('div', { class: 'wheel-wrap', role: 'slider', tabindex: '0', 'aria-label': 'Accent colour',
    'aria-valuemin': '0', 'aria-valuemax': '359' }, el('div', { class: 'wheel' }), el('div', { class: 'wheel-centre' }), knob);
  const hex = el('input', { type: 'text', 'aria-label': 'Accent colour as a hex code', spellcheck: 'false', help: 'Choose an accent hue from a hexadecimal colour.' });
  const note = el('p', { class: 'contrast-note', role: 'status' });
  const swatches = PRESETS.map((h) => el('button', { type: 'button', class: 'swatch', help: 'Apply this accent hue.', 'aria-label': `Accent hue ${h}`,
    style: `background: oklch(0.56 0.105 ${h})`, onclick: () => set(h, true) }));
  const onTheme = () => set(hue, false);
  panel.addEventListener('themechange', onTheme);
  panel.addEventListener('accentloaded', () => set(savedHue, false));
  function set(h, save) {
    hue = ((Math.round(h) % 360) + 360) % 360;
    const r = applyAccent(hue);
    const a = hue * Math.PI / 180;
    knob.style.left = `${66 + 52 * Math.cos(a)}px`;
    knob.style.top = `${66 + 52 * Math.sin(a)}px`;
    wrap.setAttribute('aria-valuenow', String(hue));
    wrap.setAttribute('aria-valuetext', `hue ${hue}, ${r.hex}`);
    hex.value = r.hex;
    hex.removeAttribute('aria-invalid');
    swatches.forEach((s, i) => s.setAttribute('aria-pressed', String(PRESETS[i] === hue)));
    note.textContent = `Buttons use ${r.ink} text on this colour (contrast ${r.ratio}:1). Status colours do not change.`;
    if (save) storeAccent(hue, set);
  }
  function fromPointer(e) {
    const r = wrap.getBoundingClientRect();
    const ang = (Math.atan2(e.clientY - (r.top + r.height / 2), e.clientX - (r.left + r.width / 2)) * 180) / Math.PI;
    set(ang, false);
  }
  wrap.addEventListener('pointerdown', (e) => { if (e.button === 0) { wrap.setPointerCapture(e.pointerId); fromPointer(e); } });
  wrap.addEventListener('pointermove', (e) => { if (wrap.hasPointerCapture(e.pointerId)) fromPointer(e); });
  wrap.addEventListener('pointerup', (e) => { if (wrap.hasPointerCapture(e.pointerId)) { fromPointer(e); storeAccent(hue, set); } });
  wrap.addEventListener('pointercancel', () => set(savedHue, false));
  wrap.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 15 : 3;
    const map = { ArrowRight: step, ArrowUp: step, ArrowLeft: -step, ArrowDown: -step, PageUp: 30, PageDown: -30 };
    if (e.key in map) { e.preventDefault(); set(hue + map[e.key], true); }
    if (e.key === 'Home') { e.preventDefault(); set(DEFAULT_HUE, true); }
  });
  hex.addEventListener('change', () => {
    const h = hexToHue(hex.value);
    if (h === null) { hex.setAttribute('aria-invalid', 'true'); note.textContent = 'Enter a colour like #1d7c81.'; return; }
    set(h, true);
  });
  const reset = el('button', { type: 'button', class: 'btn small', help: 'Restore the default accent hue.', onclick: () => set(DEFAULT_HUE, true) }, 'Reset to teal');
  panel.append(el('details', { class: 'more' },
    el('summary', { help: 'Open the colour wheel, presets and custom hue entry.' }, 'Customize accent colour'),
    el('p', { class: 'help small muted' }, 'Choose a hue with the wheel, arrow keys, swatches or hex input. Lightness adjusts for readability.'),
    el('div', { class: 'accent-picker' }, wrap,
      el('div', { class: 'accent-side' }, el('div', { class: 'swatches' }, swatches),
        el('div', { class: 'row' }, hex, reset), note))));
  set(hue, false);
}
