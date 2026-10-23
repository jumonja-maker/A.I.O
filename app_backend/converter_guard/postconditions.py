"""Bounded checks of native results, never a substitute for fidelity QA."""
from html.parser import HTMLParser
from pathlib import Path
import re

from .errors import GuardError


class _Body(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.body = False
        self.ignored = []
        self.content = False
        self.dynamic = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'body':
            self.body = True
        if tag in ('script', 'style', 'template', 'head'):
            self.ignored.append(tag)
        if tag in ('script', 'canvas') or 'url(' in attrs.get('style', '').lower():
            self.dynamic = True
        if self.body and not self.ignored:
            if tag in ('img', 'image', 'embed', 'video', 'audio', 'object'):
                self.content |= bool(attrs.get('src') or attrs.get('href') or
                                     attrs.get('xlink:href') or attrs.get('data'))
            if tag in ('path', 'polygon', 'polyline', 'rect', 'circle', 'ellipse', 'line'):
                self.content = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()
        if tag == 'body':
            self.body = False

    def handle_data(self, data):
        if self.ignored and self.ignored[-1] == 'style' and re.search(r'(?i)url\s*\(', data):
            self.dynamic = True
        if self.body and not self.ignored and data.strip(' \r\n\t\u00a0\u200b\ufeff'):
            self.content = True


def check_native_output(paths, target, source, *, cancel, budget=1024 * 1024):
    if target not in ('html', 'htm', 'xhtml'):
        return None
    extensions = {'.html', '.htm', '.xhtml'}
    candidates = [Path(p) for p in paths if Path(p).suffix.lower() in extensions]
    primary = next((p for p in candidates if p.stem == source.stem), None)
    if primary is None:
        return {'check': 'html_body', 'status': 'not_checked', 'reason': 'primary_ambiguous'}
    parser = _Body()
    inspected = 0
    import codecs
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    try:
        with primary.open('rb') as stream:
            while block := stream.read(64 * 1024):
                if cancel.is_set():
                    raise GuardError('CANCELLED', 'Проверка результата отменена.', stage='result')
                inspected += len(block)
                if inspected > budget:
                    return {'check': 'html_body', 'status': 'not_checked', 'reason': 'inspection_budget'}
                parser.feed(decoder.decode(block))
                if len(parser.rawdata) > 256 * 1024:
                    return {'check': 'html_body', 'status': 'not_checked', 'reason': 'parser_budget'}
            parser.feed(decoder.decode(b'', final=True))
            parser.close()
    except (UnicodeError, ValueError):
        return {'check': 'html_body', 'status': 'not_checked', 'reason': 'encoding_or_syntax'}
    if parser.content:
        return {'check': 'html_body', 'status': 'content_present', 'qualityCompared': False}
    if parser.dynamic:
        return {'check': 'html_body', 'status': 'not_checked', 'reason': 'dynamic_content'}
    raise GuardError('ENGINE_OUTPUT_CONTENT_EMPTY',
                     'LibreOffice создал HTML без содержимого документа. Результат не отмечен готовым.',
                     stage='result')
