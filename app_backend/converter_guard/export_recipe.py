"""Small native export settings, shared by CLI and UNO.

This never admits or rejects a source/target pair. A current native proof and
an unambiguous installed filter are required before overriding CLI detection.
The requested text dialect is independent from the source import dialect.
"""
from dataclasses import dataclass
import copy
import json

from .errors import GuardError
from ..pdf_export_policy import PDF_IMAGE_FILTER_DATA

CALC = 'com.sun.star.sheet.SpreadsheetDocument'
DRAW = 'com.sun.star.drawing.DrawingDocument'
WRITER = 'com.sun.star.text.TextDocument'
CALC_TEXT = 'Text - txt - csv (StarCalc)'
TEXT_OPTIONS = {'csv': '44,34,76,1,', 'tsv': '9,34,76,1,', 'tab': '9,34,76,1,'}
WARNING_TEXT = {
    'OOXML_PRIVATE_CONTENT_TYPES_NAMESPACE': (
        'LibreOffice не открыл исходный OOXML. Для повторного импорта во временной копии '
        'изменена только запись пространства имён [Content_Types].xml; состав и значения '
        'XML сохранены, остальные части пакета совпадают побайтно. Исходный файл не изменён. '
        'Это настройка совместимости импорта, а не подтверждение полного качества результата.'),
    'VBA_MACROS_EXPECTED_LOSS': (
        'Исходный файл содержит VBA-макросы. Выбранный формат результата не сохраняет их. '
        'Макросы не запускались; сохраните исходный файл, если они нужны.'),
    'VBA_VERIFIED_BYTE_IDENTITY': (
        'XLSM сохранён как проверенная байтовая копия без изменений. Все исходные '
        'байты, включая код и атрибуты VBA, сохранены; повторный экспорт LibreOffice '
        'не выполнялся. Макросы не запускались; их работа отдельно не проверялась.'),
    'VBA_PRESERVATION_NOT_CHECKED': (
        'Сохранность VBA-макросов не подтверждена: проверка не завершена либо нативный '
        'экспорт изменил их внутренние потоки. Макросы не запускались; сохраните исходник '
        'и проверьте совместимость отдельно.'),
    'VBA_STREAMS_PRESERVED': (
        'Все проверенные потоки проекта VBA сохранены побайтно. Макросы не запускались; '
        'их работа и совместимость с программой-получателем не проверены.'),
    'MEDIA_ALPHA_REMOVED': (
        'Формат результата не сохраняет прозрачность: исходный альфа-канал не перенесён. '
        'Проверьте фон изображения.'),
    'MEDIA_ANIMATION_FIRST_FRAME': (
        'Анимация не сохранена: статичный результат содержит только первый кадр. '
        'Для сохранения воспроизведения выберите APNG, GIF или WebP.'),
    'MEDIA_ANIMATION_AS_PAGES': (
        'Кадры анимации сохранены как отдельные страницы TIFF. '
        'Воспроизведение, задержки и повторы анимации не сохраняются.'),
    'MEDIA_IMAGE_PAGES_FIRST_PAGE': (
        'В статичном результате сохранена только первая страница исходного изображения. '
        'Для сохранения всех страниц выберите TIFF или PDF.'),
    'MEDIA_PAGES_AS_ANIMATION': (
        'Страницы исходного изображения представлены кадрами анимации. '
        'Это меняет просмотр и печать; для отдельных страниц используйте TIFF или PDF.'),
    'MEDIA_BIT_DEPTH_REDUCED': (
        'Глубина цвета результата уменьшена: часть точности цветовых значений не сохраняется.'),
    'MEDIA_GIF_PALETTE_LIMITS': (
        'GIF ограничен палитрой до 256 цветов и однобитной прозрачностью. '
        'Проверка результата не подтверждает полное визуальное совпадение.'),
    'MEDIA_PHOTOCD_BASE_ONLY': (
        'Для результата использован слой PhotoCD Base (768 × 512 пикселей до поворота). '
        'Наличие и сохранность слоёв 4Base, 16Base и более высокого разрешения '
        'не проверены; сравнение с ними не выполнялось.'),
    'MEDIA_PSD_MERGED_COMPOSITE': (
        'PSD преобразован в одно сведённое RGB-изображение. '
        'Слои, маски, редактируемые текстовые и векторные объекты PSD '
        'не переносятся в результат.'),
    'MEDIA_PROPERTIES_CHANGED': (
        'Некоторые свойства исходного изображения не сохраняются выбранным форматом. '
        'Перечень изменений указан в отчёте результата.'),
    'NATIVE_CALC_RASTER_PAGES': (
        'Каждая печатная страница Calc сохранена отдельным изображением. '
        'Имена, листы, диапазоны и разрешение указаны в AIO-pages.json. '
        'Скрытые листы и ячейки вне печатных областей не включаются; '
        'полное совпадение импорта и оформления не проверено.'),
    'NATIVE_WRITER_RASTER_PAGES': (
        'Каждая страница Writer экспортирована отдельным изображением. '
        'Номера и имена страниц указаны в AIO-pages.json; точность '
        'внешнего вида и шрифтов отдельно не сравнивалась.'),
    'NATIVE_SYLK_ALL_SHEETS': ('Каждый лист сохранён отдельным SLK/SYLK. Формулы представлены вычисленными значениями; межлистовые связи этим режимом не сохраняются.'),
    'NATIVE_CSV_ALL_SHEETS': (
        'Каждый лист книги сохранён отдельным файлом CSV/TAB/TSV; индекс и имя листа '
        'указаны в AIO-sheets.json. Формулы представлены вычисленными значениями. '
        'Оформление и диаграммы эти форматы не сохраняют. Проверка значений относится '
        'к импортированной модели LibreOffice, не к полному качеству исходника.'),
    'CSV_TEXT_IDS_NOT_CHECKED': (
        'Не удалось доказать сохранность всех идентификаторов с ведущими нулями, '
        'чисел высокой точности и формулоподобных текстовых значений '
        'при импорте CSV/TAB/TSV в Excel: превышен бюджет, неизвестна структура '
        'или недоступен анализатор. Это не подтверждённая потеря, но и не '
        'проверенная сохранность. Подробная причина есть в resultChecks.'),
    'CSV_HIGH_PRECISION_AS_TEXT': (
        'Числа CSV/TAB/TSV с более чем 15 значащими десятичными цифрами сохранены '
        'как текст, чтобы исключить округление. Другие значения в этих столбцах '
        'также могут импортироваться как текст. Проверка относится к исходной '
        'записи и типу этих ячеек, а не к полному оформлению книги.'),
    'CSV_FORMULA_LITERALS_AS_TEXT': (
        'Значения CSV/TAB/TSV, начинающиеся со знака «=», сохранены как текст, '
        'без выполнения как формул. Другие значения в этих столбцах также могут '
        'импортироваться как текст.'),
    'DIF_TABLE_CONTENT_NOT_CHECKED': (
        'Полная проверка ячеек DIF для этого результата не завершена: неподдержанная '
        'грамматика, сопоставление таблиц либо бюджет проверки. Это не доказательство '
        'повреждения и не подтверждение сохранности всех значений. Причина указана в отчёте.'),
    'NATIVE_WRITER_TEXT_TAIL_PARAGRAPH': (
        'Перед текстовым экспортом Writer во временную копию OOXML добавлен один пустой '
        'абзац после конечной таблицы: это предотвращает повтор последней ячейки в LibreOffice. '
        'Исходный файл и содержимое существующих частей не изменены. '
        'TXT остаётся обычным текстом; оформление и изображения не сохраняются.'),
    'DIF_PRIVATE_NUMERIC_LOCALE_EN_US': (
        'Для импорта DIF применена числовая локаль en-US во временном профиле LibreOffice. '
        'Числа с десятичной точкой проверяются по значениям и типам. '
        'Отображение неявных числовых форматов может отличаться от системной локали.'),
    'DIF_NUMERIC_CONTENT_NOT_CHECKED': (
        'Числовая проверка DIF не завершена: грамматика либо бюджет инспекции не поддержаны. '
        'Файл обрабатывает штатный LibreOffice; совпадение всех чисел и типов не подтверждено.'),
    'NATIVE_DIF_NUMERIC_IMPORT_VERIFIED': (
        'Числа и координаты DIF проверены в нативном FODS-снимке перед экспортом. '
        'Это проверка импорта чисел, а не полной сохранности конечного документа.'),
    'NATIVE_BMP_HEADER_CORRECTED': (
        'Исправлены четыре байта размера BMP после подтверждённой ошибки нативного экспортёра. '
        'Размер полотна, пиксели и остальные байты не изменены; строгая проверка BMP сохранена.'),
    'AUTOMATIC_RETRY_MAY_NOT_IMPROVE': (
        'Применён проверенный набор параметров экспорта. Он не гарантирует улучшение; '
        'результат повторно проверяется, предыдущая версия остаётся в AIO.'),
    'AUTOMATIC_CSV_RAW_VALUES': (
        'CSV повторно экспортирован в UTF-8 с исходной числовой точностью и отдельным файлом '
        'для каждого листа. Отображение дат, валют и округлённых чисел может измениться. '
        'Формулы представлены вычисленными значениями; оформление CSV не поддерживает.'),
    'NATIVE_EMF_RECORD_COUNT_CORRECTED': (
        'В нативном EMF исправлен подтверждённый счётчик записей заголовка. '
        'Графические записи не изменены; структура проверена повторно.'),
    'NATIVE_DBF_ENCODING_NOT_CHECKED': (
        'DBF содержит ненулевой LDID. Его байты и объявленная кодировка сохранены; '
        'соответствие UTF-8 и совместимость с получателем не подтверждены.'),
    'NATIVE_DBF_UTF8_CPG': (
        'При экспорте DBF в UTF-8 создан сопровождающий файл .cpg. '
        'Его нужно сохранять рядом с DBF; совместимость конкретного получателя проверяется отдельно.'),
    'NATIVE_DBF_CP1251_CPG': (
        'Все тексты листа представимы в Windows-1251. DBF создан штатным экспортёром '
        'в этой кодировке; LDID C9 и сопровождающий .cpg явно указывают Windows-1251. '
        'Храните DBF, .cpg, .dbt и файлы соответствия колонок вместе.'),
    'NATIVE_DBF_NUMERIC_WIDTHS': (
        'Ширина и точность числовых полей DBF рассчитаны по всем нативным значениям '
        'столбца, включая знак и дробную часть. Значения проверены после экспорта; '
        'расчётная минимальная ширина и точность записаны в .aio-columns.jsonl. '
        'Экспортёр может расширить поле без изменения значений.'),
    'NATIVE_DBF_CP1251_REQUIRES_ENCODING_SELECTION': (
        'Некоторые программы, включая автоматический импорт LibreOffice, игнорируют '
        'LDID и .cpg у DBF. При открытии выберите Windows-1251 явно; '
        'автоматическое отображение зависит от программы-получателя.'),
    'NATIVE_DBF_UTF8_REQUIRES_ENCODING_SELECTION': (
        'Unicode и совместимость Memo сохраняются штатным экспортом UTF-8 с .cpg. Некоторые программы, '
        'включая автоматический импорт LibreOffice, игнорируют .cpg у DBF. '
        'При открытии выберите UTF-8 явно; автоматическое отображение не гарантируется.'),
    'NATIVE_DBF_CLI_RECOVERY': (
        'Первая попытка штатного UNO-экспорта DBF не создала файл. '
        'Guard выполнил одну изолированную повторную попытку через штатный CLI LibreOffice '
        'с теми же подтверждёнными фильтрами CSV/dBase. DBF создан и проверен структурно; '
        'полное совпадение значений и кодировки отдельно не гарантируется.'),
    'NATIVE_DBF_FIELD_NAME_MAPPING': (
        'Некоторые названия столбцов нельзя однозначно представить в DBF. '
        'Назначены уникальные имена полей; полные исходные названия и соответствия '
        'сохранены в .aio-columns.jsonl. Храните этот файл рядом с DBF.'),
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
        'Все страницы PDF преобразованы в растровые изображения штатным Qt с разрешением 144 dpi. '
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
    'NATIVE_CALC_PDF_PRINT_LAYOUT': (
        'PDF повторяет разметку печати таблицы. Длинный текст в узких ячейках '
        'может обрезаться, а данные вне области печати — не попасть в PDF. '
        'Перед экспортом проверьте ширину столбцов, перенос текста и область печати в исходной книге. '
        'Для сохранения редактируемых данных используйте XLSX или ODS.'),
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
    if target == 'xhtml':
        # HTML filters also claim the .xhtml extension. Require the real exporter.
        names = {CALC: 'XHTML Calc File', WRITER: 'XHTML Writer File',
                 DRAW: 'XHTML Draw File',
                 'com.sun.star.presentation.PresentationDocument': 'XHTML Impress File'}
        eligible = list(services)
        if proof.get('nativeTextDocument') is True and WRITER not in eligible:
            eligible.append(WRITER)
        for service in eligible:
            name = names.get(service)
            item = native.filters.get(name)
            if item and item[0] == service and 'EXPORT' in item[2]:
                return ExportRecipe(name)
        raise GuardError('XHTML_EXPORT_UNAVAILABLE',
            'Установленный движок не подтвердил настоящий XHTML-экспорт. HTML не будет выдан под расширением XHTML.', stage='route')
    if selected == 'SYLK' and target in ('slk', 'sylk'):
        return ExportRecipe(selected, warnings=('NATIVE_SYLK_ALL_SHEETS',))
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
    if (target in ('png', 'jpg', 'jpeg', 'jfif', 'jif', 'jpe', 'webp', 'bmp', 'gif', 'tif', 'tiff')
            and WRITER in services):
        alternative = native.export_filter('jpg' if target in ('jfif','jif','jpe') else target, [WRITER])
        if alternative:
            selected = alternative
    if not selected:
        return None
    if target == 'pdf' and selected.endswith('_pdf_Export'):
        return ExportRecipe(selected, filter_data=PDF_IMAGE_FILTER_DATA,
                            warnings=('NATIVE_CALC_PDF_PRINT_LAYOUT',) if selected == 'calc_pdf_Export' else ())
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
