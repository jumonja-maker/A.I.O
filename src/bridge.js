// 00 integration: real local XLSX merge, browser save, explicit GitHub release lookup.
(() => {
  'use strict';
  const token = document.body.dataset.aioToken;
  if (!token) return; // HTML designer preview can still run without server.
  let nativeSaveSequence = 0;
  function fail(message) { throw new Error(message); }
  window.AllInOneBridge = {
    async mergeExcel(files, onProgress) {
      const payload = new FormData();
      for (const f of files) payload.append('files', f, f.name);
      // A random job ID keeps progress from two concurrent windows separate.
      // No filenames, spreadsheet contents or personal data enter the status API.
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
        b => b.toString(16).padStart(2, '0')).join('');
      let timer = null;
      let polling = false;
      let finished = false;
      async function poll() {
        if (finished || polling || typeof onProgress !== 'function') return;
        polling = true;
        try {
          const response = await fetch('/api/merge-progress?job=' + id, {
            headers: { 'X-AIO-Token': token }, cache: 'no-store'
          });
          if (response.ok) {
            const info = await response.json();
            // The merge can finish while either fetch or JSON decoding waits.
            if (!finished) onProgress(info);
          }
        } catch {
          // Polling is optional: the actual merge request remains authoritative.
        } finally {
          polling = false;
        }
      }
      if (typeof onProgress === 'function') {
        onProgress({phase: 'upload', current: 0, total: 0});
        timer = setInterval(poll, 400);
      }
      try {
        const response = await fetch('/api/merge', {
          method: 'POST',
          headers: { 'X-AIO-Token': token, 'X-AIO-Job': id },
          body: payload
        });
        if (!response.ok) {
          const err = await response.json().catch(() => ({}));
          fail(err.error || 'Ошибка обработки таблиц.');
        }
        return { blob: await response.blob(), name: 'Объединённая_таблица.xlsx' };
      } finally {
        finished = true;
        if (timer !== null) clearInterval(timer);
      }
    },
    async saveExcel(result) {
      if (!result || (!result.blob && !result.downloadUrl))
        fail('Результат объединения недоступен.');
      // Authenticated loopback downloads are already served by a bounded
      // ticket stream. In a regular browser, trigger the same-origin download
      // without buffering a giant Blob; only the native shell can confirm an
      // actual disk save. A browser download initiation is NOT saved=true.
      if (result.blob && typeof window.showSaveFilePicker === 'function') {
        try {
          const ext = '.' + String(result.name).toLowerCase().split('.').pop();
          const choices = {
            '.txt': ['Текст TXT', 'text/plain'],
            '.html': ['Веб-страница HTML', 'text/html'],
            '.rtf': ['Документ RTF', 'application/rtf'],
            '.odt': ['Документ OpenDocument', 'application/vnd.oasis.opendocument.text'],
            '.csv': ['Таблица CSV', 'text/csv'],
            '.xlsx': ['Excel XLSX', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
            '.ods': ['OpenDocument ODS', 'application/vnd.oasis.opendocument.spreadsheet'],
            '.xls': ['Excel XLS 97–2003', 'application/vnd.ms-excel'],
            '.docx': ['Документ Word DOCX', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
            '.doc': ['Документ Word DOC', 'application/msword'],
            '.pptx': ['Презентация PowerPoint PPTX', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'],
            '.ppt': ['Презентация PowerPoint PPT', 'application/vnd.ms-powerpoint'],
            '.odp': ['Презентация OpenDocument ODP', 'application/vnd.oasis.opendocument.presentation'],
            '.pdf': ['Документ PDF', 'application/pdf'],
            '.png': ['Изображение PNG', 'image/png'],
            '.jpg': ['Изображение JPEG', 'image/jpeg'],
            '.jpeg': ['Изображение JPEG', 'image/jpeg'],
            '.webp': ['Изображение WebP', 'image/webp'],
            '.bmp': ['Изображение BMP', 'image/bmp'],
            '.tiff': ['Изображение TIFF', 'image/tiff'],
            '.gif': ['Изображение GIF', 'image/gif'],
            '.zip': ['Архив ZIP', 'application/zip']
          };
          if (!choices[ext]) fail('Неизвестное расширение результата.');
          const [description, mime] = choices[ext];
          const type = {description, accept: {[mime]: [ext]}};
          const handle = await window.showSaveFilePicker({
            suggestedName: result.name, types: [type]
          });
          const stream = await handle.createWritable();
          try {
            await stream.write(result.blob);
            await stream.close();
          } catch (error) {
            // Release the file and discard any uncommitted partial write.
            // Cleanup failure must not hide the original storage error.
            try { await stream.abort(); } catch {}
            throw error;
          }
          return { name: handle.name, saved: true };
        } catch (err) {
          if (err && err.name === 'AbortError') return { cancelled: true };
          throw err;
        }
      }
      // Other browsers: initiating a download cannot prove the user saved it.
      let url = result.downloadUrl || URL.createObjectURL(result.blob);
      if (result.downloadUrl &&
          !/^\/api\/converter\/result\?ticket=[A-Za-z0-9_-]{32,128}$/.test(url))
        fail('Недопустимый путь сохранения результата.');
      // A retained ticket may be saved again after a cancel/timeout. Give
      // EACH attempt a distinct URL; even the same result cannot consume a
      // late acknowledgement from its previous native download.
      if (window.AllInOneNativeShell && result.downloadUrl)
        url += '&aioSave=' + (++nativeSaveSequence);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = result.name;
      document.body.append(anchor);
      // Native Qt exposes an actual download completion/cancellation event.
      // Waiting for it prevents the UI from claiming success before the OS
      // save dialog and file write have actually finished.
      let disposeConfirmation = () => {};
      const confirmation = window.AllInOneNativeShell
        ? new Promise((resolve, reject) => {
            const sourceUrl = new URL(url, window.location.href).href;
            function cleanup() {
              clearTimeout(timeout);
              window.removeEventListener('aio-native-save-result', onResult);
            }
            const timeout = setTimeout(() => {
              cleanup();
              reject(new Error('Не удалось подтвердить сохранение файла.'));
            }, 300_000);
            function onResult(event) {
              const detail = event.detail || {};
              if (detail.sourceUrl !== sourceUrl ||
                  !['completed', 'cancelled', 'failed'].includes(detail.status)) return;
              cleanup();
              if (detail.status === 'completed') {
                resolve({ name: detail.name || result.name, saved: true });
              } else if (detail.status === 'cancelled') {
                resolve({ cancelled: true });
              } else {
                reject(new Error('Не удалось сохранить файл.'));
              }
            }
            window.addEventListener('aio-native-save-result', onResult);
            // Also settle an abandoned promise if anchor.click() throws.
            disposeConfirmation = () => {cleanup(); resolve({cancelled:true});};
          })
        : null;
      try {
        anchor.click();
        if (confirmation) return await confirmation;
        return { name: result.name, downloadStarted: true };
      } finally {
        anchor.remove();
        disposeConfirmation();
        if (!result.downloadUrl) {
          if (window.AllInOneNativeShell) URL.revokeObjectURL(url);
          else setTimeout(() => URL.revokeObjectURL(url), 60_000);
        }
      }
    },
    async setWindowIconTheme(details) {
      const theme = details && details.theme;
      if (theme !== 'light' && theme !== 'dark') return;
      const response = await fetch('/api/icon-theme', {
        method: 'POST',
        headers: { 'X-AIO-Token': token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme })
      });
      if (!response.ok) throw new Error('Не удалось переключить иконку окна.');
    },
    async checkForUpdates() {
      const response = await fetch('/api/updates', { headers: { 'X-AIO-Token': token } });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) fail(payload.error || 'Ошибка проверки релизов.');
      return payload;
    },
    async installUpdate() {
      // Only after a real user confirms. Backend re-checks version and SHA-256.
      const response = await fetch('/api/install-update', {
        method: 'POST', headers: { 'X-AIO-Token': token }, body: ''
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) fail(payload.error || 'Не удалось установить обновление.');
      return payload;
    }
  };
})();
