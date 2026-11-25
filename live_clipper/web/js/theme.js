import { appState, setAppState } from './api.js';
import { applyAccent, syncAccent } from './accent.js';

export const THEMES = { auto: 'Automatic', dark: 'Dark', light: 'Light' };
const system = matchMedia('(prefers-color-scheme: dark)');
export function currentTheme() { return document.documentElement.dataset.themePreference || 'auto'; }

export function applyTheme(value) {
  const t = Object.hasOwn(THEMES, value) ? value : 'auto';
  const root = document.documentElement;
  root.dataset.themePreference = t;
  root.dataset.theme = t === 'auto' ? (system.matches ? 'dark' : 'light') : t;
  document.cookie = `lc_theme=${t}; path=/; max-age=31536000; samesite=strict`;
  applyAccent(Number(root.dataset.accent ?? 200));
  document.getElementById('settings-appearance')?.dispatchEvent(new Event('themechange'));
  return t;
}
system.addEventListener('change', () => { if (currentTheme() === 'auto') applyTheme('auto'); });

export async function syncTheme() {
  try { applyTheme(await appState('ui.theme')); } catch { /* Keep the cached theme. */ }
  await syncAccent();
}

export async function saveTheme(theme) {
  await setAppState('ui.theme', theme);
  return applyTheme(theme);
}
