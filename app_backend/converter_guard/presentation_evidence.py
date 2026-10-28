"""Mandatory, conservative presentation -> HTML content-loss evidence.

No browser, network request, OCR or conversion engine runs here.  The check
proves only missing source text/graphics where the entire output bundle is
inspectable.  A full-slide raster, SVG, CSS graphic or embedded object is an
unknown representation, not evidence that the source text disappeared.
"""
import base64
import binascii
import codecs
from collections import Counter
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import posixpath
import re
import unicodedata
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

from .errors import GuardError

OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
DRAW = 'urn:oasis:names:tc:opendocument:xmlns:drawing:1.0'
TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
XLINK = 'http://www.w3.org/1999/xlink'
PRESENTATION = ('http://schemas.openxmlformats.org/presentationml/2006/main',
                'http://purl.oclc.org/ooxml/presentationml/main')
DRAWING = ('http://schemas.openxmlformats.org/drawingml/2006/main',
           'http://purl.oclc.org/ooxml/drawingml/main')
RELATIONSHIPS = 'http://schemas.openxmlformats.org/package/2006/relationships'
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_HASH_BYTES = 256 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_NODES = 200000
MAX_ITEMS = 10000
MAX_INLINE_ATTRIBUTE = 2 * 1024 * 1024
MAX_MATCH_WORK = 32 * 1024 * 1024


class _Limited(ValueError):
    pass


def _cancel(cancel):
    if cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка презентации отменена.', stage='result')


def _normal(value):
    return ' '.join(unicodedata.normalize('NFC', value).split())


class _Budget:
    def __init__(self, cancel):
        self.cancel = cancel
        self.xml = self.hashed = self.nodes = self.text = 0

    def check(self):
        _cancel(self.cancel)

    def hash(self, stream):
        digest = hashlib.sha256()
        while block := stream.read(65536):
            self.check()
            self.hashed += len(block)
            if self.hashed > MAX_HASH_BYTES:
                raise _Limited('presentation_asset_hash_budget')
            digest.update(block)
        return digest.hexdigest()


class _Reader:
    def __init__(self, stream, budget):
        self.stream, self.budget, self.tail = stream, budget, b''

    def read(self, size=-1):
        self.budget.check()
        block = self.stream.read(min(65536, size) if size >= 0 else 65536)
        self.budget.xml += len(block)
        if self.budget.xml > MAX_XML_BYTES:
            raise _Limited('presentation_xml_budget')
        probe = (self.tail + block).replace(b'\0', b'').lower()
        if b'<!doctype' in probe or b'<!entity' in probe:
            raise _Limited('presentation_xml_declarations')
        self.tail = probe[-16:]
        return block


def _events(stream, budget):
    depth = 0
    for event, element in ET.iterparse(_Reader(stream, budget), events=('start', 'end')):
        budget.check()
        if event == 'start':
            depth += 1
            budget.nodes += 1
            if depth > 128 or budget.nodes > MAX_NODES:
                raise _Limited('presentation_xml_structure_budget')
        yield event, element
        if event == 'end':
            depth -= 1


def _member(origin, target):
    uri = urlsplit(target)
    value = unquote(uri.path)
    if uri.scheme or uri.netloc or uri.query or uri.fragment or '\\' in value:
        raise _Limited('presentation_external_or_ambiguous_part')
    value = posixpath.normpath(posixpath.join(posixpath.dirname(origin), value))
    if value.startswith(('../', '/')) or value in ('.', '..'):
        raise _Limited('presentation_part_outside_package')
    return value


def _relations(archive, origin, budget):
    name = posixpath.join(posixpath.dirname(origin), '_rels', posixpath.basename(origin) + '.rels')
    try:
        stream = archive.open(name)
    except KeyError:
        return {}
    relations = {}
    with stream:
        for event, item in _events(stream, budget):
            if event == 'end':
                if item.tag == '{' + RELATIONSHIPS + '}Relationship':
                    rid, kind, target = item.get('Id'), item.get('Type', ''), item.get('Target')
                    if not rid or rid in relations or not target:
                        raise _Limited('presentation_relationship_ambiguous')
                    relations[rid] = (kind.rsplit('/', 1)[-1], None if
                        item.get('TargetMode', '').lower() == 'external' else _member(origin, target))
                    if len(relations) > MAX_ITEMS:
                        raise _Limited('presentation_relationship_budget')
                item.clear()
    return relations


def _odf_paragraph_text(item):
    pieces = [item.text or '']
    for child in item:
        if child.tag in ('{' + OFFICE + '}annotation', '{' + TEXT + '}note',
                         '{' + TEXT + '}tracked-changes'):
            pass
        elif child.tag in ('{' + TEXT + '}s', '{' + TEXT + '}tab', '{' + TEXT + '}line-break'):
            pieces.append(' ')
        else:
            pieces.append(_odf_paragraph_text(child))
        pieces.append(child.tail or '')
    return ''.join(pieces)


