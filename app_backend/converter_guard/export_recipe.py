"""Small native export settings, shared by CLI and UNO.

This never admits or rejects a source/target pair. A current native proof and
an unambiguous installed filter are required before overriding CLI detection.
The requested text dialect is independent from the source import dialect.
"""
from dataclasses import dataclass

CALC = 'com.sun.star.sheet.SpreadsheetDocument'
DRAW = 'com.sun.star.drawing.DrawingDocument'
WRITER = 'com.sun.star.text.TextDocument'
CALC_TEXT = 'Text - txt - csv (StarCalc)'
TEXT_OPTIONS = {'csv': '44,34,76,1,', 'tsv': '9,34,76,1,', 'tab': '9,34,76,1,'}
WARNING_TEXT = {
    'DBF_UNDECLARED_UTF8_IMPORT': (
        'В DBF не указана кодировка. Все имена и текстовые поля проверены как UTF-8; '
        'A.I.O использует UTF-8 при импорте. Кодировка выбрана по содержимому, '
        'её исходное объявление отсутствует.'),
    'WORDML_PAGE_REGIONS_COMPANION': (
        'Колонтитулы Word XML сохранены в отдельном HTML с видимой ссылкой из основного результата. '
        'Текст, ссылки и встроенные ресурсы сохранены; повторение на страницах и исходная геометрия '
        'колонтитулов заменены непрерывными разделами.'),
    'NATIVE_DIF_UTF8_IMPORT': (
        'DIF сохранён в UTF-8 с отдельным описанием кодировки. При открытии в другой программе '
        'выберите UTF-8: кодировка ANSI по умолчанию может исказить кириллицу и другие символы. '
        'A.I.O передаёт LibreOffice явную настройку UTF-8 при импорте DIF.'),
    'NATIVE_VECTOR_PAGE_VIEW': (
        'Все страницы сохранены штатным SVG-экспортёром LibreOffice и включены в HTML/XHTML. '
        'Векторные рисунки и встроенные изображения сохранены; анимация, интерактивность '
        'и редактируемая структура презентации в это представление не переносятся.'),
    'NATIVE_WRITER_XHTML_VIEW': (
        'HTML сохранён штатным XHTML-фильтром Writer с включёнными изображениями. '
        'Для исходного Word 2003 XML этот маршрут сохраняет изображения, '
        'которые старый HTML-фильтр может пропустить. Вёрстка требует отдельной проверки.'),
    'QT_GIF_FORMAT_LIMITS': (
        'GIF ограничен палитрой до 256 цветов и прозрачностью без полутонов. '
        'Длительности кадров округляются до 10 мс; при необходимости палитра '
        'подбирается с сохранением всех кадров. Эти ограничения относятся к формату GIF.'),
    'NATIVE_DBF_MIXED_COLUMN_TEXT': (
        'В DBF столбцы со смешанными числами и текстом сохранены как Memo, чтобы не потерять текст. '
        'Числа в этих столбцах представлены отображаемыми строками; исходные значения, типы '
        'и заголовки сохранены в сопровождающем файле .aio-columns.jsonl. '
        'Остальные числовые столбцы сохраняют свой тип.'),
    'ODF_IMPLICIT_COLUMNS_EXPLICIT': (
        'Перед импортом во временной копии ODF явно указаны столбцы прямоугольной таблицы. '
        'Тексты ячеек и исходный файл сохранены без изменений.'),
    'EPUB_REFLOWED_HTML_IMPORT': (
        'EPUB импортирован через встроенный HTML-путь LibreOffice в порядке глав OPF. '
        'Изображения включены в документ; вёрстка при смене формата может измениться.'),
    'QT_PDF_RASTER_300_DPI': (
        'Все страницы PDF преобразованы в TIFF штатным Qt с разрешением 300 dpi. '
        'Векторный текст и рисунки стали растровыми изображениями страниц.'),
    'NATIVE_DRAW_SVM_PAGES': (
        'SVM сохранён нативным графическим экспортёром LibreOffice: '
        'каждая страница рисунка находится в отдельном файле. '
        'Это метафайл для просмотра, редактируемая структура Draw не сохраняется.'),
    'NATIVE_OUTPUT_NOT_CHECKED': (
        'Файл создан, но одна из дополнительных проверок результата не завершена. '
        'Отчёт содержит её причину; полная точность содержимого не подтверждена.'),
    'ODF_TABLE_CONTENT_NOT_CHECKED': (
        'Не удалось независимо сверить весь текст таблиц ODF с этим форматом результата. '
        'Сохранность всех ячеек не подтверждена; причина указана в отчёте.'),
    'NATIVE_XML_ODF_DIALECT': (
        'XML сохранён как Flat OpenDocument XML штатным фильтром LibreOffice. '
        'Это XML-представление документа OpenDocument, а не старый формат Excel/Word 2003 XML.'),
    'NATIVE_HTML_CONTENT_NOT_CHECKED': (
        'Файл создан, но ограниченная проверка содержимого HTML не завершена. '
        'Наличие всего содержимого и точность внешнего вида не подтверждены.'),
    'NATIVE_DRAW_XHTML_VIEW': (
        'Рисунок сохранён штатным XHTML-фильтром LibreOffice. Анимация, '
        'редактируемость и точность отображения отдельно не сравнивались.'),
    'NATIVE_WRITER_TEXT_ONLY': (
        'Writer сохраняет TXT/CSV/TAB/TSV как обычный текст UTF-8. Это не таблица Calc: '
        'оформление, изображения и табличная структура не сохраняются.'),
    'NATIVE_TABLE_FORMAT_LIMITS': (
        'DBF/DIF имеют ограничения структуры, типов и оформления. '
        'Наличие нативного маршрута не подтверждает сохранность всех данных. '
        'В пакетном UNO-режиме каждый лист сохраняется отдельным файлом; '
        'формулы становятся вычисленными значениями, оформление ограничено форматом.'),
    'NATIVE_CLOSE_INTERRUPTED': (
        'Нативный экспорт завершён, но закрытие модели зависло. Собственный '
        'процесс LibreOffice завершён; выходные файлы проверены на наличие.'),
    'NATIVE_DRAW_HTML_CONTENT_RISK': (
        'Штатный HTML-фильтр Draw может создавать файл без содержимого рисунка. '
        'Для нескольких страниц расположение в XHTML тоже требует проверки. '
        'Сохраните исходник; для просмотра предпочтителен PDF.'),
    'NATIVE_WRITER_SVG_EXPORT_RISK': (
        'Штатный SVG-фильтр Writer может завершиться без выходного файла. '
        'Поддержка зарегистрированного фильтра не гарантирует успешный экспорт. '
        'Guard проверяет наличие результата, а при отказе сохраняет диагностику LibreOffice.')}


