"""Small native export settings, shared by CLI and UNO.

This never admits or rejects a source/target pair. A current native proof and
an unambiguous installed filter are required before overriding CLI detection.
The requested text dialect is independent from the source import dialect.
"""
from dataclasses import dataclass
import copy
import json

from .errors import GuardError

CALC = 'com.sun.star.sheet.SpreadsheetDocument'
DRAW = 'com.sun.star.drawing.DrawingDocument'
WRITER = 'com.sun.star.text.TextDocument'
CALC_TEXT = 'Text - txt - csv (StarCalc)'
TEXT_OPTIONS = {'csv': '44,34,76,1,', 'tsv': '9,34,76,1,', 'tab': '9,34,76,1,'}
WARNING_TEXT = {
    'AUTOMATIC_RETRY_MAY_NOT_IMPROVE': (
        'Применён проверенный набор параметров экспорта. Он не гарантирует улучшение; '
        'результат повторно проверяется, предыдущая версия остаётся в AIO.'),
    'AUTOMATIC_CSV_RAW_VALUES': (
        'CSV повторно экспортирован в UTF-8 с исходной числовой точностью и отдельным файлом '
        'для каждого листа. Отображение дат, валют и округлённых чисел может измениться. '
        'Формулы представлены вычисленными значениями; оформление CSV не поддерживает.'),
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
    'QT_PDF_RASTER_144_DPI': (
        'Все страницы PDF преобразованы в TIFF штатным Qt с разрешением 144 dpi. '
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
    filter_data: tuple = ()

    def cli_spec(self, target):
        if self.filter_data:
            if self.filter_options is not None:
                raise ValueError('FilterOptions and typed FilterData cannot share a CLI slot')
            properties = {name: {'type': 'boolean', 'value': value}
                          for name, value in self.filter_data}
            return target + ':' + self.filter_name + ':' + json.dumps(properties, separators=(',', ':'))
        return target + ':' + self.filter_name + (
            ':' + self.filter_options if self.filter_options is not None else '')


_AUTOMATIC_RECIPES = {
    'pdf_preserve_images': {
        'id': 'pdf_preserve_images', 'targets': ['pdf'],
        'label': 'PDF: сохранить изображения без дополнительного сжатия',
        'parameters': {'UseLosslessCompression': True, 'ReduceImageResolution': False},
        'limitations': ['Не восстанавливает утраченное до экспорта качество.',
                        'Не исправляет отсутствующие шрифты или ошибки импорта.'],
    },
    'csv_preserve_values': {
        'id': 'csv_preserve_values', 'targets': ['csv', 'tsv', 'tab'],
        'label': 'CSV: полная точность значений и все листы',
        'parameters': {'encoding': 'UTF-8', 'SaveAsShown': False,
                       'ExportFormulas': False, 'ExportAllSheets': True, 'IncludeBOM': True},
        'limitations': ['Оформление и редактируемые формулы в CSV не восстанавливаются.',
                        'Формат отображения дат и чисел может отличаться от исходного.'],
    },
}


def automatic_recipe_catalogue():
    """Local, finite catalogue; never accept caller-supplied native options."""
    return copy.deepcopy(list(_AUTOMATIC_RECIPES.values()))


def validate_retry_recipe(recipe_id, target):
    if (not isinstance(recipe_id, str) or recipe_id not in _AUTOMATIC_RECIPES or
            target not in _AUTOMATIC_RECIPES[recipe_id]['targets']):
        raise GuardError('AUTOMATIC_RECIPE_UNAVAILABLE',
                         'Для этого преобразования нет проверенной автонастройки.', stage='plan')
    return copy.deepcopy(_AUTOMATIC_RECIPES[recipe_id])


def automatic_recipe(recipe_id, target, native, proof):
    """Apply a vetted correction only to a currently confirmed native filter.

    This does not authorize a new route, modify a personal office profile or
    reuse a mutable document model. Ordinary exports never call this helper.
    """
    validate_retry_recipe(recipe_id, target)
    base = native_export_recipe(native, target, proof)
    if base is None:
        raise GuardError('AUTOMATIC_RECIPE_UNAVAILABLE',
                         'Установленный LibreOffice не подтвердил фильтр автонастройки.', stage='plan')
    warnings = tuple(base.warnings) + ('AUTOMATIC_RETRY_MAY_NOT_IMPROVE',)
    if recipe_id == 'pdf_preserve_images' and base.filter_name.endswith('_pdf_Export'):
        return ExportRecipe(base.filter_name, required_draw_pages=base.required_draw_pages,
                            warnings=warnings, filter_data=(('UseLosslessCompression', True),
                                                           ('ReduceImageResolution', False)))
    if recipe_id == 'csv_preserve_values' and base.filter_name == CALC_TEXT:
        item = native.filters.get(base.filter_name)
        if item and item[0] == CALC and 'EXPORT' in item[2]:
            separator = '44' if target == 'csv' else '9'
            # Official CSV tokens: UTF-8, full precision (not display text),
            # formula values, all sheets (-1), explicit UTF-8 BOM. Quoted text
            # and ordinary numeric cells keep their native export types.
            options = separator + ',34,76,1,,0,false,true,false,false,false,-1,false,true'
            return ExportRecipe(base.filter_name, options,
                                warnings=warnings + ('AUTOMATIC_CSV_RAW_VALUES',))
    raise GuardError('AUTOMATIC_RECIPE_UNAVAILABLE',
                     'Проверенная автонастройка не подходит модели этого документа.', stage='plan')


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
    if (target == 'svm' and services == [DRAW]
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
