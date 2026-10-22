"""Small native export settings, shared by CLI and UNO.

This never admits or rejects a source/target pair. A current native proof and
an unambiguous installed filter are required before overriding CLI detection.
The requested text dialect is independent from the source import dialect.
"""
from dataclasses import dataclass

CALC = 'com.sun.star.sheet.SpreadsheetDocument'
DRAW = 'com.sun.star.drawing.DrawingDocument'
CALC_TEXT = 'Text - txt - csv (StarCalc)'
TEXT_OPTIONS = {'csv': '44,34,76,1,', 'tsv': '9,34,76,1,', 'tab': '9,34,76,1,'}
WARNING_TEXT = {
    'NATIVE_DRAW_HTML_CONTENT_RISK': (
        'Штатный HTML-фильтр Draw может создавать файл без содержимого рисунка. '
        'Для нескольких страниц расположение в XHTML тоже требует проверки. '
        'Сохраните исходник; для просмотра предпочтителен PDF.')}


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
    if (not native.valid or not proof or
            proof.get('registryFingerprint') != native.fingerprint or
            native.observed_decision(target, proof) != 'registered'):
        return None
    services = proof.get('documentServices') or [proof.get('documentService')]
    if not isinstance(services, list) or not services or any(not isinstance(s, str) for s in services):
        return None
    selected = native.export_filter(target, services)
    # draw_html_Export can emit an empty HTML shell. The installed XHTML
    # exporter preserves a single Draw page and is valid HTML syntax as well.
    # Do not apply this to multi-page drawings: native absolute positioning
    # can overlap pages. No custom renderer or suffix-based input admission.
    if (selected == 'draw_html_Export' and target in ('html', 'htm') and services == [DRAW]
            and type(proof.get('drawPages')) is int and proof['drawPages'] == 1
            and 'xhtml' not in native.uncertain_extensions):
        alternative = native.export_filter('xhtml', services)
        if alternative == 'XHTML Draw File':
            return ExportRecipe(alternative, required_draw_pages=1)
    if not selected:
        return None
    item = native.filters.get(selected)
    # Calc's CSV filter implements these native FilterOptions. A Writer text
    # filter or third-party filter with similar extensions does not inherit it.
    options = (TEXT_OPTIONS.get(target) if selected == CALC_TEXT and item and
               item[0] == CALC and target in item[1] and 'EXPORT' in item[2] else None)
    warnings = (('NATIVE_DRAW_HTML_CONTENT_RISK',)
                if selected == 'draw_html_Export' and target in ('html', 'htm') else ())
    return ExportRecipe(selected, options, warnings=warnings)
