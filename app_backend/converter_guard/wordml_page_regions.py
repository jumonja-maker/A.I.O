"""Keep Word 2003 XML page regions omitted by the native XHTML exporter.

Native document-body bytes stay unchanged except a visible companion link.
Headers/footers use a continuous, explicitly labelled companion representation;
this does not claim to reproduce paginated layout or editable Word structure.
"""
import base64
import binascii
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import quote, urlsplit
from xml.parsers import expat

from .errors import GuardError

W = 'http://schemas.microsoft.com/office/word/2003/wordml'
V = 'urn:schemas-microsoft-com:vml'
H = 'http://www.w3.org/1999/xhtml'
BLOCK = 65536
MAX_DEPTH = 256


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Сохранение колонтитулов отменено.', stage='result')


def _error(reason):
    raise GuardError('WORDML_PAGE_REGIONS_INVALID',
        'Не удалось сохранить все колонтитулы Word XML. Результат не подтверждён.',
        stage='result', diagnostic=reason)


def _tag(name):
    return tuple(name.split('}', 1)) if '}' in name else ('', name)


def _parser(*, native_html=False):
    parser = expat.ParserCreate(namespace_separator='}')
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    def decline(*_): _error('xml_entities_or_external_resources')
    def doctype(name, system, public, internal):
        # The known native XHTML export has this inert external declaration.
        # Expat never loads it, and entity declarations remain prohibited.
        if (not native_html or internal or name != 'html' or
                public != '-//W3C//DTD XHTML 1.1 plus MathML 2.0//EN' or
                system != 'http://www.w3.org/Math/DTD/mathml2/xhtml-math11-f.dtd'):
            _error('xml_doctype')
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = decline
    parser.ExternalEntityRefHandler = decline
    return parser


def _feed(path, parser, cancel, digest=None, *, token_budget=1024*1024):
    tag_size, quote_char, prefix, mode, tail = 0, None, b'', 'tag', b''
    try:
        with Path(path).open('rb') as stream:
            while block := stream.read(BLOCK):
                _check(cancel)
                if digest is not None: digest.update(block)
                # Expat retains an unfinished attribute token. Bound that
                # token before feeding it, while arbitrarily long text and
                # embedded base64 contents remain streamed in small chunks.
                for byte in block:
                    if not tag_size:
                        if byte == 60:
                            tag_size, quote_char, prefix, mode, tail = 1, None, b'<', 'tag', b''
                        continue
                    if mode == 'cdata':
                        tail = (tail + bytes((byte,)))[-3:]
                        if tail == b']]>': tag_size = 0
                        continue
                    tag_size += 1
                    if tag_size > token_budget: _error('xml_token_budget')
                    if len(prefix) < 9:
                        prefix += bytes((byte,))
                        if prefix == b'<!--': mode, tail = 'comment', b''
                        elif prefix == b'<![CDATA[': mode, tail = 'cdata', b''
                    if mode == 'comment':
                        tail = (tail + bytes((byte,)))[-3:]
                        if tail == b'-->': tag_size = 0
                        continue
                    if mode == 'cdata': continue
                    if quote_char is not None:
                        if byte == quote_char: quote_char = None
                    elif byte in (34, 39): quote_char = byte
                    elif byte == 62: tag_size = 0
                parser.Parse(block, False)
        parser.Parse(b'', True)
    except expat.ExpatError as error:
        _error('xml_parse:' + str(error.code))


