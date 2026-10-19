// Расширяемая регистрация РЕАЛЬНО реализованных инструментов.
// Новые инструменты добавляются по решению владельца и только после готовности.
(() => {
  'use strict';
  const tools = new Map();
  window.AIO = {
    registerTool(tool) {
      if (!tool || typeof tool.id !== 'string' || !tool.id.trim() ||
          typeof tool.title !== 'string' || !tool.title.trim() ||
          typeof tool.mount !== 'function') {
        throw new Error('Инструмент должен иметь id, title и mount(root, context).');
      }
      if (tools.has(tool.id)) throw new Error('Повторная регистрация: ' + tool.id);
      tools.set(tool.id, tool);
    },
    getTool(id) { return tools.get(id); },
    getTools() { return [...tools.values()]; }
  };
})();
