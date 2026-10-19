/* QA-only experimental hover movement for designer's exact flyout component.
   A one-line flag isolates gentle float from open/close bloom animation. */
(() => {
  'use strict';
  const EXPERIMENTAL_FLOAT = true; // Owner will decide after Windows Setup trial.
  if (!EXPERIMENTAL_FLOAT) return;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  document.addEventListener('animationend', (event) => {
    const popup = event.target;
    if (event.animationName !== 'aio-cv-bloom' ||
        !popup?.classList?.contains('aio-cv-flyout') ||
        !popup.isConnected || reduce.matches) return;
    popup.classList.add('aio-cv-flyout--float');
  });
  // Do not change the approved source module or the positions of its cards.
})();
