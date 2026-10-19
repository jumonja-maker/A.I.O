"""Small, explicit first release: native Writer/Calc/Impress PDF export.

This is coverage of this adapter, not a claim about LibreOffice's full abilities.
No document parser, text extraction or intermediate renderer gates the route.
"""
from pathlib import Path

FAMILIES = {
    'writer': ('docx', 'doc', 'odt', 'rtf'),
    'calc': ('xlsx', 'xls', 'ods'),
    'impress': ('pptx', 'ppt', 'odp'),
}
PDF_FILTERS = {'writer': 'writer_pdf_Export', 'calc': 'calc_pdf_Export',
               'impress': 'impress_pdf_Export'}
INPUTS = tuple(ext for group in FAMILIES.values() for ext in group)


def family_for(name: str) -> str | None:
    suffix = Path(name).suffix.lower().lstrip('.')
    return next((family for family, extensions in FAMILIES.items()
                 if suffix in extensions), None)


def inspect_files(files: list[dict], available: bool) -> dict:
    rows, compatible = [], []
    for file in files:
        family = family_for(file['name'])
        code = 'ENGINE_NOT_FOUND' if not available else (
            None if family else 'INPUT_NOT_CONNECTED')
        rows.append({'id': file['id'], 'name': file['name'], 'family': family,
                     'formats': ['pdf'] if available and family else [],
                     'code': code})
        if available and family:
            compatible.append(file['id'])
    return {'files': rows, 'formats': [{
        'id': 'pdf', 'label': 'PDF', 'category': 'documents',
        'enabled': bool(compatible), 'compatibleFileIds': compatible,
        'unsupportedFiles': [r['id'] for r in rows if r['id'] not in compatible],
        'description': 'Прямой экспорт LibreOffice в PDF.', 'options': [],
        'help': [{'title': 'Движок', 'text': 'Документ целиком обрабатывает LibreOffice.'},
                 {'title': 'Качество', 'text': 'Сжатие изображений без потерь, без снижения разрешения.'},
                 {'title': 'Ограничения', 'text': 'PDF сохраняет представление документа. Редактируемость, макросы и интерактивные функции исходного формата не переносятся.'}]
    }], 'message': ('LibreOffice не найден. Укажите AIO_LIBREOFFICE или установите LibreOffice.' if not available else
                   'Готово к экспорту в PDF через LibreOffice.' if compatible else
                   'Выбранные типы файлов пока не подключены к Guard. В первой версии доступны документы Writer, Calc и Impress → PDF.')}