def warning_details(codes):
    return [{'code': code, 'message': WARNING_TEXT[code]} for code in codes if code in WARNING_TEXT]


@dataclass(frozen=True)
class ExportRecipe:
    filter_name: str
    filter_options: str | None = None
    required_draw_pages: int | None = None
    warnings: tuple = ()

    def cli_spec(self, target):
        return target + ':' + self.filter_name + (
            ':' + self.filter_options if self.filter_options is not None else '')


def native_export_recipe(native, target, proof):
    runtime_filter = native.runtime_export_filter(target, proof)
    if runtime_filter:
        return ExportRecipe(runtime_filter, 'UTF8', warnings=('NATIVE_WRITER_TEXT_ONLY',))
    if (not native.valid or not proof or
            proof.get('registryFingerprint') != native.fingerprint or
            native.observed_decision(target, proof) != 'registered'):
        return None
    services = native.export_services(proof)
    if not isinstance(services, list) or not services or any(not isinstance(s, str) for s in services):
        return None
    if (target == 'svm' and set(services).intersection((DRAW, 'com.sun.star.presentation.PresentationDocument'))
            and proof.get('basis') == 'native_preflight_uno_document'
            and 'svm' in proof.get('nativeGraphicExports', ())):
        # This is a capability observed on the running GraphicExportFilter,
        # not the import-only SVM document filter or a CLI format guess.
        return ExportRecipe('svm_Export', warnings=('NATIVE_DRAW_SVM_PAGES',))
    selected = native.export_filter(target, services)
    if proof.get('basis') == 'native_preflight_uno_document':
        observed = proof.get('selectedImportFilter')
        item = native.filters.get(observed)
        if (item and item[0] in services and 'EXPORT' in item[2]
                and native.primary_extensions.get(observed) == target
                and target not in native.uncertain_extensions):
            selected = observed
    if (target in ('html', 'htm', 'xhtml') and WRITER in services
            and (proof.get('selectedImportFilter') == 'MS Word 2003 XML'
                 or proof.get('sourceContract', {}).get('contentClass') == 'word2003_xml')
            and 'xhtml' not in native.uncertain_extensions):
        alternative = native.filters.get('XHTML Writer File')
        if (alternative and alternative[0] == WRITER and 'EXPORT' in alternative[2]
                and 'xhtml' in alternative[1]):
            return ExportRecipe('XHTML Writer File', warnings=('NATIVE_WRITER_XHTML_VIEW',))
    # A generic XML target has no selected legacy application dialect. Keep
    # the model in its own native flat-ODF representation instead of choosing
    # Excel/Word 2003 XSLT filters merely because their first suffix is "xml".
    # The installed filter must explicitly export XML for this observed model.
    if target == 'xml':
        choices = {WRITER: 'OpenDocument Text Flat XML', CALC: 'OpenDocument Spreadsheet Flat XML',
                   DRAW: 'OpenDocument Drawing Flat XML',
                   'com.sun.star.presentation.PresentationDocument': 'OpenDocument Presentation Flat XML'}
        candidates = [name for service, name in choices.items() if service in services
                      and (item := native.filters.get(name)) and item[0] == service
                      and 'EXPORT' in item[2] and 'xml' in item[1]]
        if len(candidates) == 1:
            return ExportRecipe(candidates[0], warnings=('NATIVE_XML_ODF_DIALECT',))
    # draw_html_Export can emit an empty HTML shell. The installed XHTML
    # exporter preserves a single Draw page and is valid HTML syntax as well.
    # Do not apply this to multi-page drawings: native absolute positioning
    # can overlap pages. No custom renderer or suffix-based input admission.
    if (target in ('html', 'htm') and services == [DRAW]
            and type(proof.get('drawPages')) is int and proof['drawPages'] == 1
            and 'xhtml' not in native.uncertain_extensions):
        alternative = native.filters.get('XHTML Draw File')
        candidates = native.export_candidates.get(('xhtml', DRAW), ())
        if (alternative and alternative[0] == DRAW and 'EXPORT' in alternative[2]
                and 'XHTML Draw File' in candidates and all(n in native.filters for n in candidates)):
            return ExportRecipe('XHTML Draw File', required_draw_pages=1,
                                warnings=('NATIVE_DRAW_XHTML_VIEW',))
    if target in ('csv', 'tab', 'tsv', 'txt') and WRITER in services:
        item = native.filters.get('Text (encoded)')
        if item and item[0] == WRITER and 'EXPORT' in item[2]:
            return ExportRecipe('Text (encoded)', 'UTF8', warnings=('NATIVE_WRITER_TEXT_ONLY',))
    if (target in ('png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif', 'tif', 'tiff')
            and WRITER in services):
        alternative = native.export_filter(target, [WRITER])
        if alternative:
            selected = alternative
    if not selected:
        return None
    item = native.filters.get(selected)
    # Calc's CSV filter implements these native FilterOptions. A Writer text
    # filter or third-party filter with similar extensions does not inherit it.
    options = (TEXT_OPTIONS.get(target) if selected == CALC_TEXT and item and
               item[0] == CALC and target in item[1] and 'EXPORT' in item[2] else None)
    if selected in ('DIF', 'dBase') and item and item[0] == CALC and 'EXPORT' in item[2]:
        return ExportRecipe(selected, '76', warnings=('NATIVE_TABLE_FORMAT_LIMITS',))
    warnings = (('NATIVE_DRAW_HTML_CONTENT_RISK',)
                if selected == 'draw_html_Export' and target in ('html', 'htm') else
                ('NATIVE_WRITER_SVG_EXPORT_RISK',)
                if selected == 'writer_svg_Export' and target == 'svg' else ())
    return ExportRecipe(selected, options, warnings=warnings)

