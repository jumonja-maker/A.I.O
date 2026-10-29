"""Export contracts run inside the existing loaded UNO model, not a new pass.

The worker uses the same source string as the unit tests, without importing the
application or extra packages into LibreOffice's embedded Python.
"""
NATIVE_FIDELITY_SOURCE = r'''
import json
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

class AioPresentationTextLoss(RuntimeError):
    pass

class AioPresentationUnverified(RuntimeError):
    pass


def aio_normal_text(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', value))


def aio_presentation_text(model):
    """Read text from the model already opened for this XHTML export."""
    pages = model.getDrawPages()
    texts, inspected, total_bytes = Counter(), 0, 0
    for index in range(pages.getCount()):
        stack = [pages.getByIndex(index)]
        while stack:
            shape = stack.pop()
            inspected += 1
            if inspected > 100000:
                raise AioPresentationUnverified('presentation_shape_budget')
            if hasattr(shape, 'getString'):
                value = shape.getString()
                if isinstance(value, str):
                    total_bytes += len(value.encode('utf-8'))
                    if total_bytes > 4 * 1024 * 1024:
                        raise AioPresentationUnverified('presentation_text_budget')
                    texts.update(t for t in (aio_normal_text(p) for p in value.splitlines()) if t)
            if hasattr(shape, 'getCount') and hasattr(shape, 'getByIndex'):
                count = shape.getCount()
                if inspected + count + len(stack) > 100000:
                    raise AioPresentationUnverified('presentation_shape_budget')
                stack.extend(shape.getByIndex(n) for n in range(count))
    return texts


class _AioExportText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.suppressed = [], 0
        self.alternative = False
    def handle_starttag(self, tag, attrs):
        if tag.lower() in ('script', 'style', 'head'):
            self.suppressed += 1
        if tag.lower() in ('svg', 'canvas', 'object', 'embed', 'iframe'):
            self.alternative = True
    def handle_endtag(self, tag):
        if tag.lower() in ('script', 'style', 'head'):
            self.suppressed = max(0, self.suppressed - 1)
    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def aio_verify_presentation_xhtml(paths, expected):
    if not expected:
        return
    parser = _AioExportText()
    consumed = 0
    for path in paths:
        if Path(path).suffix.lower() not in ('.html', '.htm', '.xhtml'):
            continue
        with Path(path).open('r', encoding='utf-8-sig', errors='strict') as stream:
            while True:
                block = stream.read(65536)
                if not block:
                    break
                consumed += len(block)
                if consumed > 16 * 1024 * 1024:
                    raise AioPresentationUnverified('presentation_output_budget')
                parser.feed(block)
    parser.close()
    observed = aio_normal_text(''.join(parser.parts))
    if len(observed) * len(expected) > 32 * 1024 * 1024:
        raise AioPresentationUnverified('presentation_match_budget')
    missing = sum(max(0, required - observed.count(text)) for text, required in expected.items())
    if missing:
        if parser.alternative:
            raise AioPresentationUnverified('alternative_graphic_representation')
        raise AioPresentationTextLoss('native_presentation_missing_paragraphs:' + str(missing))


def aio_export_graphic_pages(model, remote, prop, target):
    """Export every Draw/Impress page via the native page-specific encoder."""
    pages = model.getDrawPages()
    count = pages.getCount()
    mime = {'.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg',
            '.jfif':'image/jpeg', '.jif':'image/jpeg', '.jpe':'image/jpeg',
            '.bmp':'image/bmp', '.gif':'image/gif', '.tif':'image/tiff',
            '.tiff':'image/tiff', '.webp':'image/webp', '.svg':'image/svg+xml'}.get(target.suffix.lower())
    if count <= 1 or mime is None:
        return None
    exporter = remote.ServiceManager.createInstanceWithContext('com.sun.star.drawing.GraphicExportFilter', remote)
    if exporter is None or not exporter.supportsMimeType(mime):
        raise RuntimeError('NATIVE_PAGE_ENCODER_UNAVAILABLE')
    outputs, mapping = [], []
    for index in range(count):
        path = target if index == 0 else target.with_name(target.stem + '__page_%04d' % (index + 1) + target.suffix)
        exporter.setSourceDocument(pages.getByIndex(index))
        if not exporter.filter((prop('URL', path.as_uri()), prop('MediaType', mime))):
            raise RuntimeError('NATIVE_PAGE_EXPORT_FAILED:' + str(index + 1))
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError('NATIVE_PAGE_OUTPUT_MISSING:' + str(index + 1))
        outputs.append(path)
        mapping.append({'page': index + 1, 'file': path.name})
    manifest = target.parent / 'AIO-pages.json'
    manifest.write_text(json.dumps({'schema': 1, 'pageCount': count, 'pages': mapping}, ensure_ascii=False), encoding='utf-8')
    return outputs + [manifest]
'''
