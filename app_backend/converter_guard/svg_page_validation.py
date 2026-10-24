"""Streaming structural checks for native SVG page exports.

No SVG is rendered here. Checks establish complete XML, a usable page canvas,
actual drawing/text references, and self-contained visual resources. They do
not claim pixel, layout, font, or semantic equivalence with the source model.
"""
import base64
import binascii
import hashlib
import math
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
from xml.parsers import expat

from .errors import GuardError

SVG = 'http://www.w3.org/2000/svg'
XLINK = 'http://www.w3.org/1999/xlink'
MAX_DEPTH = 256
MAX_REFERENCES = 65536
BLOCK = 65536
_INERT = {'defs', 'metadata', 'title', 'desc', 'clipPath', 'mask', 'pattern',
          'marker', 'symbol', 'linearGradient', 'radialGradient', 'filter'}
_ACTIVE = {'script', 'foreignobject', 'iframe', 'object', 'embed', 'audio',
           'video', 'animate', 'animatetransform', 'animatemotion', 'set', 'discard'}
_NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
_LENGTH = re.compile(r'^\s*(' + _NUMBER + r')\s*(px|mm|cm|in|pt|pc|q|%)?\s*$', re.I)
_URL = re.compile(r'''(?is)\burl\(\s*(?:"([^"\r\n]*)"|'([^'\r\n]*)'|([^\)\s]*))\s*\)''')
_NATIVE_DOCTYPE = re.compile(br'''<!DOCTYPE\s+svg\s+PUBLIC\s+(["'])-//W3C//DTD SVG 1\.1//EN\1\s+(["'])http://www\.w3\.org/Graphics/SVG/1\.1/DTD/svg11\.dtd\2\s*>''')


def _fail(reason, code='NATIVE_SVG_OUTPUT_INVALID'):
    raise GuardError(code, 'Не удалось подтвердить целостность SVG-страницы LibreOffice.',
                     stage='result', diagnostic=reason)


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED', 'Проверка SVG-страницы отменена.', stage='result')


def _parts(name):
    return tuple(name.split('}', 1)) if '}' in name else ('', name)


def _length(value):
    match = _LENGTH.fullmatch(value or '')
    if match is None:
        return None
    number = float(match[1])
    return (number, (match[2] or '').lower()) if math.isfinite(number) and number > 0 else None


def _geometry(attrs):
    box = None
    if 'viewBox' in attrs:
        values = re.split(r'[\s,]+', attrs['viewBox'].strip())
        try:
            box = [float(value) for value in values]
        except ValueError:
            _fail('svg_viewbox')
        if len(box) != 4 or not all(math.isfinite(v) for v in box) or min(box[2:]) <= 0:
            _fail('svg_viewbox')
    width, height = _length(attrs.get('width')), _length(attrs.get('height'))
    if 'width' in attrs and width is None or 'height' in attrs and height is None:
        _fail('svg_canvas')
    if width and width[1] == '%' or height and height[1] == '%':
        if box is None:
            _fail('relative_svg_canvas_without_viewbox')
    if box is None and (width is None or height is None):
        _fail('svg_canvas_missing')
    return {'width': width[0] if width else box[2], 'height': height[0] if height else box[3],
            'widthUnit': width[1] if width else 'viewBox',
            'heightUnit': height[1] if height else 'viewBox', 'viewBox': box,
            'widthCss': format(width[0] if width else box[2], '.15g') +
                        ((width[1] or 'px') if width else 'px'),
            'heightCss': format(height[0] if height else box[3], '.15g') +
                         ((height[1] or 'px') if height else 'px')}


