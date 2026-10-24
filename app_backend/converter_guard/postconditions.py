"""Streaming checks of native results, never a substitute for fidelity QA.

An incomplete check must not turn into a successful conversion.  The streams
are bounded in memory, rather than by an arbitrary document-size limit.
"""
import codecs
import gzip
from html.parser import HTMLParser
from pathlib import Path
import re
import zlib

from .errors import GuardError
from .content_invariants import check_content_invariants
from .media_fidelity import check_media_preservation


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


def _cancelled(cancel):
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка результата отменена.', stage='result')


def _incomplete(reason):
    raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                     'Не удалось полностью проверить результат. Он не отмечен готовым.',
                     stage='result', diagnostic=reason)


class _AttributeLimiter:
    """Keep HTMLParser's pending tags small, including embedded image data.

    Attribute values are needed only to establish that a resource reference
    exists.  Preserve their prefix and their closing quote, while discarding
    the rest *only in the parser's inspection copy*.  Output files never change.
    Comments are copied literally so quotes in comments cannot swallow a body.
    """
    def __init__(self):
        self.in_tag = False
        self.quote = None
        self.kept = 0
        self.prefix = ''
        self.comment = False
        self.comment_tail = ''
        self.tag_head = ''
        self.rawtext = None
        self.raw_pending = ''

    def feed(self, text):
        result = []
        index = 0
        while index < len(text):
            if self.rawtext is not None:
                raw = self.raw_pending + text[index:]
                closing = re.search(r'</' + self.rawtext + r'(?=[\s/>])', raw, re.I)
                if closing is None:
                    keep = len(self.rawtext) + 3
                    result.append(raw[:-keep] if len(raw) > keep else '')
                    self.raw_pending = raw[-keep:]
                    return ''.join(result)
                result.append(raw[:closing.start()])
                text = raw[closing.start():]
                index = 0
                self.rawtext = None
                self.raw_pending = ''
            if self.quote is not None:
                closing = text.find(self.quote, index)
                end = len(text) if closing < 0 else closing
                keep = min(4096 - self.kept, end - index)
                if keep:
                    result.append(text[index:index + keep])
                    self.kept += keep
                if closing < 0:
                    return ''.join(result)
                result.append(self.quote)
                self.quote = None
                index = closing + 1
                continue
            char = text[index]
            index += 1
            if self.comment:
                result.append(char)
                self.comment_tail = (self.comment_tail + char)[-3:]
                if self.comment_tail == '-->':
                    self.comment = False
                    self.in_tag = False
                continue
            result.append(char)
            if not self.in_tag:
                if char == '<':
                    self.in_tag = True
                    self.prefix = '<'
                    self.tag_head = '<'
                continue
            if len(self.tag_head) < 64:
                self.tag_head += char
            if len(self.prefix) < 4:
                self.prefix += char
                if self.prefix == '<!--':
                    self.comment = True
                    self.comment_tail = ''
                    continue
            if char in ('"', "'"):
                self.quote = char
                self.kept = 0
            elif char == '>':
                self.in_tag = False
                opening = re.match(r'<(script|style)(?=[\s/>])', self.tag_head, re.I)
                if opening and not self.tag_head.rstrip().endswith('/>'):
                    self.rawtext = opening.group(1).lower()
        return ''.join(result)


def _check_gzip(paths, target, *, cancel, budget):
    candidates = [Path(p) for p in paths if Path(p).suffix.lower() == '.' + target]
    if not candidates:
        _incomplete('gzip_primary_missing')
    expanded = 0
    for path in candidates:
        produced = 0
        try:
            # GzipFile checks CRC and ISIZE at every member's EOF.  Reading just
            # a header, or stopping after an initial prefix, misses truncation.
            with gzip.open(path, 'rb') as stream:
                while True:
                    _cancelled(cancel)
                    block = stream.read(64 * 1024)
                    if not block:
                        break
                    produced += len(block)
                    expanded += len(block)
                    if budget is not None and expanded > budget:
                        _incomplete('gzip_inspection_budget')
        except (OSError, EOFError, zlib.error) as error:
            raise GuardError('ENGINE_OUTPUT_CONTAINER_INVALID',
                'LibreOffice создал повреждённый сжатый метафайл. Результат не отмечен готовым.',
                stage='result', diagnostic='gzip_integrity:' + type(error).__name__) from error
        if produced == 0:
            raise GuardError('ENGINE_OUTPUT_CONTENT_EMPTY',
                'LibreOffice создал пустой сжатый метафайл. Результат не отмечен готовым.',
                stage='result')
    return {'check': 'gzip_container', 'status': 'validated', 'files': len(candidates),
            'uncompressedBytes': expanded, 'qualityCompared': False}


