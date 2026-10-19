/* Brand addon v1.2. UI image and favicon switch with app theme.
   Optional native host updates its window/taskbar icon using setWindowIconTheme.
   Desktop shortcuts are OS resources: see 00_BRAND_INTEGRATION.md. */
(() => {
  'use strict';
  const ASSET_ROOT = 'assets/brand/';
  // One approved application mark in both themes; Elva's responsive header
  // image is independent and must never be replaced by a small favicon.
  const mark = Object.freeze({
    svg: ASSET_ROOT + 'icons/aio-256.png', // legacy property, raster mark
    ico: ASSET_ROOT + 'icons/aio.ico',
    icns: null, // this Windows/Linux patch does not invent an ICNS resource
    png: ASSET_ROOT + 'icons/aio-256.png'
  });
  const icons = Object.freeze({ light: mark, dark: mark });
  function setTheme(theme) {
    if (theme !== 'light' && theme !== 'dark') return;
    const files = icons[theme];
    const mark = document.querySelector('[data-aio-app-logo]');
    if (mark) mark.setAttribute('src', files.png);
    const favicon = document.getElementById('app-favicon');
    if (favicon) {
      favicon.setAttribute('href', ASSET_ROOT + 'icons/aio-64.png');
      favicon.setAttribute('type', 'image/png');
    }
    // Give native hosts a stable, explicit contract without assuming the GUI stack.
    // Theme state remains in app.js; the UI never modifies desktop shortcuts itself.
    window.dispatchEvent(new CustomEvent('aio:brand-theme', {
      detail: { theme, ...files }
    }));
    const host = window.AllInOneBridge;
    if (host && typeof host.setWindowIconTheme === 'function') {
      try {
        Promise.resolve(host.setWindowIconTheme({ theme, ...files })).catch((err) => {
          console.warn('Не удалось применить иконку окна:', err);
        });
      } catch (err) {
        console.warn('Не удалось применить иконку окна:', err);
      }
    }
  }
  window.AIOBranding = Object.freeze({ setTheme, icons });
})();