def _data_image(value, cancel):
    header, separator, payload = value.partition(',')
    match = re.fullmatch(r'(?i)data:(image/(?:png|jpeg|gif|webp));base64', header)
    if not separator or not match:
        _fail('svg_data_image_type', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
    kind = match[1].lower()
    digest, size, first, pending, finished = hashlib.sha256(), 0, b'', '', False
    try:
        for offset in range(0, len(payload), BLOCK):
            _cancel(cancel)
            text = re.sub(r'\s+', '', payload[offset:offset + BLOCK])
            if finished and text:
                raise ValueError('data_after_padding')
            pending += text
            count = len(pending) // 4 * 4
            if not count:
                continue
            current, pending = pending[:count], pending[count:]
            decoded = base64.b64decode(current, validate=True)
            if '=' in current:
                finished = True
            digest.update(decoded); size += len(decoded)
            first = (first + decoded)[:16]
        if pending or not size:
            raise ValueError('truncated_or_empty')
    except (ValueError, binascii.Error, UnicodeError):
        _fail('svg_image_base64', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
    correct = ((kind == 'image/png' and first.startswith(b'\x89PNG\r\n\x1a\n')) or
               (kind == 'image/jpeg' and first.startswith(b'\xff\xd8\xff')) or
               (kind == 'image/gif' and first[:6] in (b'GIF87a', b'GIF89a')) or
               (kind == 'image/webp' and first[:4] == b'RIFF' and first[8:12] == b'WEBP'))
    if not correct:
        _fail('svg_image_signature', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
    return {'mediaType': kind, 'sha256': digest.hexdigest(), 'bytes': size}


def _own_visual(tag, attrs):
    if tag == 'path':
        return bool(re.search(r'[LlHhVvCcSsQqTtAaZz]', attrs.get('d', '')))
    if tag in ('rect', 'image'):
        return bool(_length(attrs.get('width')) and _length(attrs.get('height')))
    if tag == 'circle':
        return bool(_length(attrs.get('r')))
    if tag == 'ellipse':
        return bool(_length(attrs.get('rx')) and _length(attrs.get('ry')))
    if tag in ('polygon', 'polyline'):
        return len(re.findall(_NUMBER, attrs.get('points', ''))) >= 4
    if tag == 'line':
        try:
            return (float(attrs.get('x1', 0)), float(attrs.get('y1', 0))) != (
                float(attrs.get('x2', 0)), float(attrs.get('y2', 0)))
        except ValueError:
            return False
    return False


def inspect_svg_page(path, *, cancel, content_expected=True):
    """Validate a self-contained native page and return structural metadata."""
    path = Path(path)
    _cancel(cancel)
    parser = expat.ParserCreate(namespace_separator='}')
    stack, ids, references, active_uses, images = [], {}, set(), set(), []
    counts = {'vectorElements': 0, 'textElements': 0, 'imageElements': 0,
              'textCharacters': 0, 'externalHyperlinks': 0}
    geometry, root_seen, total_references = None, False, 0

    def remember(value, *, hyperlink=False):
        nonlocal total_references
        total_references += 1
        if total_references > MAX_REFERENCES:
            _fail('svg_reference_complexity')
        if value.startswith('#') and len(value) > 1:
            identity = unquote(value[1:])
            references.add(identity)
            return identity
        if value.lower().startswith('data:'):
            if hyperlink:
                _fail('svg_hyperlink_scheme', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
            images.append(_data_image(value, cancel))
            return None
        try:
            uri = urlsplit(value)
        except ValueError:
            _fail('svg_resource_uri', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
        if hyperlink and uri.scheme.lower() in ('http', 'https', 'mailto'):
            counts['externalHyperlinks'] += 1
            return None
        _fail('svg_external_visual_resource', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')

    def css(value):
        scan = re.sub(r'(?s)/\*.*?\*/', '', value)
        if ('\\' in scan or re.search(r'(?i)@import|expression\s*\(|image-set\s*\(|-moz-binding|behavior\s*:', scan)):
            _fail('svg_css_resource_syntax', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
        matches = list(_URL.finditer(scan))
        if len(matches) != len(re.findall(r'(?i)\burl\s*\(', scan)):
            _fail('svg_css_resource_syntax', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
        for match in matches:
            remember(next(value for value in match.groups() if value is not None).strip())

    def start(name, attrs):
        nonlocal geometry, root_seen
        _cancel(cancel)
        namespace, tag = _parts(name)
        local = {_parts(key)[1]: value for key, value in attrs.items()}
        if len(stack) >= MAX_DEPTH:
            _fail('svg_xml_depth')
        if not stack:
            if root_seen or namespace != SVG or tag != 'svg':
                _fail('svg_root')
            root_seen = True; geometry = _geometry(local)
        if tag.lower() in _ACTIVE or any(_parts(key)[1].lower().startswith('on') for key in attrs):
            _fail('svg_active_content', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
        if any(_parts(key) == ('http://www.w3.org/XML/1998/namespace', 'base') for key in attrs):
            _fail('svg_xml_base', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
        inherited_inert = stack[-1]['inert'] if stack else False
        if namespace != SVG and not inherited_inert:
            _fail('svg_foreign_content', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
        inert = inherited_inert or tag in _INERT
        style = local.get('style', '')
        hidden = (stack[-1]['hidden'] if stack else False) or bool(re.search(
            r'(?i)(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0*)?)\s*(?:;|$)', style))
        hidden |= local.get('display', '').lower() == 'none' or local.get('visibility', '').lower() == 'hidden'
        identity = local.get('id')
        if identity and identity in ids:
            _fail('svg_duplicate_id')
        if identity:
            if len(ids) >= MAX_REFERENCES:
                _fail('svg_id_complexity')
            ids[identity] = None
        own = not hidden and namespace == SVG and _own_visual(tag, local)
        frame = {'tag': tag, 'id': identity, 'inert': inert, 'hidden': hidden,
                 'concrete': own, 'refs': set(), 'style': [] if tag == 'style' else None,
                 'styleBytes': 0}
        stack.append(frame)
        if own and not inert:
            counts['imageElements' if tag == 'image' else 'vectorElements'] += 1
        found_image_href = False
        for key, value in attrs.items():
            attr_namespace, attr = _parts(key)
            if attr == 'href' and attr_namespace in ('', XLINK):
                target = remember(value.strip(), hyperlink=tag == 'a')
                found_image_href |= tag == 'image'
                if tag == 'image' and target is not None:
                    _fail('svg_image_fragment', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
                if tag == 'use':
                    if target is None:
                        _fail('svg_use_requires_fragment', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
                    frame['refs'].add(target)
                    if not inert and not hidden:
                        active_uses.add(target)
            elif attr == 'style' or re.search(r'(?i)\burl\s*\(', value):
                css(value)
        if tag == 'image' and not found_image_href:
            _fail('svg_image_resource_missing', 'NATIVE_SVG_RESOURCE_UNSUPPORTED')

    def characters(value):
        if not stack:
            return
        frame = stack[-1]
        if frame['style'] is not None:
            frame['styleBytes'] += len(value.encode('utf-8'))
            if frame['styleBytes'] > 8 * 1024 * 1024:
                _fail('svg_style_complexity')
            frame['style'].append(value)
        if (not frame['hidden'] and any(node['tag'] in ('text', 'tspan', 'textPath') for node in stack)
                and value.strip()):
            frame['concrete'] = True
            if not frame['inert']:
                counts['textCharacters'] += len(value.strip())
                counts['textElements'] += 1

    def end(name):
        frame = stack.pop()
        if frame['style'] is not None:
            css(''.join(frame['style']))
        if frame['id']:
            inert_container = frame['tag'] in _INERT - {'symbol'}
            ids[frame['id']] = {'concrete': frame['concrete'] and not inert_container,
                                'refs': set() if inert_container else frame['refs']}
        if stack and frame['tag'] not in _INERT:
            stack[-1]['concrete'] |= frame['concrete']
            if frame['id']:
                stack[-1]['refs'].add(frame['id'])
            else:
                stack[-1]['refs'].update(frame['refs'])

    def doctype(name, system, public, internal):
        if name != 'svg' or system or public or internal:
            _fail('svg_xml_doctype')

    def forbidden(*_):
        _fail('svg_xml_entity_or_instruction')

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = characters
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = parser.ExternalEntityRefHandler = forbidden
    parser.ProcessingInstructionHandler = forbidden
    try:
        with path.open('rb') as stream:
            while block := stream.read(BLOCK):
                _cancel(cancel); parser.Parse(block, False)
            parser.Parse(b'', True)
    except GuardError:
        raise
    except (expat.ExpatError, OSError, ValueError, OverflowError) as error:
        _fail('svg_xml_read:' + type(error).__name__)
    if not root_seen or stack or any(identity not in ids for identity in references):
        _fail('svg_unresolved_reference_or_root')
    use_content = False
    pending, seen = list(active_uses), set()
    while pending:
        _cancel(cancel)
        identity = pending.pop()
        if identity in seen:
            continue
        seen.add(identity)
        target = ids.get(identity)
        if target is None:
            _fail('svg_unresolved_use')
        if target['concrete']:
            use_content = True; break
        pending.extend(target['refs'] - seen)
    visual = counts['vectorElements'] + counts['imageElements'] + counts['textElements'] + int(use_content)
    if content_expected and not visual:
        _fail('svg_page_content_empty', 'ENGINE_OUTPUT_CONTENT_EMPTY')
    return {'check': 'native_svg_page', 'status': 'structural_content_present' if visual else 'blank_page',
            **geometry, **counts, 'resolvedUsesPresent': use_content,
            'embeddedImages': images, 'qualityCompared': False}


def sanitize_native_svg_doctype(source, destination, *, cancel):
    """Copy a native page, removing only LO's inert external SVG 1.1 DTD URL.

    Every drawing, text, style and embedded image byte stays identical. The
    standard declaration is removed so opening a packaged object cannot ask a
    browser to retrieve a DTD. All other declarations/entities/active resources
    are rejected by the same complete streaming inspection used for pages.
    """
    source, destination = Path(source), Path(destination)
    _cancel(cancel)
    if source.is_symlink() or not source.is_file() or source.resolve() == destination.resolve():
        _fail('native_svg_copy_path')
    raw_hash, copied_hash, removed, created = hashlib.sha256(), hashlib.sha256(), False, False
    try:
        with source.open('rb') as incoming, destination.open('xb') as outgoing:
            created = True
            before = os.fstat(incoming.fileno())
            prefix = incoming.read(BLOCK)
            raw_hash.update(prefix)
            root = re.search(br'<svg(?:\s|>)', prefix)
            declaration = _NATIVE_DOCTYPE.search(prefix)
            if declaration is not None and root is not None and declaration.end() <= root.start():
                prefix = prefix[:declaration.start()] + prefix[declaration.end():]
                removed = True
            outgoing.write(prefix); copied_hash.update(prefix)
            while block := incoming.read(BLOCK):
                _cancel(cancel)
                raw_hash.update(block); copied_hash.update(block); outgoing.write(block)
            after = os.fstat(incoming.fileno())
            if any(getattr(before, key) != getattr(after, key) for key in
                   ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
                _fail('native_svg_changed_during_copy')
        inspect_svg_page(destination, cancel=cancel, content_expected=False)
        return {'originalSha256': raw_hash.hexdigest(), 'packagedSha256': copied_hash.hexdigest(),
                'removedNativeDoctype': removed}
    except BaseException as error:
        if created:
            destination.unlink(missing_ok=True)
        if isinstance(error, GuardError):
            raise
        if isinstance(error, (OSError, ValueError)):
            _fail('native_svg_copy:' + type(error).__name__)
        raise