def _check_html(paths, source, *, cancel, budget):
    extensions = {'.html', '.htm', '.xhtml'}
    candidates = [Path(p) for p in paths if Path(p).suffix.lower() in extensions]
    primary = next((p for p in candidates if p.stem == source.stem), None)
    if primary is None:
        _incomplete('html_primary_ambiguous')
    parser = _Body()
    limiter = _AttributeLimiter()
    inspected = 0
    decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
    try:
        with primary.open('rb') as stream:
            while block := stream.read(64 * 1024):
                _cancelled(cancel)
                inspected += len(block)
                if budget is not None and inspected > budget:
                    _incomplete('html_inspection_budget')
                parser.feed(limiter.feed(decoder.decode(block)))
                if len(parser.rawdata) > 256 * 1024:
                    _incomplete('html_parser_budget')
            parser.feed(limiter.feed(decoder.decode(b'', final=True)))
            if limiter.quote is not None or limiter.in_tag or limiter.rawtext is not None:
                _incomplete('html_unterminated_markup')
            parser.close()
    except (UnicodeError, ValueError, OSError) as error:
        _incomplete('html_encoding_syntax_or_read:' + type(error).__name__)
    _cancelled(cancel)
    if parser.content:
        # Existence of an <img> is not enough: its local exported sidecars must
        # also exist inside the output directory, without following links or
        # retrieving any network resource. Reuse the source dependency rules.
        from .core import _LocalHtmlResources
        from .html_resources import inspect_dependencies
        missing = []
        resources = inspect_dependencies(primary, _LocalHtmlResources, cancel=cancel,
            missing=missing, seconds=None, transform_factory=_AttributeLimiter)
        if resources == 'HTML_LOCAL_RESOURCES_NOT_UPLOADED':
            raise GuardError('ENGINE_OUTPUT_RESOURCE_MISSING',
                'В HTML отсутствует локальный ресурс результата. Он не отмечен готовым.',
                stage='result', diagnostic='html_missing_local_resources:' + str(len(missing)))
        if resources is not None:
            _incomplete('html_resources_not_inspected')
        return {'check': 'html_body', 'status': 'content_present', 'qualityCompared': False,
                'inspectedBytes': inspected, 'localResourcesChecked': True}
    if parser.dynamic:
        _incomplete('html_dynamic_content')
    raise GuardError('ENGINE_OUTPUT_CONTENT_EMPTY',
                     'LibreOffice создал HTML без содержимого документа. Результат не отмечен готовым.',
                     stage='result')


def check_native_output(paths, target, source, *, cancel, budget=None):
    """Validate complete containers/content before the adapter commits outputs.

    ``budget`` is an explicit caller-provided inspection quota, not a default
    file-size veto.  Exhaustion fails closed instead of returning ``ready``.
    """
    _cancelled(cancel)
    invariant = check_content_invariants(paths, target, source, cancel=cancel)
    media = check_media_preservation(paths, target, source, cancel=cancel)
    if media is not None:
        result = media
    elif target in ('emz', 'wmz'):
        result = _check_gzip(paths, target, cancel=cancel, budget=budget)
    elif target in ('html', 'htm', 'xhtml'):
        result = _check_html(paths, Path(source), cancel=cancel, budget=budget)
    else:
        result = None
    if result is not None and invariant is not None:
        return {**result, 'additionalChecks': [invariant]}
    return result if result is not None else invariant

