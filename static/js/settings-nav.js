// Runs in <head> before styles/other scripts: mark a standalone settings page
// so its Back / Refresh nav is always present, even if later JS fails.
(function () {
  try {
    if (window.self === window.top) document.documentElement.classList.add('settings-top');
  } catch (_) {
    document.documentElement.classList.add('settings-top');
  }
})();
