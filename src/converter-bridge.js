/* New Converter Guard boundary.
 * The approved DESIGN is mounted from its archived designer snapshot, but the
 * old converter controller, filters and conversion policies are NOT imported.
 * Conversion remains intentionally unavailable until the new Guard is ready.
 * Do not invent a route, an output or a success status.
 */
(() => {
  'use strict';
  const bridge = window.AllInOneBridge;
  if (!bridge) return;
  const extensions = '.docx,.doc,.odt,.rtf,.txt,.html,.htm,.xlsx,.xls,.ods,.csv,.pptx,.ppt,.odp,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tiff,.tif';
  function unavailable() {
    const err = new Error('Новый A.I.O Converter Guard ещё не подключён. Конвертация временно недоступна.');
    err.code = 'GUARD_NOT_READY';
    err.userMessage = err.message;
    throw err;
  }
  function reset() {}
  bridge.converter = Object.freeze({
    acceptedExtensions: extensions,
    // Provide an empty, honest capability catalogue, never the old
    // synthetic allowlist that previously blocked or misrepresented LibreOffice.
    async inspectFiles({files} = {}) {
      const input = Array.from(files || []);
      return {
        files: input.map(f => ({
          id: String(f.id || ''), name: String(f.name || f.file?.name || ''),
          formats: [], capabilities: []
        })),
        formats: [],
        message: 'Интерфейс сохранён. Новый Converter Guard пока не подключён.'
      };
    },
    async diagnoseFiles() { return {status:'unavailable',issues:[],message:'Диагностика Guard пока не подключена.'}; },
    async convert() { unavailable(); },
    async convertMany() { unavailable(); },
    async retryFailed() { unavailable(); },
    async saveResult() { unavailable(); },
    resetProgress: reset
  });
  // A clear status, also visible when the old frontend hides disabled formats.
  window.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('tool-root');
    if (!root) return;
    const note = document.createElement('p');
    note.className = 'guard-migration-note';
    note.textContent = 'Новая основа A.I.O: конвертер ожидает подключения Converter Guard. Объединитель Excel работает отдельно.';
    note.setAttribute('role', 'status');
    root.addEventListener('click', () => {
      const pane = root.querySelector('[data-tool-id="file-converter"]');
      if (pane && !pane.querySelector('.guard-migration-note')) pane.prepend(note);
    });
  });
})();
