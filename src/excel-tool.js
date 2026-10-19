// UI первой функции. Excel-логика отсутствует: её предоставляет AllInOneBridge.
(() => {
  'use strict';
  window.AIO.registerTool({
    id: 'excel-merge',
    title: 'Объединение Excel',
    iconText: 'X',
    mount(root, context) {
      const state = {
        files: new Map(), phase: 'empty', result: null,
        error: null, errorStage: 'merge', savedName: null
      };
      const input = document.createElement('input');
      input.type = 'file';
      input.multiple = true;
      input.accept = '.xlsx';
      input.hidden = true;
      root.appendChild(input);
      let destroyed = false;
      let draggedKey = null;
      function elt(tag, className, text) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (text !== undefined) el.textContent = text;
        return el;
      }
      function button(label, className, callback, disabled = false) {
        const b = elt('button', className, label);
        b.type = 'button'; b.disabled = disabled;
        b.addEventListener('click', callback);
        return b;
      }
      function openFiles() { if (state.phase !== 'processing' && state.phase !== 'saving') input.click(); }
      function displayProgress(info) {
        if (destroyed || state.phase !== 'processing') return;
        if (!info || typeof info.phase !== 'string') return;
        const current = Number.isInteger(info.current) ? info.current : 0;
        const total = Number.isInteger(info.total) ? info.total : 0;
        let message = 'Передаём файлы для обработки…';
        if (info.phase === 'read' && total > 0)
          message = `Проверяем исходный файл ${current} из ${total}…`;
        else if (info.phase === 'write' && total > 0)
          message = `Записываем строки: ${current.toLocaleString('ru-RU')} из ${total.toLocaleString('ru-RU')}`;
        else if (info.phase === 'save')
          message = 'Формируем итоговый Excel-файл…';
        else if (info.phase === 'verify')
          message = 'Проверяем готовый файл…';
        const line = root.querySelector('.merge-progress');
        if (line) line.textContent = message;
        const meter = root.querySelector('.merge-progress-meter');
        if (meter) {
          meter.hidden = info.phase !== 'write' || total < 1;
          if (!meter.hidden) {
            meter.max = total;
            meter.value = Math.min(Math.max(0, current), total);
          }
        }
      }
      function addFiles(items) {
        if (state.phase === 'processing' || state.phase === 'saving') return;
        // Mirror Python's server-side budgets without reading file contents.
        // A rejected addition must not discard an existing valid selection.
        const nextFiles = new Map(state.files);
        let rejected = 0;
        let changed = false;
        for (const file of items) {
          if (!/\.xlsx$/i.test(file.name)) { rejected++; continue; }
          if (file.size > 20 * 1024 * 1024) {
            context.notify('Один Excel-файл не может превышать 20 МБ.');
            return;
          }
          const key = `${file.name}` + '\0' + `${file.size}` + '\0' + `${file.lastModified}`;
          if (!nextFiles.has(key)) {
            nextFiles.set(key, file);
            changed = true;
          }
        }
        if (rejected) context.notify('Можно выбрать только Excel-файлы .xlsx.');
        if (!changed) return;
        if (nextFiles.size > 100) {
          context.notify('Можно выбрать не более 100 Excel-файлов.');
          return;
        }
        if ([...nextFiles.values()].reduce((sum, file) => sum + file.size, 0) > 50 * 1024 * 1024) {
          context.notify('Общий размер Excel-файлов не может превышать 50 МБ.');
          return;
        }
        state.files = nextFiles;
        state.result = null;
        state.savedName = null;
        state.downloadStarted = false;
        state.phase = state.files.size ? 'selected' : 'empty';
        render();
      }
      input.addEventListener('change', () => { addFiles([...input.files]); input.value = ''; });
      async function startMerge() {
        if (state.files.size < 2 || state.phase === 'processing') return;
        const action = window.AllInOneBridge?.mergeExcel;
        if (typeof action !== 'function') {
          context.notify('Это дизайн-прототип. Обработку Excel подключает разработчик 00.');
          return;
        }
        state.phase = 'processing';
        state.error = null;
        render();
        displayProgress({phase: 'upload'});
        try {
          const result = await action([...state.files.values()], displayProgress);
          if (destroyed) return;
          if (result == null) throw new Error('Пустой результат обработки');
          state.result = result;
          state.phase = 'ready';
        } catch (error) {
          if (destroyed) return;
          state.error = error?.message || 'Не удалось объединить файлы. Проверьте таблицы и попробуйте ещё раз.';
          state.errorStage = 'merge';
          state.phase = 'error';
        }
        render();
      }
      async function saveFile() {
        if (!state.result) return;
        const action = window.AllInOneBridge?.saveExcel;
        if (typeof action !== 'function') {
          context.notify('Это дизайн-прототип. Сохранение подключает разработчик 00.');
          return;
        }
        state.phase = 'saving';
        render();
        try {
          const saved = await action(state.result);
          if (destroyed) return;
          if (saved == null || saved.cancelled === true) {
            state.phase = 'ready';
          } else {
            state.savedName = saved.name || 'Объединённый файл';
            state.downloadStarted = !!saved.downloadStarted;
            state.phase = 'saved';
          }
        } catch {
          if (destroyed) return;
          state.error = 'Не удалось сохранить файл. Выберите другое место и повторите попытку.';
          state.errorStage = 'save';
          state.phase = 'error';
        }
        render();
      }
      function reset() {
        if (state.phase === 'processing' || state.phase === 'saving') return;
        state.files.clear(); state.result = null; state.savedName = null;
        state.downloadStarted = false;
        state.phase = 'empty'; render();
      }
      function editFiles() {
        if (state.phase === 'processing' || state.phase === 'saving') return;
        state.error = null;
        state.result = null;
        state.phase = state.files.size ? 'selected' : 'empty';
        render();
      }
      // The displayed order is also the FormData/Excel row order.
      // Rebuild the insertion-ordered Map, never sort files by name.
      function reorderFile(sourceKey, targetKey, after = false) {
        if (state.phase !== 'selected' || sourceKey === targetKey) return;
        const entries = [...state.files.entries()];
        const previousKeys = entries.map(([key]) => key);
        const from = entries.findIndex(([key]) => key === sourceKey);
        const target = entries.findIndex(([key]) => key === targetKey);
        if (from < 0 || target < 0) return;
        const [moved] = entries.splice(from, 1);
        const destination = entries.findIndex(([key]) => key === targetKey) + (after ? 1 : 0);
        entries.splice(destination, 0, moved);
        if (entries.every(([key], i) => key === previousKeys[i])) return;
        state.files = new Map(entries);
        render();
        // Keep the moved row keyboard-accessible after the list is redrawn.
        const focused = [...root.querySelectorAll('.file-row')]
          .find(row => row._aioFileKey === sourceKey);
        focused?.focus({ preventScroll: true });
      }
      function shiftFile(key, direction) {
        const keys = [...state.files.keys()];
        const position = keys.indexOf(key);
        const targetKey = keys[position + direction];
        if (position < 0 || targetKey === undefined) return;
        reorderFile(key, targetKey, direction > 0);
      }
      function clearDragStyles() {
        root.querySelectorAll('.file-row').forEach(row =>
          row.classList.remove('dragging', 'drop-before', 'drop-after'));
      }
      const dropHandlers = {
        dragover(e) {
          if (draggedKey || state.phase === 'processing' || state.phase === 'saving') return;
          if (![...e.dataTransfer?.types || []].includes('Files')) return;
          e.preventDefault();
          e.currentTarget.classList.add('dragover');
        },
        dragleave(e) {
          if (!e.currentTarget.contains(e.relatedTarget))
            e.currentTarget.classList.remove('dragover');
        },
        drop(e) {
          if (draggedKey || state.phase === 'processing' || state.phase === 'saving') return;
          if (![...e.dataTransfer?.types || []].includes('Files')) return;
          e.preventDefault();
          e.currentTarget.classList.remove('dragover');
          if (e.dataTransfer?.files) addFiles([...e.dataTransfer.files]);
        }
      };
      function render() {
        root.replaceChildren(input);
        const head = elt('div', 'tool-heading');
        head.append(elt('h1', '', 'Объединение Excel-таблиц'), elt('p', '', 'Выберите несколько Excel-файлов и объедините их в один.'));
        root.appendChild(head);
        const panel = elt('section', 'work-panel');
        root.appendChild(panel);
        if (state.phase === 'empty' || state.phase === 'selected') {
          Object.entries(dropHandlers).forEach(([event, handler]) =>
            panel.addEventListener(event, handler));
        }
        if (state.phase === 'empty') {
          panel.classList.add('dropzone');
          const block = elt('div', 'empty-block');
          const icon = elt('div', 'empty-icon', 'X');
          icon.setAttribute('aria-hidden', 'true');
          block.append(icon, elt('h2', '', 'Выберите Excel-файлы'));
          block.append(elt('p', '', 'Или перетащите файлы сюда'));
          block.append(button('Выбрать файлы', 'btn primary large', openFiles));
          panel.appendChild(block);
          root.appendChild(elt('div', 'status-line', 'Файлы не выбраны'));
        } else if (state.phase === 'selected') {
          const toolbar = elt('div', 'files-heading');
          toolbar.append(elt('h2', '', `Выбрано файлов: ${state.files.size}`));
          toolbar.append(button('+ Добавить', 'btn quiet', openFiles));
          panel.appendChild(toolbar);
          panel.appendChild(elt('p', 'reorder-hint',
            'Можно перетащить сюда ещё файлы из Проводника. Строки объединяются в порядке списка; меняйте его перетаскиванием или стрелками.'));
          const list = elt('ul', 'file-list');
          for (const [index, [key, file]] of [...state.files.entries()].entries()) {
            const row = elt('li', 'file-row');
            row._aioFileKey = key;
            row.draggable = true;
            row.tabIndex = 0;
            row.setAttribute('aria-label',
              `Файл ${index + 1} из ${state.files.size}: ${file.name}. Alt+стрелка вверх или вниз меняет порядок.`);
            row.setAttribute('aria-keyshortcuts', 'Alt+ArrowUp Alt+ArrowDown');
            row.addEventListener('keydown', event => {
              if (event.target !== row || !event.altKey) return;
              if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
                event.preventDefault();
                shiftFile(key, event.key === 'ArrowUp' ? -1 : 1);
              }
            });
            row.addEventListener('dragstart', event => {
              draggedKey = key;
              if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = 'move';
                event.dataTransfer.setData('text/plain', 'aio-file-order');
              }
              row.classList.add('dragging');
            });
            row.addEventListener('dragover', event => {
              if (!draggedKey || draggedKey === key) return;
              event.preventDefault();
              if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
              const after = event.clientY > row.getBoundingClientRect().top + row.offsetHeight / 2;
              clearDragStyles();
              row.classList.add(after ? 'drop-after' : 'drop-before');
            });
            row.addEventListener('dragleave', event => {
              if (!row.contains(event.relatedTarget)) {
                row.classList.remove('drop-before', 'drop-after');
              }
            });
            row.addEventListener('drop', event => {
              if (!draggedKey) return;
              event.preventDefault();
              event.stopPropagation();
              const sourceKey = draggedKey;
              const after = event.clientY > row.getBoundingClientRect().top + row.offsetHeight / 2;
              draggedKey = null;
              clearDragStyles();
              reorderFile(sourceKey, key, after);
            });
            row.addEventListener('dragend', () => {
              draggedKey = null;
              clearDragStyles();
            });
            const grip = elt('span', 'file-grip', '⠿');
            grip.setAttribute('aria-hidden', 'true');
            const order = elt('span', 'file-order', String(index + 1));
            order.setAttribute('aria-hidden', 'true');
            const tag = elt('span', 'file-tag', 'X');
            tag.setAttribute('aria-hidden', 'true');
            const name = elt('span', 'file-name', file.name); name.title = file.name;
            const controls = elt('div', 'file-reorder-controls');
            const up = button('↑', 'move-file', () => shiftFile(key, -1), index === 0);
            up.setAttribute('aria-label', `Поднять ${file.name} выше`);
            const down = button('↓', 'move-file', () => shiftFile(key, 1), index === state.files.size - 1);
            down.setAttribute('aria-label', `Опустить ${file.name} ниже`);
            controls.append(up, down);
            const remove = button('×', 'remove-file', () => {
              state.files.delete(key);
              state.phase = state.files.size ? 'selected' : 'empty';
              render();
            });
            remove.setAttribute('aria-label', `Удалить файл ${file.name}`);
            row.append(grip, order, tag, name, controls, remove);
            list.append(row);
          }
          panel.appendChild(list);
          const actions = elt('div', 'actions');
          actions.append(button('Объединить', 'btn primary', startMerge, state.files.size < 2));
          panel.append(actions);
          if (state.files.size === 1) panel.appendChild(elt('p', 'hint', 'Для объединения нужен хотя бы ещё один файл.'));
        } else if (state.phase === 'processing' || state.phase === 'saving') {
          const isSaving = state.phase === 'saving';
          const block = elt('div', 'process-block');
          const circle = elt('div', 'spinner'); circle.setAttribute('aria-hidden', 'true');
          block.append(circle, elt('h2', '', isSaving ? 'Сохраняем файл…' : 'Объединяем файлы…'));
          block.append(elt('p', isSaving ? '' : 'merge-progress',
            isSaving ? 'Ожидаем подтверждения сохранения файла…' : 'Передаём файлы для обработки…'));
          if (!isSaving) {
            const meter = elt('progress', 'merge-progress-meter');
            meter.max = 100;
            meter.value = 0;
            meter.hidden = true;
            meter.setAttribute('aria-label', 'Запись строк в итоговый Excel');
            block.append(meter);
          }
          panel.append(block);
        } else if (state.phase === 'ready') {
          const block = elt('div', 'result-block');
          block.append(elt('div', 'result-icon success-icon', '✓'), elt('h2', '', 'Файл готов'));
          block.append(elt('p', '', 'Таблицы объединены. Теперь сохраните результат.'));
          block.append(button('Сохранить файл', 'btn primary large', saveFile));
          panel.appendChild(block);
        } else if (state.phase === 'saved') {
          const block = elt('div', 'result-block');
          block.append(elt('div', 'result-icon success-icon', '✓'), elt('h2', '',
            state.downloadStarted ? 'Скачивание начато' : 'Скачивание завершено'));
          block.append(elt('p', '', state.savedName || 'Объединённый файл'));
          block.append(button('Начать заново', 'btn secondary', reset));
          panel.appendChild(block);
        } else if (state.phase === 'error') {
          const block = elt('div', 'result-block');
          block.append(elt('div', 'result-icon error-icon', '!'), elt('h2', '', state.errorStage === 'save' ? 'Не удалось сохранить' : 'Ошибка объединения'));
          block.append(elt('p', '', state.error));
          const options = elt('div', 'error-actions');
          options.append(button('Изменить файлы', 'btn secondary', editFiles));
          options.append(button('Повторить', 'btn primary',
            state.errorStage === 'save' ? saveFile : startMerge));
          block.append(options);
          panel.append(block);
        }
      }
      render();
      return () => { destroyed = true; };
    }
  });
})();
