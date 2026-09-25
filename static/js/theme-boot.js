/* Applied before first paint (loaded synchronously in <head>) so pages don't flash
 * the default theme. External file, not inline, so the CSP can forbid inline script. */
(function () {
  var t = localStorage.getItem('a770_theme') || 'claude';
  document.documentElement.setAttribute('data-theme', t);
})();
