/* Separate LibreOffice website link, mirroring GitHub header behavior.
   The document artwork is original, monochrome and theme-driven. */
(() => {
  'use strict';
  const OFFICIAL_URL = 'https://www.libreoffice.org/';
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function documentMark() {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.classList.add('aio-lo-mark');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');

    // Clean folded document with three editorial lines, sized to the GitHub mark.
    for (const d of [
      'M6.5 2.5h7l5 5v12.25a2 2 0 0 1-2 2h-10a2 2 0 0 1-2-2V4.5a2 2 0 0 1 2-2Z',
      'M13.5 2.5v4.8c0 .4.3.7.7.7h4.3',
      'M9 12h6.3M9 15.5h6.3M9 19h4.3'
    ]) {
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      svg.appendChild(path);
    }
    return svg;
  }

  function showOpenError() {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = 'Не удалось открыть сайт LibreOffice в браузере.';
    toast.hidden = false;
  }

  function mount() {
    const header = document.querySelector('.app-header');
    if (!header || document.getElementById('aio-libreoffice-link')) return;

    const anchor = document.createElement('a');
    anchor.id = 'aio-libreoffice-link';
    anchor.className = 'aio-libreoffice-link';
    anchor.href = OFFICIAL_URL;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer external';
    anchor.referrerPolicy = 'no-referrer';
    anchor.title = 'Открыть официальный сайт LibreOffice';
    anchor.setAttribute('aria-label', 'Открыть официальный сайт LibreOffice в браузере');
    anchor.appendChild(documentMark());

    anchor.addEventListener('click', (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey ||
          event.shiftKey || event.altKey || event.metaKey) return;
      const bridge = window.AllInOneBridge;
      // Desktop bridge if exposed; otherwise normal anchor handled by the host.
      if (bridge && typeof bridge.openExternalUrl === 'function') {
        event.preventDefault();
        try {
          Promise.resolve(bridge.openExternalUrl(OFFICIAL_URL)).then(
            (result) => { if (result === false) showOpenError(); },
            () => showOpenError()
          );
        } catch (_) { showOpenError(); }
      }
    });

    const github = document.getElementById('aio-github-link');
    if (github && github.parentNode === header) {
      header.insertBefore(anchor, github);
      header.classList.add('aio-has-libreoffice-link');
    } else {
      header.appendChild(anchor);
    }
  }

  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', mount, { once: true });
  else mount();
})();