def _paragraph_value(item, odf):
    if odf:
        return _normal(_odf_paragraph_text(item))
    pieces = []
    for child in item.iter():
        if any(child.tag == '{' + ns + '}t' for ns in DRAWING):
            pieces.append(child.text or '')
        elif any(child.tag in ('{' + ns + '}br', '{' + ns + '}tab') for ns in DRAWING):
            pieces.append(' ')
    return _normal(''.join(pieces))


def _scan_slide(stream, budget, *, odf=False):
    """Keep at most one paragraph, counts and bounded text values in memory."""
    paragraphs, images, depth, active, paragraph = Counter(), [], 0, False, None
    inline_assets = []
    inside_image = 0
    for event, item in _events(stream, budget):
        local = item.tag.rsplit('}', 1)[-1]
        is_paragraph = (item.tag in ('{' + TEXT + '}p', '{' + TEXT + '}h') if odf else
                        any(item.tag == '{' + ns + '}p' for ns in DRAWING))
        if event == 'start':
            if odf and active and depth and item.tag == '{' + DRAW + '}image':
                inside_image += 1
            if odf and item.tag == '{' + OFFICE + '}presentation':
                active = True
            if odf and active and item.tag == '{' + DRAW + '}page':
                depth += 1
            if is_paragraph and (not odf or active and depth):
                if paragraph is not None:
                    raise _Limited('presentation_nested_paragraph')
                paragraph = item
            if not odf and local == 'blip' and any(item.tag == '{' + ns + '}blip' for ns in DRAWING):
                rid = next((value for key, value in item.attrib.items()
                            if key.rsplit('}', 1)[-1] == 'embed'), None)
                images.append(rid)
            elif odf and active and depth and item.tag == '{' + DRAW + '}image':
                images.append(item.get('{' + XLINK + '}href'))
            if len(images) > MAX_ITEMS:
                raise _Limited('presentation_image_budget')
        else:
            if odf and inside_image and item.tag == '{' + OFFICE + '}binary-data':
                encoded = re.sub(r'\s+', '', item.text or '')
                if len(encoded) > MAX_INLINE_ATTRIBUTE:
                    raise _Limited('presentation_inline_source_asset_budget')
                try:
                    inline_assets.append(hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest())
                except (ValueError, binascii.Error) as error:
                    raise _Limited('presentation_inline_source_asset_invalid') from error
            if odf and inside_image and item.tag == '{' + DRAW + '}image':
                inside_image -= 1
            if item is paragraph:
                # Inline runs concatenate; a run boundary is not a word gap.
                value = _paragraph_value(item, odf)
                if value:
                    budget.text += len(value.encode('utf-8'))
                    if budget.text > MAX_TEXT_BYTES or len(paragraphs) > MAX_ITEMS:
                        raise _Limited('presentation_text_budget')
                    paragraphs[value] += 1
                paragraph = None
            if odf and active and item.tag == '{' + DRAW + '}page':
                depth -= 1
            if paragraph is None:
                item.clear()
    return paragraphs, images, active, inline_assets


