/* AIO GitHub Link Addon v1.3. Self-contained and dependency-free.
   Adds exactly ONE external-link control to .app-header, without touching app.js. */
(() => {
  'use strict';
  // Verified private project repository; external access still needs permission.
  const DEFAULT_URL = 'https://github.com/jumonja-maker/All-in-One-';
  const ASSET_DIR = 'addons/github-link/assets/';

  function safeGitHubUrl(input) {
    try {
      const candidate = new URL(input || DEFAULT_URL);
      if (candidate.protocol !== 'https:' || candidate.hostname !== 'github.com' ||
          candidate.port || candidate.username || candidate.password) return DEFAULT_URL;
      return candidate.href;
    } catch (_) { return DEFAULT_URL; }
  }

  function mark(name, src) {
    const img = document.createElement('img');
    img.className = 'aio-gh-mark aio-gh-mark-' + name;
    img.src = ASSET_DIR + src;
    img.alt = '';
    img.width = 22;
    img.height = 22;
    img.setAttribute('aria-hidden', 'true');
    return img;
  }

  function errorNotice() {
    const toast = document.getElementById('toast');
    if (toast) {
      toast.textContent = 'Не удалось открыть GitHub в браузере.';
      toast.hidden = false;
    }
  }

  function mount() {
    const header = document.querySelector('.app-header');
    if (!header || document.getElementById('aio-github-link')) return;

    const url = safeGitHubUrl(window.AIO_GITHUB_URL);
    const anchor = document.createElement('a');
    anchor.id = 'aio-github-link';
    anchor.className = 'aio-github-link';
    anchor.href = url;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer external';
    anchor.referrerPolicy = 'no-referrer';
    anchor.title = 'Открыть GitHub проекта';
    anchor.setAttribute('aria-label', 'Открыть GitHub в браузере');
    anchor.append(mark('light', 'github-black.svg'), mark('dark', 'github-white.svg'));

    anchor.addEventListener('click', function (event) {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey ||
          event.shiftKey || event.altKey || event.metaKey) return;
      const bridge = window.AllInOneBridge;
      // Browser: standard external anchor. Native host: delegate to OS browser.
      if (bridge && typeof bridge.openExternalUrl === 'function') {
        event.preventDefault();
        try {
          Promise.resolve(bridge.openExternalUrl(url)).then(
            (result) => { if (result === false) errorNotice(); },
            () => errorNotice()
          );
        } catch (_) { errorNotice(); }
      }
    });
    header.appendChild(anchor);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, {once:true});
  else mount();
})();