def _token(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _image_kind(path):
    with path.open('rb') as stream: prefix = stream.read(32)
    if prefix.startswith(b'\x89PNG\r\n\x1a\n'): return 'png', 'image/png', True
    if prefix.startswith(b'\xff\xd8\xff'): return 'jpg', 'image/jpeg', True
    if prefix.startswith((b'GIF87a', b'GIF89a')): return 'gif', 'image/gif', True
    if prefix.startswith(b'BM'): return 'bmp', 'image/bmp', True
    if prefix[:4] == b'RIFF' and prefix[8:12] == b'WEBP': return 'webp', 'image/webp', True
    # Preserve native TIFF/vector/unknown bytes as explicit downloads: browser
    # support is not guaranteed and active SVG must never be embedded blindly.
    return 'bin', 'application/octet-stream', False


def _images(source, folder, cancel):
    images, active = {}, None
    parser = _parser()
    depth = 0
    digest = hashlib.sha256()
    def start(name, attrs):
        nonlocal active, depth
        depth += 1
        if depth > MAX_DEPTH: _error('xml_depth')
        if depth == 1 and _tag(name) != (W, 'wordDocument'): _error('not_word2003_xml')
        if _tag(name) == (W, 'binData'):
            identity = attrs.get(W + '}name')
            if not identity or len(identity) > 4096 or len(images) >= 16384 or identity in images or active is not None:
                _error('duplicate_or_missing_image_identity')
            path = folder / ('binary-%06d' % (len(images) + 1))
            active = {'identity': identity, 'path': path, 'stream': path.open('xb'),
                      'tail': '', 'digest': hashlib.sha256(), 'bytes': 0, 'depth': depth, 'ended': False}
    def text(value):
        if active is None: return
        # Base64 whitespace is insignificant. At most a small parser chunk and
        # three trailing characters are retained, never the complete picture.
        clean = ''.join(value.split())
        if active['ended'] and clean: _error('embedded_image_data_after_padding')
        value = active['tail'] + clean
        length = len(value) - len(value) % 4
        if length:
            try: data = base64.b64decode(value[:length], validate=True)
            except (ValueError, binascii.Error): _error('embedded_image_base64')
            active['stream'].write(data);active['digest'].update(data);active['bytes'] += len(data)
            active['ended'] = '=' in value[:length]
        active['tail'] = value[length:]
    def end(name):
        nonlocal active, depth
        if active is not None and active['depth'] == depth:
            if _tag(name) != (W, 'binData') or active['tail'] or not active['bytes']:
                _error('embedded_image_incomplete')
            active['stream'].close()
            images[active['identity']] = {'path': active['path'], 'sha256': active['digest'].hexdigest(),
                                          'bytes': active['bytes']}
            active = None
        depth -= 1
    parser.StartElementHandler, parser.EndElementHandler, parser.CharacterDataHandler = start, end, text
    try:
        _feed(source, parser, cancel, digest)
    finally:
        if active is not None: active['stream'].close()
    return images, digest.hexdigest()


def _regions(source, images, staged, companion_name, asset_name, cancel):
    parser = _parser()
    stack, records, used = [], [], {}
    link_count, link_digest = 0, hashlib.sha256()
    link_manifest = staged/'links.jsonl'
    link_output = link_manifest.open('x', encoding='utf-8', newline='\n')
    region = None
    source_section = 0
    in_text = 0
    def start(name, attrs):
        nonlocal region, source_section, in_text, link_count
        tag = _tag(name);stack.append(tag)
        if len(stack) > MAX_DEPTH: _error('xml_depth')
        if tag == (W, 'sectPr'): source_section += 1
        if tag in ((W, 'hdr'), (W, 'ftr')):
            if region is not None or len(records) >= 65536: _error('nested_or_excessive_regions')
            kind = 'header' if tag[1] == 'hdr' else 'footer'
            variant = attrs.get(W + '}type', 'default')
            if len(variant) > 128: _error('region_variant_budget')
            path = staged / ('region-%06d.html' % (len(records) + 1))
            region = {'kind': kind, 'variant': variant, 'section': source_section,
                      'depth': len(stack), 'path': path, 'stream': path.open('x', encoding='utf-8'),
                      'textDigest': hashlib.sha256(), 'textCharacters': 0, 'links': 0, 'images': 0,
                      'paragraphs': 0, 'tables': 0, 'openTags': [], 'vectors': []}
            return
        if region is None: return
        out = region['stream']
        if tag == (W, 't'):
            in_text += 1
        elif tag == (W, 'p'):
            region['paragraphs'] += 1;out.write('<p>');region['openTags'].append((len(stack), 'p'))
        elif tag in ((W, 'tab'), (W, 'br'), (W, 'cr')):
            value = '\t' if tag[1] == 'tab' else '\n'
            out.write('&#9;' if tag[1] == 'tab' else '<br/>')
            region['textDigest'].update(value.encode());region['textCharacters'] += 1
        elif tag in ((W, 'tbl'), (W, 'tr'), (W, 'tc')):
            html_tag = {'tbl': 'table', 'tr': 'tr', 'tc': 'td'}[tag[1]]
            if html_tag == 'table': region['tables'] += 1
            out.write('<'+html_tag+'>');region['openTags'].append((len(stack), html_tag))
        elif tag == (W, 'hlink'):
            href = attrs.get(W + '}dest', '')
            bookmark = attrs.get(W + '}bookmark')
            if not href and bookmark: href = '#' + bookmark
            if not href: _error('missing_page_link_target')
            if len(href) > 65536: _error('page_link_budget')
            parsed = urlsplit(href)
            region['links'] += 1
            record = {'kind': 'link', 'region': len(records) + 1, 'href': href,
                      'clickable': parsed.scheme.lower() in ('http', 'https', 'mailto')}
            encoded = json.dumps(record, ensure_ascii=True) + '\n'
            link_output.write(encoded);link_digest.update(encoded.encode());link_count += 1
            if parsed.scheme.lower() in ('http', 'https', 'mailto'):
                out.write('<a rel="noopener noreferrer" href="'+html.escape(href, quote=True)+'">')
                region['openTags'].append((len(stack), 'a'))
            else:
                # File/internal references are preserved visibly, without
                # guessing a changed local path or fetching an external file.
                out.write('<span data-original-href="'+html.escape(href, quote=True)+'">')
                region['openTags'].append((len(stack), 'span'))
                out.write('<small>[ссылка: '+html.escape(href)+']</small> ')
        elif tag[0] == V and (tag[1] == 'imagedata' or 'src' in attrs):
            identity = attrs.get('src')
            if identity not in images: _error('page_image_not_embedded')
            item = images[identity]
            if identity not in used:
                extension, mime, preview = _image_kind(item['path'])
                if preview:
                    from .media_fidelity import inspect_media
                    typed = item['path'].with_suffix('.' + extension)
                    item['path'].rename(typed);item['path'] = typed
                    try:
                        inspect_media(typed, cancel=cancel)
                    except GuardError as error:
                        if error.code == 'CANCELLED': raise
                        _error('embedded_page_image_invalid:' + error.code)
                filename = 'image-%06d.%s' % (len(used) + 1, extension)
                used[identity] = {**item, 'sourceId': identity, 'file': filename,
                                  'mime': mime, 'previewAvailable': preview}
            item = used[identity];region['images'] += 1
            reference = quote(asset_name, safe='')+'/'+quote(item['file'], safe='')
            if item['previewAvailable']:
                out.write('<img alt="Изображение колонтитула" src="'+reference+'"/>')
            else:
                out.write('<a download="'+html.escape(item['file'], quote=True)+'" href="'+reference+
                          '">Исходный ресурс колонтитула (без предпросмотра)</a>')
        elif tag[0] == W and tag[1] in ('object', 'control', 'subDoc', 'altChunk'):
            _error('unsupported_page_region_object')
        elif (tag[0] in ('http://www.w3.org/1998/Math/MathML', 'http://www.w3.org/2000/svg', H,
                         'http://schemas.openxmlformats.org/officeDocument/2006/math')
              or tag[0].startswith('http://schemas.openxmlformats.org/drawingml/')):
            _error('page_region_requires_native_math_or_graphics_rendering')
        elif tag[0] == V and tag[1] in ('shape', 'rect', 'oval', 'line', 'polyline', 'group'):
            region['vectors'].append((len(stack), region['textCharacters'], region['images']))
    def text(value):
        if region is not None and in_text:
            region['stream'].write(html.escape(value))
            region['textDigest'].update(value.encode('utf-8'))
            region['textCharacters'] += len(value)
    def end(name):
        nonlocal region, in_text
        tag = stack[-1]
        if region is not None:
            if tag == (W, 't'): in_text -= 1
            if region['vectors'] and region['vectors'][-1][0] == len(stack):
                _, text_before, images_before = region['vectors'].pop()
                if text_before == region['textCharacters'] and images_before == region['images']:
                    _error('page_vector_shape_requires_native_rendering')
            while region['openTags'] and region['openTags'][-1][0] == len(stack):
                region['stream'].write('</'+region['openTags'].pop()[1]+'>')
            if region['depth'] == len(stack):
                region['stream'].close()
                records.append({key: region[key] for key in ('kind', 'variant', 'section', 'path',
                    'textCharacters', 'links', 'images', 'paragraphs', 'tables')})
                records[-1]['textSha256'] = region['textDigest'].hexdigest()
                region = None
        stack.pop()
    parser.StartElementHandler, parser.EndElementHandler, parser.CharacterDataHandler = start, end, text
    try:
        _feed(source, parser, cancel)
    finally:
        if region is not None: region['stream'].close()
        link_output.close()
    return records, used, {'count': link_count, 'sha256': link_digest.hexdigest(), 'path': link_manifest}


def _body_close(primary, cancel):
    parser = _parser(native_html=True)
    closing, count = None, 0
    def start(name, attrs):
        nonlocal count
        if _tag(name) == (H, 'body'): count += 1
    def end(name):
        nonlocal closing
        if _tag(name) == (H, 'body'): closing = parser.CurrentByteIndex
    parser.StartElementHandler, parser.EndElementHandler = start, end
    _feed(primary, parser, cancel, token_budget=32*1024*1024)
    if count != 1 or closing is None: _error('native_xhtml_body_missing_or_duplicate')
    return closing


def _copy(reader, writer, cancel, length=None):
    while length is None or length:
        _check(cancel)
        block = reader.read(BLOCK if length is None else min(BLOCK, length))
        if not block:
            if length: _error('file_changed_during_copy')
            break
        writer.write(block)
        if length is not None: length -= len(block)


def restore_wordml_page_regions(source, paths, target, *, cancel):
    """Return original/new result paths plus exact region-preservation evidence."""
    source = Path(source)
    if target not in ('html', 'htm', 'xhtml'): return paths, None
    candidates = [Path(p) for p in paths if Path(p).suffix.lower() in ('.html', '.htm', '.xhtml')
                  and Path(p).stem == source.stem]
    if len(candidates) != 1: _error('native_primary_ambiguous')
    primary = candidates[0]
    if primary.is_symlink() or source.is_symlink(): _error('unexpected_symbolic_link')
    before = _token(source.stat())
    companion = primary.with_name(primary.stem + '.aio-page-regions.html')
    manifest = primary.with_name(primary.stem + '.aio-page-regions.jsonl')
    assets = primary.with_name(primary.stem + '.aio-page-regions.assets')
    if companion.exists() or assets.exists() or manifest.exists(): _error('page_region_output_collision')
    created, rewritten = [], False
    with tempfile.TemporaryDirectory(prefix='aio-wordml-regions-', dir=primary.parent.parent) as temporary:
        staging = Path(temporary)
        images, source_hash = _images(source, staging, cancel)
        regions, used, links = _regions(source, images, staging, companion.name, assets.name, cancel)
        if _token(source.stat()) != before: _error('source_changed_during_region_read')
        if not regions: return paths, None
        closing = _body_close(primary, cancel)
        primary_token = _token(primary.stat())
        primary_hash = hashlib.sha256()
        with primary.open('rb') as stream:
            while block := stream.read(BLOCK): _check(cancel);primary_hash.update(block)
        try:
            with companion.open('x', encoding='utf-8', newline='\n') as out:
                created.append(companion)
                out.write('<!DOCTYPE html><html xmlns="'+H+'"><head><meta charset="utf-8"/>'
                    '<title>Колонтитулы Word XML</title><style>body{font:16px sans-serif;margin:2rem;line-height:1.5}'
                    'section{border-top:1px solid #999;padding:1rem 0}p{white-space:pre-wrap}'
                    'img{max-width:100%;height:auto}td{border:1px solid #bbb;padding:.3rem}</style></head><body>'
                    '<h1>Колонтитулы Word XML</h1><p>Текст, ссылки и встроенные изображения сохранены по разделам. '
                    'Повторение на страницах и исходная геометрия колонтитулов здесь не воспроизводятся.</p>')
                for index, region in enumerate(regions, 1):
                    _check(cancel)
                    label = 'Верхний колонтитул' if region['kind'] == 'header' else 'Нижний колонтитул'
                    out.write('<section id="region-%04d"><h2>Раздел %d · %s · %s</h2>' %
                        (index, region['section'], label, html.escape(region['variant'])))
                    with region['path'].open(encoding='utf-8') as fragment:
                        while text := fragment.read(BLOCK): _check(cancel);out.write(text)
                    out.write('</section>')
                out.write('</body></html>')
            if used:
                assets.mkdir();created.append(assets)
                for item in used.values():
                    destination = assets/item['file']
                    with item['path'].open('rb') as reader, destination.open('xb') as writer:
                        _copy(reader, writer, cancel)
                    created.append(destination)
            with manifest.open('x', encoding='utf-8', newline='\n') as out:
                created.append(manifest)
                out.write(json.dumps({'kind': 'source', 'schemaVersion': 1,
                    'sourceSha256': source_hash, 'regionCount': len(regions),
                    'layout': 'continuous_sections_not_paginated'}, ensure_ascii=True) + '\n')
                for index, region in enumerate(regions, 1):
                    out.write(json.dumps({'recordType': 'region', 'index': index,
                        **{key: value for key, value in region.items() if key != 'path'}}, ensure_ascii=True) + '\n')
                with links['path'].open(encoding='utf-8') as link_input:
                    while block := link_input.read(BLOCK): _check(cancel);out.write(block)
                for item in used.values():
                    out.write(json.dumps({'kind': 'image', **{key: value for key, value in item.items() if key != 'path'}},
                                         ensure_ascii=True) + '\n')
            link = ('<div xmlns="'+H+'" id="aio-wordml-page-regions-link" role="note">'
                    '<a href="'+quote(companion.name, safe='')+'">Колонтитулы исходного Word XML '
                    '(текст, ссылки и изображения)</a></div>').encode('utf-8')
            modified = staging/'primary-with-region-link'
            with primary.open('rb') as reader, modified.open('xb') as writer:
                _copy(reader, writer, cancel, closing);writer.write(link);_copy(reader, writer, cancel)
            modified_hash = hashlib.sha256()
            with modified.open('rb') as stream:
                while block := stream.read(BLOCK): _check(cancel);modified_hash.update(block)
            _check(cancel)
            if _token(source.stat()) != before: _error('source_changed_during_region_write')
            if _token(primary.stat()) != primary_token: _error('native_primary_changed_during_region_write')
            os.replace(modified, primary);rewritten = True
            result_paths = list(paths) + [companion, manifest] + [assets/item['file'] for item in used.values()]
            return result_paths, {'check': 'wordml_page_regions', 'status': 'preserved_in_companion',
                'sourceSha256': source_hash, 'nativePrimaryBeforeLinkSha256': primary_hash.hexdigest(),
                'nativePrimaryAfterLinkSha256': modified_hash.hexdigest(),
                'nativeBodyPreservedExceptCompanionLink': True, 'nativePrimaryChange': 'visible_companion_link',
                'companion': companion.name, 'manifest': manifest.name,
                'regions': [{key: value for key, value in region.items() if key != 'path'} for region in regions[:32]],
                'regionPreviewTruncated': len(regions) > 32, 'regionCount': len(regions),
                'linkCount': links['count'], 'linkManifestSha256': links['sha256'], 'imageCount': len(used),
                'imagePreviewUnavailable': sum(not item['previewAvailable'] for item in used.values()),
                'layout': 'continuous_sections_not_paginated', 'qualityCompared': False}
        except BaseException:
            if not rewritten:
                for path in reversed(created):
                    if path.is_dir(): shutil.rmtree(path, ignore_errors=True)
                    else: path.unlink(missing_ok=True)
            raise
