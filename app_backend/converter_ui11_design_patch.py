"""Minimal, fail-closed UI11 design hooks on SHA-verified original runtime JS.

Never rewrite the approved designer's compressed source, animations, glyphs,
theme icons, question-mark control or conversion protocol. Apply after
existing drag/drop and multi-output behavior patches.
"""
from __future__ import annotations


def apply_ui11_design(raw: bytes) -> bytes:
    source = raw.decode('utf-8')

    def change_once(old: str, new: str) -> None:
        nonlocal source
        occurrences = source.count(old)
        if occurrences != 1:
            raise ValueError(
                'UI11 owner design requires exact source anchor '
                + str(occurrences) + ': ' + old[:72])
        source = source.replace(old, new, 1)

    # Owner requested the original green ⇄ glyph, moon/sun and animated ?
    # left untouched. These are role-scoped classes, not replacements.
    change_once(
        '<section class="aio-cv__pane" aria-label="Исходные файлы">',
        '<section class="aio-cv__pane aio-cv__pane--left" aria-label="Исходные файлы">')
    change_once(
        '<section class="aio-cv__pane" aria-label="Параметры конвертации">',
        '<section class="aio-cv__pane aio-cv__pane--right" aria-label="Параметры конвертации">')

    change_once(
        '    function renderFileList() {',
        '    let typeFilter = null;\n'
        '    function renderFileList() {\n'
        '      typeFilter?.sync();')

    # The counter, max 100, converter payload, queue, sorting and selection
    # always use ALL files. Only visibility follows the selected type.
    change_once(
        "const filtered = files.filter(x=>x.file.name.toLocaleLowerCase('ru').includes(filters));",
        "const filtered = files.filter(x=>x.file.name.toLocaleLowerCase('ru').includes(filters) && "
        "(!typeFilter || typeFilter.matches(x)));")

    # Other is not presently exposed by the truthful DOCX output catalog.
    # Do not relabel unimplemented formats as working.
    change_once(
        'const cats=[...new Set(all.map(f=>f.category))];',
        "const cats=[...new Set(all.filter(f=>f.category!=='other').map(f=>f.category))];")

    change_once(
        "wrapper.setAttribute('aria-selected',String(selectedFormats.has(fmt.id)));",
        "wrapper.setAttribute('aria-selected',String(selectedFormats.has(fmt.id)));"
        "wrapper.dataset.available=String(fmt.enabled);")
    change_once(
        'wrapper.append(button,help);formatsArea.append(wrapper);',
        "if(!fmt.enabled){const availability=document.createElement('span');"
        "availability.className='aio-cv__availability';"
        "availability.textContent='Недоступно';"
        "wrapper.append(button,availability,help);}"
        "else wrapper.append(button,help);formatsArea.append(wrapper);")

    # Preserve real format settings; skip rendering the original inactive
    # "no additional options" heading/message rather than patching its text.
    # This is resilient to phrasing changes in the source language.
    change_once(
        "      const heading=document.createElement('h3');",
        "      if (!current.options?.length) return;\n"
        "      optionsArea.hidden=false;\n"
        "      const heading=document.createElement('h3');")

    # Filter module is preloaded in index.html. Mount/cleanup are confined
    # to this converter instance (no leaks into Excel or other tools).
    change_once(
        '    render();\n    return () => {',
        "    if(!window.AIOTypeFilter?.attach) throw new Error('UI11 file type filter module missing');\n"
        "    typeFilter=window.AIOTypeFilter.attach({root,getFiles:()=>files,"
        "onFilterChanged:renderFileList});\n"
        "    typeFilter.sync();\n"
        "    render();\n"
        "    return () => {")
    change_once(
        'alive=false;inspectionToken++;closePopover();',
        'alive=false;inspectionToken++;closePopover();typeFilter?.dispose();')

    return source.encode('utf-8')
