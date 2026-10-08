/* Applied before first paint (loaded synchronously in <head>) so pages don't flash
 * the default theme. External file, not inline, so the CSP can forbid inline script. */
(function () {
  var t = localStorage.getItem('a770_theme') || 'claude';
  document.documentElement.setAttribute('data-theme', t);
  var isNative = !!(
    (typeof window !== 'undefined' && window.electronAPI && window.electronAPI.isNativeApp) ||
    (typeof navigator !== 'undefined' && (navigator.userAgent.includes("A770NativeApp") || navigator.userAgent.includes("Electron")))
  );
  if (isNative) {
    document.documentElement.classList.add('is-native-app');
  } else {
    document.documentElement.classList.remove('is-native-app');
    try { localStorage.setItem('app_mode', 'chat'); } catch (_) {}
  }
  var mode = localStorage.getItem('app_mode') || 'chat';
  document.documentElement.setAttribute('data-app-mode', mode);
})();
