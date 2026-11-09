"""M01/M13/M15: bounded converter layout; no conversion or storage policy."""
from __future__ import annotations

TRASH_ANCHOR = 'aria-label="Очистить список исходников">🗑</button>'
TRASH_CONTROL = ('aria-label="Очистить список исходников">'
    '<svg class="aio-cv__trash-icon" viewBox="0 0 24 24" '
    'aria-hidden="true" focusable="false">'
    '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7"/>'
    '</svg></button>')


SAVE_ENTRY_ANCHOR = 'saving=true;renderFileList();saveBtn.disabled=true;'
SAVE_ENTRY_CONTROL = 'saving=true;renderFileList();renderSummary();saveBtn.disabled=true;'
ADD_BUTTON_ANCHOR = 'data-cv-add>+ Добавить файлы</button>'
ADD_BUTTON_CONTROL = ('data-cv-add aria-label="Добавить файлы" title="Добавить файлы">'
    '<svg class="aio-cv__add-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    '<path d="M12 5v14M5 12h14"/></svg><span class="aio-cv__add-label">Добавить файлы</span></button>')
ADD_VISIBILITY_ANCHOR = "root.querySelector('[data-cv-add]').hidden = files.length === 0;"
ADD_VISIBILITY_CONTROL = "root.querySelector('[data-cv-add]').hidden = false;"


def apply_layout_controls(raw: bytes) -> bytes:
    """Keep existing handlers; synchronize lock state before entering native save."""
    code = raw.decode('utf-8')
    for old, new in ((TRASH_ANCHOR, TRASH_CONTROL), (SAVE_ENTRY_ANCHOR, SAVE_ENTRY_CONTROL),
                     (ADD_BUTTON_ANCHOR, ADD_BUTTON_CONTROL), (ADD_VISIBILITY_ANCHOR, ADD_VISIBILITY_CONTROL)):
        if code.count(old) != 1:
            raise ValueError('Layout controls require review of anchor: ' + old[:70])
        code = code.replace(old, new, 1)
    return code.encode('utf-8')


LAYOUT_CSS = r'''
/* AIO_M01_M13_M15_LAYOUT_V3. Higher specificity than historical late sheets.
   Lists use the available desktop height. Short/stacked windows scroll only
   as far as needed for readable controls; file count never grows the page.
   No JS resize/mutation observer, no preflight and no document operations. */
.app-shell .workspace.aio-qa-converter-mode {
  padding: 18px 24px !important;
  height: auto;
}
.app-shell .workspace > .aio-mounted-tool:not([hidden]):has(.aio-cv) {
  height: 100%;
  min-height: 0;
}
.app-shell .workspace .aio-cv {
  height: 100%;
  min-height: 0;
}
.app-shell .workspace .aio-cv__columns {
  display: grid;
  flex: 1 0 500px;
  grid-template-columns: minmax(0, .92fr) minmax(0, 1.08fr);
  grid-auto-rows: minmax(0, 1fr);
  min-height: 500px;
  align-items: stretch;
  min-width: 0;
}
.app-shell .workspace .aio-cv__pane {
  box-sizing: border-box;
  flex: 0 0 auto;
  height: 100% !important;
  min-height: 500px !important;
  max-height: none !important;
  min-width: 0;
}
.app-shell .workspace .aio-cv__pane--left .aio-cv__pane-head {
  display: grid;
  grid-template-columns: 11ch minmax(0, 1fr) 36px;
  grid-template-rows: 44px;
  flex: 0 0 44px;
  height: 44px;
  min-height: 44px;
  max-height: 44px;
  flex-wrap: nowrap;
  align-items: center;
  gap: 6px;
  /* ch must use the counter's font, not the smaller shell/body font. */
  font-family: var(--font);
  font-size: 17px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.app-shell .workspace .aio-cv__pane--left .aio-cv__pane-head .aio-cv__pane-title {
  grid-column: 1;
  grid-row: 1;
  display: flex;
  align-items: center;
  width: auto;
  height: 38px;
  min-width: 0;
  flex: 0 0 auto;
  font: inherit;
  font-variant-numeric: inherit;
}
.app-shell .workspace .aio-cv__pane--left .aio-cv-filter__actions {
  grid-column: 2;
  grid-row: 1;
  display: flex;
  flex-wrap: nowrap !important;
  flex: 0 1 auto;
  min-width: 0;
  width: 100%;
  justify-content: flex-end;
  align-items: center;
  margin-left: 0;
  gap: 6px;
}
.app-shell .workspace .aio-cv__pane--left .aio-cv-filter__trigger {
  flex: 0 0 104px;
  width: 104px;
  min-width: 104px;
  max-width: 104px;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.2;
  padding-inline: 8px;
  height: 38px;
  min-height: 38px;
}
.app-shell .workspace [data-filter-label] {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.app-shell .workspace [data-cv-add] {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  flex: 0 1 156px;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.2;
  min-width: 0;
  min-height: 38px;
  height: 38px;
  white-space: nowrap;
  padding-inline: 8px;
}
.app-shell .workspace .aio-cv__add-icon {
  display: block;
  flex: 0 0 16px;
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
}
.app-shell .workspace .aio-cv__add-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.app-shell .workspace .aio-cv__pane--left .aio-cv__search {
  flex: 0 0 40px;
  height: 40px;
  min-height: 40px;
  margin-top: 0;
}
.app-shell .workspace [data-cv-file-area] {
  flex: 1 1 0;
  min-height: 96px;
  max-height: none;
  overflow: auto;
}
.app-shell .workspace .aio-cv__format-head {
  flex: 0 0 auto;
  height: auto;
  min-height: 44px;
  flex-wrap: wrap;
}
.app-shell .workspace .aio-cv__format-head .aio-cv__pane-title {
  min-width: 100px;
}
.app-shell .workspace .aio-cv__categories {
  display: flex;
  flex-wrap: wrap;
  height: auto;
  min-width: 0;
  flex: 0 0 auto;
  gap: 6px;
}
.app-shell .workspace .aio-cv__categories > button {
  flex: 1 1 96px;
  min-width: 0;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.app-shell .workspace .aio-cv__pane--right .aio-cv__formats {
  flex: 1 1 0;
  min-height: 96px !important;
  max-height: none;
  min-width: 0;
  overflow: auto;
}
/* Real buttons stay in their original DOM order; hidden slots are inert.
   visibility:hidden retains geometry but cannot receive pointer/keyboard focus. */
.app-shell .workspace .aio-cv .aio-cv__footer {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: 40px 44px 32px 48px 44px;
  gap: 6px;
  min-width: 0;
  flex: 0 0 auto !important;
  height: auto !important;
  min-height: 0 !important;
  max-height: none !important;
  margin-top: auto !important;
  align-content: normal;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-summary] {
  grid-row: 1;
  box-sizing: border-box;
  height: 40px;
  min-height: 0;
  max-height: 40px;
  min-width: 0;
  margin: 0;
  overflow: auto;
  overflow-wrap: anywhere;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-status] {
  grid-row: 2;
  box-sizing: border-box;
  height: 44px;
  min-height: 44px;
  max-height: 44px;
  min-width: 0;
  margin: 0;
  overflow: auto;
  overflow-wrap: anywhere;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-run] {
  grid-row: 4;
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  height: 48px;
  margin: 0;
  white-space: normal;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-run][hidden] {
  display: block !important;
  visibility: hidden;
  pointer-events: none;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-result-actions] {
  grid-row: 5;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 8px;
  height: 44px;
  min-width: 0;
  margin: 0;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-result-actions][hidden] {
  display: grid !important;
  visibility: hidden;
  pointer-events: none;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-result-actions] > button {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  height: 44px !important;
  min-height: 44px !important;
  max-height: 44px !important;
  grid-row: 1;
  padding: 2px 6px;
  line-height: 1.2;
  white-space: normal;
  overflow-wrap: anywhere;
}
/* The queue keeps its own reserved row; old queued-state rules must not
   overlay the run/save rows or clip the summary with a 176px footer. */
.app-shell .workspace .aio-cv .aio-cv__footer .aio-guard-queue {
  grid-row: 3;
  height: 32px;
  min-height: 32px;
  min-width: 0;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-summary],
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-status] {
  scrollbar-width: auto;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-summary]::-webkit-scrollbar,
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-status]::-webkit-scrollbar {
  display: block;
}
/* The document never becomes a second scroll owner. Short logical viewports
   keep wheel/keyboard access through the bounded workspace and list viewports. */
html:has(.workspace.aio-qa-converter-mode),
body:has(.workspace.aio-qa-converter-mode) {
  height: 100%;
  overflow: hidden;
}
body:has(.workspace.aio-qa-converter-mode) {
  position: fixed;
  inset: 0;
  width: 100%;
  /* hidden still permits focus/scrollIntoView to scroll the fixed body.
     clip leaves scrolling to the reachable workspace instead. */
  overflow: clip;
}
/* M13: the SVG inherits currentColor; colour emoji ignored CSS colour. */
.app-shell .workspace .aio-cv__clear-inputs {
  grid-column: 3;
  grid-row: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  width: 36px;
  min-width: 36px;
  height: 38px;
  min-height: 38px;
  align-self: center;
  padding: 6px;
  color: var(--danger);
}
.app-shell .workspace .aio-cv__clear-inputs:disabled {
  color: var(--muted);
  opacity: 1;
  cursor: not-allowed;
}
.app-shell .workspace .aio-cv__trash-icon {
  display: block;
  width: 22px;
  height: 22px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
  pointer-events: none;
}
@container aio-inputs (max-width: 420px) {
  .app-shell .workspace .aio-cv__pane--left .aio-cv-filter__trigger {
    flex: 0 0 36px;
    width: 36px;
    min-width: 36px;
    max-width: 36px;
    padding-inline: 8px;
  }
  .app-shell .workspace .aio-cv__pane--left [data-filter-label] { display: none; }
}
@container aio-inputs (max-width: 350px) {
  .app-shell .workspace .aio-cv__pane--left [data-cv-add] {
    flex: 0 0 36px;
    width: 36px;
    min-width: 36px;
    padding-inline: 8px;
  }
  .app-shell .workspace .aio-cv__pane--left .aio-cv__add-label { display: none; }
}
@container (max-width: 740px) {
  .app-shell .workspace .aio-cv__columns { display: grid; grid-template-columns: minmax(0, 1fr); grid-auto-rows: auto; flex: 0 0 auto; min-height: 0; }
  .app-shell .workspace .aio-cv__pane--left { height: clamp(300px, 42vh, 400px) !important; min-height: 300px !important; }
  .app-shell .workspace .aio-cv__pane--right { height: 600px !important; min-height: 600px !important; }
}
@media (max-width: 610px) {
  .app-shell:has(.workspace.aio-qa-converter-mode) .app-layout { min-height: 0; }
  .app-shell:has(.workspace.aio-qa-converter-mode) .sidebar {
    flex: 0 0 auto;
    padding: 8px 12px;
    grid-template-rows: 36px 36px;
    gap: 6px;
  }
  .app-shell:has(.workspace.aio-qa-converter-mode) .search-wrap { height: 36px; }
  .app-shell:has(.workspace.aio-qa-converter-mode) .tool-nav { flex-direction: row; gap: 6px; }
  .app-shell:has(.workspace.aio-qa-converter-mode) .nav-item { flex: 1; width: auto; min-height: 36px; padding: 5px; gap: 6px; }
  .app-shell:has(.workspace.aio-qa-converter-mode) .tool-glyph { width: 24px; height: 24px; font-size: 16px; }
  .app-shell:has(.workspace.aio-qa-converter-mode) .tool-name { font-size: 12px; }
  .app-shell .app-layout .workspace.aio-qa-converter-mode {
    flex: 1 1 0;
    height: auto;
    min-height: 0;
    overflow-y: auto !important;
  }
}
'''
