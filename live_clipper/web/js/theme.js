// Appearance: dark by default, light when chosen. The saved preference is app state `ui.theme`; a cookie lets
// theme-boot.js apply it before the first paint.

import { appState, setAppState } from './api.js';

export const THEMES = { dark: 'Dark', light: 'Light' };

export function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

export function applyTheme(theme) {
  const t = theme === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', t);
  document.cookie = `lc_theme=${t}; path=/; max-age=31536000; samesite=strict`;
  return t;
}

// The saved preference wins over whatever the cookie said (another browser, or a cleared cookie).
export async function syncTheme() {
  try { applyTheme(await appState('ui.theme')); } catch { /* not signed in yet: keep what the page shows */ }
}

export async function saveTheme(theme) {
  await setAppState('ui.theme', theme);
  return applyTheme(theme);
}
