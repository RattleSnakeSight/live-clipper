// Resolve the saved preference before first paint, including on the update screen.
(function () {
  const choice = document.cookie.match(/(?:^|; )lc_theme=(light|dark|auto)(?:;|$)/)?.[1] || 'auto';
  const root = document.documentElement;
  root.dataset.themePreference = choice;
  root.dataset.theme = choice === 'auto' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : choice;
}());
