"""Prepare an OPF-ordered reflowable EPUB for LibreOffice's HTML importer.

This input adapter uses the installed office engine for all document rendering
and conversion. Chapters are read with a streaming XML parser, local image
bytes are embedded before import, and unresolved/active content fails visibly.
"""
import base64
from dataclasses import dataclass
import hashlib
import html
from pathlib import Path
import posixpath
import re
import shutil
import stat
from urllib.parse import quote, unquote, urlsplit
import xml.etree.ElementTree as ET
import xml.sax
from xml.sax.handler import ContentHandler, feature_external_ges, feature_external_pes
import zipfile

from .errors import GuardError
from .html_resources import resource_path

OPF = '{http://www.idpf.org/2007/opf}'
OCF = '{urn:oasis:names:tc:opendocument:xmlns:container}'
DC = '{http://purl.org/dc/elements/1.1/}'
TEXT = frozenset(('application/xhtml+xml', 'text/html'))
WRITER = 'com.sun.star.text.TextDocument'
WEB = 'com.sun.star.text.WebDocument'
IMAGE_TYPES = {'image/png': 'png', 'image/jpeg': 'jpeg', 'image/gif': 'gif',
               'image/webp': 'webp', 'image/bmp': 'bmp'}
# Bound declarations and stylesheet parsing, not total book text/media bytes.
MAX_PACKAGE_XML = 8 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 100000


def _error(reason, code='EPUB_PACKAGE_INVALID'):
    raise GuardError(code, 'Не удалось полностью подготовить EPUB для LibreOffice.',
                     stage='preflight', diagnostic=reason)


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Подготовка EPUB отменена.', stage='preflight')


def _member(value):
    try:
        result = resource_path(value)
    except GuardError:
        _error('unsafe_package_path')
    if result != value:
        _error('noncanonical_package_path')
    return result


def _resolve(base, value, entries, *, hyperlink=False):
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if hyperlink and parsed.scheme.lower() in ('http', 'https', 'mailto'):
            return None, value
        _error('external_resource', 'EPUB_EXTERNAL_RESOURCE_UNSUPPORTED')
    raw = unquote(parsed.path)
    if '\\' in raw or raw.startswith('/') or parsed.query:
        _error('unsafe_resource_reference')
    target = posixpath.normpath(posixpath.join(posixpath.dirname(base), raw)) if raw else base
    _member(target)
    if target not in entries:
        _error('missing_package_resource')
    return target, unquote(parsed.fragment)


class _XMLReader:
    """Reject entities/external DTDs before XML can expand them."""
    def __init__(self, stream, cancel=None, limit=None):
        self.stream, self.cancel, self.limit = stream, cancel, limit
        self.used, self.tail = 0, b''

    def read(self, size=-1):
        _cancel(self.cancel)
        block = self.stream.read(min(size, 65536) if size >= 0 else 65536)
        self.used += len(block)
        if self.limit is not None and self.used > self.limit:
            _error('package_metadata_complexity', 'EPUB_PACKAGE_COMPLEXITY')
        text = (self.tail + block).replace(b'\0', b'').lower()
        if b'<!entity' in text:
            _error('xml_entity_declaration')
        # The inert HTML5 <!DOCTYPE html> is common in EPUB XHTML.
        if re.search(br'<!doctype\s+[^>]*(?:\bsystem\b|\bpublic\b)', text):
            _error('external_xml_doctype')
        self.tail = text[-1024:]
        return block

    def close(self):
        pass  # ZipFile owns the underlying stream.


def _xml(archive, name, cancel):
    with archive.open(name) as stream:
        try:
            return ET.parse(_XMLReader(stream, cancel, MAX_PACKAGE_XML)).getroot()
        except ET.ParseError:
            _error('invalid_package_xml')


def _anchor(chapter, fragment=''):
    return 'epub-' + hashlib.sha256(chapter.encode()).hexdigest()[:16] + ('-' + fragment if fragment else '')


