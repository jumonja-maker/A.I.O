/* A.I.O → native LibreOffice integration without Converter Guard.
 * This file contains transport only. Neither legacy converter routes nor
 * Guard policies are imported or evaluated. No pretend output on errors.
 */
(() => {
  'use strict';
  const bridge = window.AllInOneBridge;
  const token = document.body.dataset.aioToken;
  if (!bridge || !token) return;
  const acceptedExtensions = '.docx,.doc,.odt,.rtf,.txt,.html,.htm,.xlsx,.xls,.ods,.csv,.pptx,.ppt,.odp';
  const MIME = Object.freeze({
    pdf:'application/pdf', docx:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    doc:'application/msword', odt:'application/vnd.oasis.opendocument.text',
    rtf:'application/rtf', txt:'text/plain', html:'text/html',
    xlsx:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    xls:'application/vnd.ms-excel', ods:'application/vnd.oasis.opendocument.spreadsheet',
    csv:'text/csv', pptx:'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    ppt:'application/vnd.ms-powerpoint', odp:'application/vnd.oasis.opendocument.presentation'
  });
  function fail(message) {
    const e = new Error(message);
    e.userMessage = message;
    throw e;
  }
  function selectedFiles(input) {
    const files = Array.from(input || []);
    if (!files.length || files.length > 100) fail('Выберите от 1 до 100 файлов.');
    const total = files.reduce((n, x) => n + (x.file?.size || 0), 0);
    if (total > 50 * 1024 * 1024 ||
        files.some(x => !x.file || !x.name || !x.id ||
          x.file.size < 1 || x.file.size > 20 * 1024 * 1024))
      fail('Общий размер не более 50 МБ, отдельный файл не более 20 МБ.');
    return files;
  }
  function requestForm(files, formats) {
    const payload = new FormData();
    payload.append('ids', JSON.stringify(files.map(x => String(x.id))));
    for (const x of files) payload.append('files', x.file, x.name);
    if (formats?.length === 1) payload.append('format', formats[0]);
    else if (formats?.length > 1) payload.append('formats', JSON.stringify(formats));
    return payload;
  }
  async function send(operation, files, formats) {
    let response;
    try {
      response = await fetch('/api/converter/' + operation, {
        method:'POST',
        headers:{'X-AIO-Token':token},
        body:requestForm(files, formats),
        cache:'no-store'
      });
    } catch {
      fail('Не удалось связаться с локальным движком LibreOffice.');
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const err = new Error(payload.error || 'Конвертация не удалась.');
      err.userMessage = err.message;
      err.code = payload.code || 'LIBREOFFICE_REQUEST_FAILED';
      throw err;
    }
    return response;
  }
  function outputName(files, formats) {
    if (files.length !== 1 || formats.length !== 1)
      return 'Конвертированные_файлы.zip';
    const base = String(files[0].name).replace(/\.[^.]*$/, '')
      .replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 100) || 'Документ';
    return base + '.' + formats[0];
  }
  async function execute(files, formats, onProgress) {
    const selected = selectedFiles(files);
    if (!Array.isArray(formats) || !formats.length ||
        new Set(formats).size !== formats.length ||
        formats.some(x => !Object.hasOwn(MIME, x)))
      fail('Некорректный выбор форматов.');
    onProgress?.({message:'LibreOffice преобразует выбранные файлы…'});
    const response = await send('convert', selected, formats);
    const expectedMime = selected.length * formats.length > 1 ?
      'application/zip' : MIME[formats[0]];
    const actualMime = (response.headers.get('Content-Type') || '')
      .split(';')[0].trim().toLowerCase();
    if (actualMime !== expectedMime) fail('LibreOffice вернул неожиданный формат результата.');
    const blob = await response.blob();
    if (!blob.size) fail('LibreOffice не создал готовый файл.');
    onProgress?.({message:'Результат готов к сохранению.'});
    return {ok:true, result:{blob, name:outputName(selected,formats)},
      message:'LibreOffice завершил конвертацию.'};
  }
  bridge.converter = Object.freeze({
    acceptedExtensions,
    resetProgress(){},
    async inspectFiles({files} = {}) {
      const chosen = selectedFiles(files);
      const response = await send('inspect',chosen,null);
      const data = await response.json();
      if (!Array.isArray(data.files) || !Array.isArray(data.formats) ||
          data.files.length !== chosen.length ||
          data.files.some((x,i) => String(x.id) !== String(chosen[i].id)))
        fail('Неверный ответ LibreOffice при проверке файлов.');
      return data;
    },
    async convert({files,formatId,onProgress}) {
      return execute(files,[formatId],onProgress);
    },
    async convertMany({files,formatIds,onProgress}) {
      return execute(files,formatIds,onProgress);
    },
    async retryFailed({result,files,onProgress}) {
      const formats = result?.formatIds || [];
      if (!formats.length) fail('Нет результатов для повторной конвертации.');
      return execute(files, formats,onProgress);
    },
    async saveResult({result}) {
      if (!result?.blob || !result.name) fail('Результат не готов к сохранению.');
      const saved = await bridge.saveExcel(result);
      return {saved: saved?.saved === true, cancelled: saved?.cancelled === true};
    },
    async diagnoseFiles() {
      return {status:'unavailable', issues:[],
        message:'Сравнение до и после будет добавлено отдельно после передачи модуля Guard.'};
    }
  });
})();
