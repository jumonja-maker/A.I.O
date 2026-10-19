// Оболочка: поиск, переключение подключённых инструментов и проверка обновлений.
// AllInOneBridge определяет разработчик 00 в Windows-хосте.
(() => {
  'use strict';
  const nav = document.getElementById('tool-nav');
  const search = document.getElementById('tool-search');
  const root = document.getElementById('tool-root');
  const noTools = document.getElementById('no-tools');
  const toast = document.getElementById('toast');
  const update = document.getElementById('check-updates');
  const themeButton = document.getElementById('theme-toggle');
  // Не оставляем настройки в профиле внешнего браузера. До внедрения
  // контролируемого локального хранилища тема сохраняется только в сеансе.
  const THEME_KEY = 'aio-theme-v1';
  function readTheme() {
    try {
      const stored = window.localStorage.getItem(THEME_KEY);
      if (stored === 'dark' || stored === 'light') return stored;
    } catch (_) { /* Ephemeral Qt WebEngine profiles may disable persistence. */ }
    return 'light';
  }
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    themeButton.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
    themeButton.setAttribute('aria-label', theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему');
    themeButton.title = theme === 'dark' ? 'Светлая тема' : 'Тёмная тема';
    window.AIOBranding?.setTheme(theme);
  }
  applyTheme(readTheme());
  themeButton.addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try { window.localStorage.setItem(THEME_KEY, next); } catch (_) { /* UI still works. */ }
  });
  let activeId = null;
  let toastTimeout = null;
  // Keep mounted tool instances alive: expensive conversions and unsaved
  // Blob results belong to their tool, not to the currently selected tab.
  const mountedTools = new Map();
  function notify(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => { toast.hidden = true; }, 5000);
  }
  function renderMenu() {
    const value = search.value.trim().toLocaleLowerCase('ru');
    const tools = window.AIO.getTools().filter(t => t.title.toLocaleLowerCase('ru').includes(value));
    nav.replaceChildren();
    noTools.hidden = tools.length !== 0;
    for (const tool of tools) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'nav-item' + (activeId === tool.id ? ' active' : '');
      button.setAttribute('aria-current', activeId === tool.id ? 'page' : 'false');
      const badge = document.createElement('span');
      badge.className = 'tool-glyph';
      badge.setAttribute('aria-hidden', 'true');
      badge.textContent = tool.iconText || '•';
      const name = document.createElement('span');
      name.className = 'tool-name';
      name.textContent = tool.title;
      button.append(badge, name);
      button.addEventListener('click', () => activate(tool.id));
      nav.appendChild(button);
    }
  }
  function activate(id) {
    const tool = window.AIO.getTool(id);
    if (!tool) return;
    let instance = mountedTools.get(id);
    if (!instance) {
      const view = document.createElement('section');
      view.className = 'aio-mounted-tool';
      view.dataset.toolId = id;
      view.hidden = true;
      root.append(view);
      try {
        const cleanup = tool.mount(view, { notify }) || null;
        instance = { view, cleanup };
        mountedTools.set(id, instance);
      } catch (error) {
        // Keep the previously mounted tool usable if an optional tool fails.
        // A failed mount must not crash the global navigation click handler.
        view.remove();
        console.error('AIO tool mount failed:', id, error);
        notify('Не удалось открыть инструмент. Остальные инструменты доступны.');
        return;
      }
    }
    for (const [key, value] of mountedTools) {
      value.view.hidden = key !== id;
    }
    activeId = id;
    root.dataset.activeTool = id;
    renderMenu();
    root.scrollTop = 0;
  }
  search.addEventListener('input', renderMenu);
  update.addEventListener('click', async () => {
    const fn = window.AllInOneBridge?.checkForUpdates;
    if (typeof fn !== 'function') {
      notify('В прототипе проверка обновлений не подключена. Реализует 00.');
      return;
    }
    update.disabled = true;
    try {
      const outcome = await fn();
      notify(outcome?.message || 'Проверка обновлений завершена.');
      if (outcome?.available && outcome.can_install
          && typeof window.AllInOneBridge?.installUpdate === 'function') {
        if (window.confirm(outcome.message + '\n\nЗагрузить и установить обновление?')) {
          notify('Скачиваем и проверяем обновление. Не закрывайте программу.');
          const launched = await window.AllInOneBridge.installUpdate();
          notify(launched?.message || 'Установщик обновления запущен.');
        }
      } else if (outcome?.available && outcome.release_url) {
        if (window.confirm(outcome.message + '\n\nОткрыть страницу релиза?')) {
          window.open(outcome.release_url, '_blank', 'noopener,noreferrer');
        }
      }
    } catch (error) {
      notify(error?.message || 'Не удалось проверить обновления. Попробуйте позже.');
    } finally { update.disabled = false; }
  });
  renderMenu();
  const first = window.AIO.getTools()[0];
  if (first) activate(first.id);
})();