def _data_uri(archive, target, kind, cancel):
    if kind not in IMAGE_TYPES:
        _error('unhandled_css_image_type', 'EPUB_RESOURCE_TYPE_UNSUPPORTED')
    parts = ['data:' + kind + ';base64,']
    with archive.open(target) as stream:
        while block := stream.read(3 * 16384):
            _cancel(cancel); parts.append(base64.b64encode(block).decode('ascii'))
    return ''.join(parts)


def _css(text, base, entries, references, *, prefix='', archive=None, media_types=None, cancel=None):
    # Escapes and split identifiers must not hide resource loads from validation.
    scan = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    if ('\\' in scan or '</style' in scan.lower() or
            re.search(r'(?i)expression\s*\(|image-set\s*\(|-moz-binding|behavior\s*:', scan)):
        _error('active_or_escaped_css', 'EPUB_CSS_UNSUPPORTED')
    def reference(match):
        value = next((v for v in match.groups() if v is not None), '').strip()
        target, fragment = _resolve(base, value, entries)
        references.add(target)
        if archive is not None and (media_types or {}).get(target, '').startswith('image/'):
            return 'url("' + _data_uri(archive, target, media_types[target], cancel) + '")'
        relative = prefix + posixpath.relpath(target, posixpath.dirname(base) or '.')
        return 'url("' + quote(relative, safe='/.-_~') + ('#' + quote(fragment) if fragment else '') + '")'
    result = re.sub(r'(?i)url\(\s*(?:"([^"\r\n]*)"|\'([^\'\r\n]*)\'|([^\)\s]*))\s*\)', reference, scan)
    def imported(match):
        target, fragment = _resolve(base, match[2], entries)
        references.add(target)
        relative = prefix + posixpath.relpath(target, posixpath.dirname(base) or '.')
        return '@import ' + match[1] + quote(relative, safe='/.-_~') + match[1]
    result = re.sub(r'(?i)@import\s*(["\'])([^"\']+)\1', imported, result)
    if len(re.findall(r'(?i)\burl\s*\(', scan)) != len(re.findall(r'(?i)\burl\(', result)):
        _error('css_resource_syntax', 'EPUB_CSS_UNSUPPORTED')
    for token in re.finditer(r'(?i)@import\b', result):
        if not re.match(r'(?i)\s*(?:url\(|["\'])', result[token.end():]):
            _error('css_import_syntax', 'EPUB_CSS_UNSUPPORTED')
    return result