def _source_presentation(source, cancel):
    source = Path(source)
    budget = _Budget(cancel)
    if not source.is_file():
        return None
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if len(names) > MAX_ITEMS or len(names) != len(set(names)):
                raise _Limited('presentation_package_members')
            if 'ppt/presentation.xml' in names:
                part = 'ppt/presentation.xml'
                relations = _relations(archive, part, budget)
                slides = []
                with archive.open(part) as stream:
                    for event, item in _events(stream, budget):
                        if event == 'end':
                            if any(item.tag == '{' + ns + '}sldId' for ns in PRESENTATION):
                                rid = next((value for key, value in item.attrib.items()
                                            if key.endswith('}id')), None)
                                kind, target = relations.get(rid, ('', None))
                                if kind != 'slide' or target not in names or target in slides:
                                    raise _Limited('presentation_active_slide_mapping')
                                slides.append(target)
                            item.clear()
                if not slides:
                    raise _Limited('presentation_active_slides_missing')
                text, image_refs = Counter(), []
                inline_assets = []
                for slide in slides:
                    with archive.open(slide) as stream:
                        paragraphs, images, _, _ = _scan_slide(stream, budget)
                    text.update(paragraphs)
                    rels = _relations(archive, slide, budget)
                    for rid in images:
                        kind, member = rels.get(rid, ('', None))
                        if kind != 'image' or member not in names:
                            raise _Limited('presentation_source_image_not_embedded')
                        image_refs.append(member)
            elif 'mimetype' in names and 'content.xml' in names:
                with archive.open('mimetype') as stream:
                    mime = stream.read(256).strip()
                if mime not in (b'application/vnd.oasis.opendocument.presentation',
                                b'application/vnd.oasis.opendocument.presentation-template'):
                    return None
                with archive.open('content.xml') as stream:
                    text, images, active, inline_assets = _scan_slide(stream, budget, odf=True)
                if not active:
                    raise _Limited('presentation_odf_body_missing')
                image_refs = [_member('content.xml', image) for image in images if image]
                if (len(image_refs) + len(inline_assets) != len(images) or
                        any(ref not in names for ref in image_refs)):
                    raise _Limited('presentation_source_image_not_embedded')
            else:
                return None
            hashes = set(inline_assets)
            for member in set(image_refs):
                with archive.open(member) as stream:
                    hashes.add(budget.hash(stream))
            return {'paragraphs': text, 'imageOccurrences': len(image_refs) + len(inline_assets), 'assetHashes': hashes}
    # Flat ODF is content-confirmed, with no extension-based admission.
    with source.open('rb') as stream:
        head = stream.read(1024)
    if not head.lstrip().startswith((b'<?xml', b'<office:')):
        return None
    with source.open('rb') as stream:
        text, images, active, inline_assets = _scan_slide(stream, budget, odf=True)
    if not active:
        return None
    if images:
        # Flat ODF may embed office:binary-data instead of href.  Text and
        # image-category counts are still useful; exact asset hashes unknown.
        return {'paragraphs': text, 'imageOccurrences': len(images), 'assetHashes': set(inline_assets)}
    return {'paragraphs': text, 'imageOccurrences': 0, 'assetHashes': set()}


