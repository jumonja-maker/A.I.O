/* Owner-approved A.I.O title and Cycle navigation icon (UI 11 additive layer).
   Original signed Designer 11, themes, help animations and Excel are untouched. */
(() => {
  'use strict';
  const nav = document.getElementById('tool-nav');
  const root = document.getElementById('tool-root');
  const svgNS = 'http://www.w3.org/2000/svg';
  const paths = Object.freeze([
    'M20 11a8 8 0 0 0-14-5L4 8',
    'M4 3v5h5',
    'M4 13a8 8 0 0 0 14 5l2-2',
    'M20 21v-5h-5'
  ]);
  function update() {
    const heading = root?.querySelector('.aio-mounted-tool:not([hidden]) .aio-cv__heading h1');
    if (heading && heading.textContent.trim() === 'Конвертер файлов') {
      heading.textContent = 'Конвертер файлов X LibreOffice';
    }
    const isConverter = Boolean(root?.querySelector('.aio-mounted-tool:not([hidden]) .aio-cv__pane--right'));
    root?.classList.toggle('aio-qa-converter-mode', isConverter);
    for (const row of nav?.querySelectorAll('.nav-item') || []) {
      const name = row.querySelector('.tool-name');
      if (name?.textContent?.trim() !== 'Конвертер файлов') continue;
      const badge = row.querySelector('.tool-glyph');
      if (!badge || badge.dataset.aioDesignCycleIcon === 'true') break;
      const svg = document.createElementNS(svgNS, 'svg');
      svg.setAttribute('viewBox','0 0 24 24');
      svg.setAttribute('aria-hidden','true');
      svg.setAttribute('focusable','false');
      for (const d of paths) {
        const path = document.createElementNS(svgNS,'path');
        path.setAttribute('d',d);
        svg.appendChild(path);
      }
      badge.replaceChildren(svg);
      badge.dataset.aioDesignCycleIcon='true';
      break;
    }
  }
  update();
  if (root) new MutationObserver(update).observe(root,{childList:true,subtree:true,attributes:true,attributeFilter:['data-active-tool']});
  if (nav) new MutationObserver(update).observe(nav,{childList:true});
})();