class _Chapter(ContentHandler):
    VOID = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'wbr'))
    ACTIVE = frozenset(('script', 'iframe', 'object', 'embed', 'form', 'audio', 'video', 'canvas', 'applet'))
    BLOCKS = frozenset(('section', 'article', 'nav', 'header', 'footer', 'main', 'aside', 'figure', 'figcaption'))

    def __init__(self, out, head, chapter, entries, spine, references, cancel, archive, media_types):
        super().__init__()
        self.out, self.head, self.chapter = out, head, chapter
        self.entries, self.spine, self.references, self.cancel = entries, spine, references, cancel
        self.archive, self.media_types = archive, media_types
        self.in_body = self.in_head = False
        self.body_count = self.depth = 0
        self.style = None
        self.namespaces = {}

    def _style(self, value):
        prefix = '_epub-assets/' + posixpath.dirname(self.chapter) + '/'
        return _css(value, self.chapter, self.entries, self.references, prefix=prefix,
                    archive=self.archive, media_types=self.media_types, cancel=self.cancel)

    def startElement(self, name, attrs):
        _cancel(self.cancel)
        self.depth += 1
        if self.depth > 256:
            _error('chapter_xml_depth', 'EPUB_PACKAGE_COMPLEXITY')
        tag = name.rsplit(':', 1)[-1].lower()
        attributes = dict(attrs)
        for key, value in attributes.items():
            if key.startswith('xmlns:'): self.namespaces[key.split(':', 1)[1]] = value
        if ':' in name and self.namespaces.get(name.split(':', 1)[0]) != 'http://www.w3.org/1999/xhtml':
            _error('foreign_chapter_namespace', 'EPUB_CHAPTER_FEATURE_UNSUPPORTED')
        if tag in self.ACTIVE or any(k.lower().startswith('on') for k in attributes):
            _error('active_chapter_content', 'EPUB_ACTIVE_CONTENT_UNSUPPORTED')
        if tag == 'html':
            if attributes.get('xmlns', 'http://www.w3.org/1999/xhtml') != 'http://www.w3.org/1999/xhtml':
                _error('chapter_namespace')
            return
        if tag == 'head': self.in_head = True; return
        if tag == 'body':
            self.in_body = True; self.body_count += 1
            self.out.write('<div id="' + _anchor(self.chapter) + '"')
            for key in ('lang', 'dir', 'class', 'style', 'bgcolor', 'text'):
                if key in attributes:
                    value = self._style(attributes[key]) if key == 'style' else attributes[key]
                    self.out.write(' ' + key + '="' + html.escape(value, quote=True) + '"')
            self.out.write('>\n')
            if attributes.get('id'):
                self.out.write('<a id="' + html.escape(_anchor(self.chapter, attributes['id']), quote=True) + '"></a>')
            if attributes.get('background'):
                _error('body_background_requires_native_support', 'EPUB_CHAPTER_FEATURE_UNSUPPORTED')
            return
        if self.in_head:
            if tag == 'link' and attributes.get('rel', '').lower() == 'stylesheet':
                target, fragment = _resolve(self.chapter, attributes.get('href', ''), self.entries)
                if self.media_types.get(target) != 'text/css': _error('stylesheet_media_type_mismatch')
                self.references.add(target)
                self.head.write('<link rel="stylesheet" href="_epub-assets/' + quote(target, safe='/.-_~') + '">\n')
            elif tag == 'style': self.style = []
            elif tag == 'base' or tag == 'meta' and attributes.get('http-equiv', '').lower() == 'refresh':
                _error('active_head_content', 'EPUB_ACTIVE_CONTENT_UNSUPPORTED')
            return
        if not self.in_body: return
        if tag in ('svg', 'math', 'picture') or 'srcset' in attributes or 'xml:base' in attributes:
            _error('chapter_feature_requires_native_support', 'EPUB_CHAPTER_FEATURE_UNSUPPORTED')
        output_tag = 'div' if tag in self.BLOCKS else tag
        self.out.write('<' + output_tag)
        for key, value in attributes.items():
            if key.startswith('xmlns') or key.startswith('epub:'): continue
            if ':' in key and key not in ('xml:lang', 'xml:id'):
                _error('foreign_chapter_attribute', 'EPUB_CHAPTER_FEATURE_UNSUPPORTED')
            key = 'id' if key == 'xml:id' else key
            key = 'lang' if key == 'xml:lang' else key
            if key in ('id', 'name'): value = _anchor(self.chapter, value)
            elif key in ('href', 'src', 'poster', 'background', 'data', 'xlink:href'):
                target, fragment = _resolve(self.chapter, value, self.entries, hyperlink=tag == 'a' and key == 'href')
                if target is None: pass  # Web/mail hyperlink, never fetched.
                elif tag == 'a' and key == 'href':
                    if target not in self.spine:
                        _error('hyperlink_to_nonspine_document', 'EPUB_ATTACHMENT_LINK_UNSUPPORTED')
                    value = '#' + _anchor(target, fragment)
                else:
                    self.references.add(target)
                    if tag == 'img' and key == 'src':
                        kind = self.media_types.get(target, '')
                        if kind not in IMAGE_TYPES: _error('unhandled_image_type', 'EPUB_RESOURCE_TYPE_UNSUPPORTED')
                        # Native HTML otherwise stores dangling file-linked
                        # images in DOCX/ODT. Data URIs force embedded bytes.
                        self.out.write(' ' + key + '="data:' + kind + ';base64,')
                        with self.archive.open(target) as stream:
                            while block := stream.read(3 * 16384):
                                _cancel(self.cancel); self.out.write(base64.b64encode(block).decode('ascii'))
                        self.out.write('"'); continue
                    value = '_epub-assets/' + quote(target, safe='/.-_~') + ('#' + quote(fragment) if fragment else '')
            elif key == 'style': value = self._style(value)
            elif key in ('action', 'formaction', 'srcdoc'):
                _error('active_chapter_attribute', 'EPUB_ACTIVE_CONTENT_UNSUPPORTED')
            self.out.write(' ' + key + '="' + html.escape(value, quote=True) + '"')
        self.out.write('>')

    def endElement(self, name):
        tag = name.rsplit(':', 1)[-1].lower()
        if tag == 'body': self.out.write('</div>\n'); self.in_body = False
        elif tag == 'head': self.in_head = False
        elif self.in_head and tag == 'style' and self.style is not None:
            self.head.write('<style>' + self._style(''.join(self.style)) + '</style>\n'); self.style = None
        elif self.in_body and tag not in self.VOID:
            self.out.write('</' + ('div' if tag in self.BLOCKS else tag) + '>')
        self.depth -= 1

    def characters(self, content):
        if self.style is not None: self.style.append(content)
        elif self.in_body: self.out.write(html.escape(content))

    def processingInstruction(self, target, data):
        _error('chapter_processing_instruction', 'EPUB_ACTIVE_CONTENT_UNSUPPORTED')


