// Runs before the page paints, so there is no flash of the wrong colours: dark unless this browser was told
// "light" in Settings > Appearance. The cookie mirrors the saved preference (app state `ui.theme`) and, unlike
// browser storage, is shared by every port the app may start on.
(function () {
  var m = document.cookie.match(/(?:^|; )lc_theme=(light|dark)/);
  document.documentElement.setAttribute('data-theme', m ? m[1] : 'dark');
}());
