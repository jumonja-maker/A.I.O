/* Brand addon v1.2. UI image and favicon switch with app theme.
   Optional native host updates its window/taskbar icon using setWindowIconTheme.
   Desktop shortcuts are OS resources: see 00_BRAND_INTEGRATION.md. */
(() => {
  'use strict';
  const ASSET_ROOT = 'assets/brand/';
  const icons = Object.freeze({
    light: Object.freeze({
      // Legacy svg property retained for existing listeners; it points to the
      // approved raster mark so no UI consumer displays the retired SVG.
      svg: ASSET_ROOT + 'elva-v13-happy-thumb.webp',
      ico: ASSET_ROOT + 'elva-v13-happy-thumb.webp',
      icns: ASSET_ROOT + 'elva-v13-happy-thumb.webp',
      png: ASSET_ROOT + 'elva-v13-happy-thumb.webp'
    }),
    dark: Object.freeze({
      // Legacy svg property retained for existing listeners; it points to the
      // approved raster mark so no UI consumer displays the retired SVG.
      svg: ASSET_ROOT + 'elva-v13-happy-thumb.webp',
      ico: ASSET_ROOT + 'elva-v13-happy-thumb.webp',
      icns: ASSET_ROOT + 'elva-v13-happy-thumb.webp',
      png: ASSET_ROOT + 'elva-v13-happy-thumb.webp'
    })
  });
  function setTheme(theme) {
    if (theme !== 'light' && theme !== 'dark') return;
    const files = icons[theme];
    const mark = document.querySelector('[data-aio-app-logo]');
    if (mark) mark.setAttribute('src', files.png);
    const favicon = document.getElementById('app-favicon');
    if (favicon) favicon.setAttribute('href', files.png);
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