def _chapter(archive, chapter, out, head, entries, spine, references, cancel, media_types):
    parser = xml.sax.make_parser()
    parser.setFeature(feature_external_ges, False)
    try: parser.setFeature(feature_external_pes, False)
    except xml.sax.SAXNotSupportedException: pass
    handler = _Chapter(out, head, chapter, entries, spine, references, cancel, archive, media_types)
    parser.setContentHandler(handler)
    with archive.open(chapter) as stream:
        try: parser.parse(_XMLReader(stream, cancel))
        except xml.sax.SAXException: _error('invalid_chapter_xml')
    if handler.body_count != 1: _error('chapter_body_missing_or_duplicate')


@dataclass(frozen=True)
class PreparedEPUB:
    source: Path
    summary: dict


def prepare_epub(source, destination, *, cancel=None):
    """Make private HTML and path-free provenance without changing the EPUB."""
    source, destination = Path(source), Path(destination)
    _cancel(cancel)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_PACKAGE_ENTRIES: _error('package_entry_complexity', 'EPUB_PACKAGE_COMPLEXITY')
            entries, folded = {}, set()
            for info in infos:
                _cancel(cancel)
                if info.is_dir(): continue
                name = _member(info.filename)
                if name.casefold() in folded or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                    _error('duplicate_link_or_encrypted_entry')
                folded.add(name.casefold()); entries[name] = info
            if ('mimetype' not in entries or entries['mimetype'].file_size > 128
                    or archive.read('mimetype') != b'application/epub+zip'):
                _error('epub_mimetype')
            if 'META-INF/encryption.xml' in entries: _error('encrypted_epub', 'EPUB_ENCRYPTION_UNSUPPORTED')
            container = _xml(archive, 'META-INF/container.xml', cancel)
            rootfiles = container.findall(OCF + 'rootfiles/' + OCF + 'rootfile')
            if container.tag != OCF + 'container' or len(rootfiles) != 1: _error('ambiguous_epub_rootfile')
            package_name = _member(rootfiles[0].get('full-path', ''))
            package = _xml(archive, package_name, cancel)
            if package.tag != OPF + 'package': _error('opf_root')
            metadata = package.find(OPF + 'metadata')
            if metadata is None: _error('opf_metadata_missing')
            if any(e.get('property') == 'rendition:layout' and (e.text or '').strip() == 'pre-paginated'
                   or e.get('name') == 'fixed-layout' and e.get('content', '').lower() == 'true' for e in metadata):
                _error('fixed_layout_epub', 'EPUB_FIXED_LAYOUT_UNSUPPORTED')
            manifest, spine = package.find(OPF + 'manifest'), package.find(OPF + 'spine')
            if manifest is None or spine is None: _error('opf_manifest_or_spine_missing')
            items, declared_paths = {}, set()
            for item in manifest:
                if item.tag != OPF + 'item': continue
                identity = item.get('id')
                if not identity or identity in items: _error('duplicate_manifest_id')
                name, fragment = _resolve(package_name, item.get('href', ''), entries)
                if name in declared_paths: _error('duplicate_manifest_path')
                declared_paths.add(name)
                if fragment or item.get('media-overlay') or 'scripted' in item.get('properties', '').split():
                    _error('active_or_overlay_manifest', 'EPUB_ACTIVE_CONTENT_UNSUPPORTED')
                items[identity] = (name, item.get('media-type', ''))
            chapters = []
            for reference in spine:
                if reference.tag != OPF + 'itemref': continue
                item = items.get(reference.get('idref'))
                if item is None or item[1] not in TEXT: _error('spine_resource_type')
                if item[0] in chapters: _error('duplicate_spine_item')
                if 'rendition:layout-pre-paginated' in reference.get('properties', '').split():
                    _error('fixed_layout_epub', 'EPUB_FIXED_LAYOUT_UNSUPPORTED')
                chapters.append(item[0])
            if not chapters: _error('empty_spine')
            title = metadata.findtext(DC + 'title') or source.stem
            body_path, head_path = destination / 'body.part', destination / 'head.part'
            references = set()
            media_types = {name: kind for name, kind in items.values()}
            with body_path.open('w', encoding='utf-8') as body, head_path.open('w', encoding='utf-8') as head:
                for chapter in chapters:
                    _chapter(archive, chapter, body, head, entries, set(chapters), references, cancel, media_types)
            processed = set()
            while references - processed:
                name = next(iter(references - processed)); processed.add(name)
                _cancel(cancel)
                kind = media_types.get(name, '')
                if kind not in set(IMAGE_TYPES) | {'text/css', 'font/otf', 'font/ttf', 'font/woff', 'font/woff2',
                        'application/vnd.ms-opentype', 'application/font-sfnt', 'application/font-woff'}:
                    _error('unhandled_resource_type', 'EPUB_RESOURCE_TYPE_UNSUPPORTED')
                target = destination / '_epub-assets' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as stream, target.open('xb') as output:
                    if kind == 'text/css':
                        chunks, content_size = [], 0
                        while block := stream.read(65536):
                            _cancel(cancel); chunks.append(block); content_size += len(block)
                            if content_size > MAX_PACKAGE_XML: _error('stylesheet_complexity', 'EPUB_PACKAGE_COMPLEXITY')
                        output.write(_css(b''.join(chunks).decode('utf-8-sig'), name, entries, references,
                            archive=archive, media_types=media_types, cancel=cancel).encode('utf-8'))
                    else:
                        while block := stream.read(65536):
                            _cancel(cancel); output.write(block)
                if kind in IMAGE_TYPES:
                    from .media_fidelity import inspect_media
                    try: image = inspect_media(target, cancel=cancel)
                    except GuardError as error:
                        if error.code == 'CANCELLED': raise
                        _error('invalid_embedded_image', 'EPUB_IMAGE_INVALID')
                    if not image or image['format'] != IMAGE_TYPES[kind]:
                        _error('image_media_type_mismatch', 'EPUB_IMAGE_INVALID')
                    if image.get('frames', 1) > 1 or image.get('pages', 1) > 1:
                        _error('animated_book_image', 'EPUB_ANIMATED_RESOURCE_UNSUPPORTED')
            result = destination / (source.stem + '.html')
            with result.open('w', encoding='utf-8') as output:
                output.write('<!DOCTYPE html><html><head><meta charset="utf-8"><title>' + html.escape(title) + '</title>\n')
                with head_path.open(encoding='utf-8') as stream: shutil.copyfileobj(stream, output, 65536)
                output.write('</head><body>\n')
                with body_path.open(encoding='utf-8') as stream: shutil.copyfileobj(stream, output, 65536)
                output.write('</body></html>')
            body_path.unlink(); head_path.unlink()
            return PreparedEPUB(result, {'kind': 'epub_spine_html', 'spineItems': len(chapters),
                'resources': len(processed), 'warnings': ['EPUB_REFLOWED_HTML_IMPORT'],
                'readingOrder': 'opf_spine', 'externalResources': False})
    except GuardError:
        shutil.rmtree(destination, ignore_errors=True); raise
    except (OSError, ValueError, KeyError, UnicodeError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        shutil.rmtree(destination, ignore_errors=True)
        _error('epub_container_unreadable')