class _HtmlEvidence(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.body = False
        self.ignored = []
        self.text = []
        self.text_bytes = 0
        self.images = []
        self.alternative = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'body':
            self.body = True
        if tag in ('head', 'script', 'style', 'template'):
            self.ignored.append(tag)
        if tag == 'script' or tag == 'link' and 'stylesheet' in attrs.get('rel', '').lower():
            self.alternative = True
        if self.body and not self.ignored:
            if tag in ('svg', 'canvas', 'object', 'embed', 'video', 'audio', 'iframe', 'input'):
                self.alternative = True
            if 'url(' in attrs.get('style', '').lower():
                self.alternative = True
            if tag in ('img', 'image'):
                self.images.append(attrs.get('src') or attrs.get('href') or attrs.get('xlink:href'))
                if len(self.images) > MAX_ITEMS:
                    raise _Limited('presentation_html_image_budget')
            if tag in ('br', 'p', 'div', 'td', 'th', 'li', 'h1', 'h2', 'h3'):
                self.text.append(' ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()
        if tag == 'body':
            self.body = False
        if self.body and not self.ignored and tag in ('p', 'div', 'td', 'th', 'li', 'h1', 'h2', 'h3'):
            self.text.append(' ')

    def handle_data(self, data):
        if self.ignored and self.ignored[-1] == 'style' and re.search(r'(?i)url\s*\(|(?:^|[;{])\s*content\s*:', data):
            self.alternative = True
        if self.body and not self.ignored:
            self.text_bytes += len(data.encode('utf-8'))
            if self.text_bytes > MAX_TEXT_BYTES:
                raise _Limited('presentation_html_text_budget')
            self.text.append(data)


def _html_evidence(paths, source, cancel, budget):
    from .postconditions import _AttributeLimiter
    candidates = [Path(p) for p in paths if Path(p).suffix.lower() in ('.html', '.htm', '.xhtml')]
    primary = next((path for path in candidates if path.stem == Path(source).stem), None)
    if primary is None:
        raise _Limited('presentation_html_primary_ambiguous')
    # Additional HTML pages may contain the remaining slides.  Compare the
    # entire native bundle instead of just its landing page.
    parser = _HtmlEvidence()
    hashes = set()
    bytes_seen = 0
    for candidate in candidates:
        limiter = _AttributeLimiter(attribute_limit=MAX_INLINE_ATTRIBUTE)
        decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
        local = _HtmlEvidence()
        with candidate.open('rb') as stream:
            while block := stream.read(65536):
                budget.check()
                bytes_seen += len(block)
                if bytes_seen > MAX_XML_BYTES:
                    raise _Limited('presentation_html_read_budget')
                local.feed(limiter.feed(decoder.decode(block)))
                if len(local.rawdata) > MAX_INLINE_ATTRIBUTE + 65536:
                    raise _Limited('presentation_html_parser_budget')
            local.feed(limiter.feed(decoder.decode(b'', final=True)))
        if limiter.in_tag or limiter.quote is not None or limiter.rawtext is not None:
            raise _Limited('presentation_html_unterminated_markup')
        local.close()
        parser.alternative |= local.alternative or limiter.truncated
        parser.text.extend(local.text)
        parser.text_bytes += local.text_bytes
        if parser.text_bytes > MAX_TEXT_BYTES:
            raise _Limited('presentation_html_text_budget')
        parser.images.extend(local.images)
        if len(parser.images) > MAX_ITEMS:
            raise _Limited('presentation_html_image_budget')
        for value in local.images:
            if not value:
                parser.alternative = True
                continue
            if value.lower().startswith('data:'):
                try:
                    heading, data = value.split(',', 1)
                    if ';base64' not in heading.lower():
                        parser.alternative = True
                        continue
                    raw = base64.b64decode(re.sub(r'\s+', '', data), validate=True)
                    hashes.add(hashlib.sha256(raw).hexdigest())
                except (ValueError, binascii.Error):
                    parser.alternative = True
                continue
            uri = urlsplit(value)
            if uri.scheme or uri.netloc or uri.query or uri.fragment:
                parser.alternative = True
                continue
            relative = unquote(uri.path)
            asset = candidate.parent / relative
            root = primary.parent.resolve()
            try:
                if ('\\' in relative or relative.startswith('/') or
                        not asset.resolve(strict=True).is_relative_to(root) or
                        any(part.is_symlink() for part in (asset, *asset.parents))):
                    parser.alternative = True
                    continue
                with asset.open('rb') as stream:
                    hashes.add(budget.hash(stream))
            except OSError:
                parser.alternative = True
    return parser, hashes


def check_presentation_html(paths, target, source, *, cancel, source_evidence=None):
    if target not in ('html', 'htm', 'xhtml'):
        return None
    try:
        expected = (_source_presentation(source, cancel) if source_evidence is None else
            source_evidence.get('presentation_content', source,
                lambda: _source_presentation(source, cancel)))
    except (_Limited, ET.ParseError, zipfile.BadZipFile, KeyError, OSError, ValueError) as error:
        return {'check': 'presentation_html_content', 'status': 'not_checked',
                'reason': 'source_inspection_unavailable', 'inspectionLimit': type(error).__name__,
                'qualityCompared': False}
    if expected is None:
        return None
    base = {'check': 'presentation_html_content', 'qualityCompared': False,
            'sourceTextParagraphs': sum(expected['paragraphs'].values()),
            'sourceImageOccurrences': expected['imageOccurrences']}
    try:
        observed, hashes = _html_evidence(paths, source, cancel, _Budget(cancel))
    except (_Limited, UnicodeError, OSError, ValueError) as error:
        raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
            'Не удалось проверить содержимое презентации в HTML. Результат не подтверждён.',
            stage='result', diagnostic='presentation_html:' + type(error).__name__) from error
    text = _normal(''.join(observed.text))
    if expected['imageOccurrences'] and not observed.images and not observed.alternative:
        raise GuardError('ENGINE_OUTPUT_CONTENT_LOSS',
            'В HTML исчезли изображения презентации. Результат не отмечен готовым.',
            stage='result', diagnostic='presentation_html_missing_graphics')
    only_original_assets = not observed.alternative and (
        not observed.images or len(hashes) > 0 and hashes <= expected['assetHashes'])
    if expected['paragraphs'] and only_original_assets:
        # A graphic copy of an original media asset cannot contain the missing
        # slide captions.  Rasterized whole slides intentionally remain unknown.
        missing = 0
        if len(text) * len(expected['paragraphs']) > MAX_MATCH_WORK:
            raise GuardError('ENGINE_OUTPUT_VALIDATION_INCOMPLETE',
                'Проверка текста презентации превысила бюджет анализа.', stage='result',
                diagnostic='presentation_html_text_match_budget')
        for paragraph, required in expected['paragraphs'].items():
            _cancel(cancel)
            actual = sum(1 for _ in re.finditer(r'(?<!\w)' + re.escape(paragraph) + r'(?!\w)', text))
            missing += max(0, required - actual)
        if missing:
            raise GuardError('ENGINE_OUTPUT_CONTENT_LOSS',
                'В HTML отсутствует исходный текст презентации. Результат не отмечен готовым.',
                stage='result', diagnostic='presentation_html_missing_paragraphs:' + str(missing))
    if observed.alternative or observed.images and not only_original_assets:
        return {**base, 'status': 'not_checked', 'reason': 'alternative_graphic_representation',
                'bodyTextInspected': True, 'imageReferences': len(observed.images)}
    return {**base, 'status': 'checked_subset', 'comparedScope': 'body_text_and_graphic_presence',
            'bodyTextInspected': True, 'imageReferences': len(observed.images),
            'visualLayoutCompared': False}
