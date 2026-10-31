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


def apply_layout_controls(raw: bytes) -> bytes:
    """Keep existing handlers; synchronize lock state before entering native save."""
    code = raw.decode('utf-8')
    for old, new in ((TRASH_ANCHOR, TRASH_CONTROL), (SAVE_ENTRY_ANCHOR, SAVE_ENTRY_CONTROL)):
        if code.count(old) != 1:
            raise ValueError('Layout controls require review of anchor: ' + old[:70])
        code = code.replace(old, new, 1)
    return code.encode('utf-8')


LAYOUT_CSS = r'''
/* AIO_M01_M13_M15_LAYOUT_V1. Higher specificity than historical late sheets.
   Short windows scroll the workspace, never compress controls over each other.
   No JS resize/mutation observer, no preflight and no document operations. */
.app-shell .workspace .aio-cv {
  height: auto;
  min-height: 100%;
}
.app-shell .workspace .aio-cv__columns {
  display: grid;
  flex: 0 0 auto;
  grid-template-columns: minmax(0, .92fr) minmax(0, 1.08fr);
  grid-auto-rows: auto;
  align-items: stretch;
  min-width: 0;
}
.app-shell .workspace .aio-cv__pane {
  box-sizing: border-box;
  flex: 0 0 auto;
  height: clamp(640px, calc(100vh - 220px), 820px) !important;
  min-height: 640px !important;
  max-height: none !important;
  min-width: 0;
}
.app-shell .workspace .aio-cv__pane--left .aio-cv__pane-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto 36px;
  flex: 0 0 auto;
  height: auto;
  min-height: 44px;
  align-items: center;
  gap: 6px;
}
.app-shell .workspace .aio-cv-filter__actions {
  display: flex;
  flex-wrap: wrap;
  flex: 0 1 auto;
  min-width: 0;
  width: auto;
  gap: 6px;
}
.app-shell .workspace .aio-cv-filter__trigger {
  flex: 0 1 116px;
  min-width: 84px;
  max-width: 116px;
}
.app-shell .workspace [data-cv-add] {
  flex: 1 1 112px;
  min-width: 0;
  min-height: 34px;
  height: auto;
  white-space: normal;
  overflow-wrap: anywhere;
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
  white-space: normal;
  overflow-wrap: anywhere;
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
  grid-template-rows: 64px 56px 32px 48px 44px;
  gap: 8px;
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
  height: 64px;
  min-height: 0;
  max-height: 64px;
  min-width: 0;
  margin: 0;
  overflow: auto;
  overflow-wrap: anywhere;
}
.app-shell .workspace .aio-cv .aio-cv__footer [data-cv-status] {
  grid-row: 2;
  box-sizing: border-box;
  height: 56px;
  min-height: 56px;
  max-height: 56px;
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
/* On a very short stacked shell the fixed navigation can consume almost
   the whole viewport. Let the document scroll only in active converter mode;
   changing tools restores the original shell without a resize listener. */
@media (max-width: 610px) and (max-height: 540px) {
  .app-shell:has(.workspace.aio-qa-converter-mode) {
    height: auto !important;
    min-height: 100vh !important;
    overflow: visible;
  }
  .app-shell:has(.workspace.aio-qa-converter-mode) .app-layout {
    flex: 0 0 auto;
    overflow: visible;
  }
  .app-shell .app-layout .workspace.aio-qa-converter-mode {
    height: auto;
    overflow-y: visible !important;
    overflow-x: clip !important;
  }
}
/* M13: the SVG inherits currentColor; colour emoji ignored CSS colour. */
.app-shell .workspace .aio-cv__clear-inputs {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  width: 36px;
  min-width: 36px;
  height: 36px;
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
@container aio-inputs (max-width: 460px) {
  .app-shell .workspace .aio-cv__pane--left .aio-cv__pane-head {
    grid-template-columns: minmax(0, 1fr) 36px;
    grid-template-rows: auto auto;
    height: auto;
    flex-basis: auto;
    min-height: 72px;
  }
  .app-shell .workspace .aio-cv__pane--left .aio-cv__pane-title { grid-column: 1 / -1; }
  .app-shell .workspace .aio-cv__pane--left .aio-cv-filter__actions { grid-column: 1; grid-row: 2; width: auto; }
  .app-shell .workspace .aio-cv__pane--left .aio-cv__clear-inputs { grid-column: 2; grid-row: 2; align-self: start; }
}
@container (max-width: 740px) {
  .app-shell .workspace .aio-cv__columns { display: grid; grid-template-columns: minmax(0, 1fr); }
}
'''
