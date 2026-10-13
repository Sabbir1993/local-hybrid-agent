/* Removes the start-up splash (#splash, see splash.css) once the page has loaded and the first status poll answered, so the
   app appears fully built and themed. Never traps the user: it also goes after a fixed time. */
(function () {
  'use strict';
  var s = document.getElementById('splash');
  if (!s) return;
  var gone = false;
  function hide() {
    if (gone) return;
    gone = true;
    s.classList.add('splash-out');
    setTimeout(function () { if (s.parentNode) s.parentNode.removeChild(s); }, 400);
  }
  function waitForApp() {
    var t0 = Date.now();
    (function check() {
      // curStatus is set by the first /control/status poll (gpu-status.js); give up waiting after 3 s
      if ((typeof curStatus !== 'undefined' && curStatus) || Date.now() - t0 > 3000) hide();
      else setTimeout(check, 80);
    })();
  }
  if (document.readyState === 'complete') waitForApp(); else window.addEventListener('load', waitForApp);
  setTimeout(hide, 10000);
})();
