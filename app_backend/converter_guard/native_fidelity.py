"""Export contracts run inside the existing loaded UNO model, not a new pass.

The worker uses the same source string as the unit tests, without importing the
application or extra packages into LibreOffice's embedded Python.
"""
from .preservation_contract import PRESERVATION_POLICY_SOURCE
from .calc_pages import CALC_PAGES_SOURCE

NATIVE_FIDELITY_SOURCE = PRESERVATION_POLICY_SOURCE + CALC_PAGES_SOURCE + r'''
import json
import re
import hashlib
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

class AioPresentationTextLoss(RuntimeError):
    pass

class AioPresentationUnverified(RuntimeError):
    pass

class AioWriterPagesUnverified(RuntimeError):
    pass


def aio_native_preservation_plan(model, services, target):
    """Read one count only for a contracted export from the already-open model."""
    scope = aio_preservation_scope(services, target)
    if scope is None:
        return None
    try:
        if scope[1] == 'sheets':
            count = model.getSheets().getCount()
        elif scope[2] == 'loaded_writer_layout':
            count = model.getCurrentController().PageCount
        else:
            count = model.getDrawPages().getCount()
    except Exception as error:
        raise AioPreservationIncomplete('native_unit_count_unavailable') from error
    return aio_plan_preservation(services, target, count)


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





class _AioXhtmlVisualScope(HTMLParser):
    """Remember original image references and graphical container attributes.

    Only used after native PPT -> XHTML has already demonstrably lost text.
    It cannot prove source/visual equivalence; it detects further loss caused
    by our recovery candidate and refuses to publish a changed visual subset.
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images, self.links = [], []
        self.divs = Counter()
        self.unsupported = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ('svg', 'canvas', 'object', 'embed', 'iframe',
                   'script', 'video', 'audio', 'picture', 'source',
                   'applet', 'foreignobject', 'svg:image'):
            self.unsupported = True
        if tag == 'img':
            value = dict(attrs).get('src')
            prefix = (value.split(',', 1)[0].lower()
                      if isinstance(value, str) and ',' in value else '')
            if prefix not in ('data:image/png;base64',
                              'data:image/jpeg;base64', 'data:image/gif;base64',
                              'data:image/webp;base64', 'data:image/bmp;base64'):
                self.unsupported = True
            self.images.append(hashlib.sha256(repr(attrs).encode('utf-8')).hexdigest())
        elif tag == 'div':
            self.divs[tuple(sorted(attrs))] += 1
        elif tag == 'link':
            self.links.append(tuple(sorted(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def aio_xhtml_visual_scope(path):
    parser = _AioXhtmlVisualScope()
    total = 0
    if Path(path).stat().st_size > 16 * 1024 * 1024:
        raise AioPresentationUnverified('ppt_xhtml_visual_budget')
    with Path(path).open('r', encoding='utf-8-sig', errors='strict') as stream:
        while True:
            block = stream.read(65536)
            if not block:
                break
            total += len(block)
            if total > 16 * 1024 * 1024:
                raise AioPresentationUnverified('ppt_xhtml_visual_budget')
            parser.feed(block)
    parser.close()
    return parser


def aio_try_ppt_xhtml_textboxes(model, target, export_options, expected):
    """Recover text while keeping preexisting XHTML images and shape containers.

    Old PPT may import text as mso-spt202 / rectangle / round-rectangle native
    CustomShapes. Mixed slides are eligible only when all other shapes are
    native image objects and the original XHTML already exported *all* of
    them as embedded images. Any loss/change in image refs, link refs or
    existing DIV attributes during retry fails closed. Unknown shape classes,
    media objects, external images and groups remain unsupported.
    """
    if not expected:
        return None
    boxes, graphic_objects, mixed = [], 0, False
    try:
        pages = model.getDrawPages()
        count = pages.getCount()
        if not 1 <= count <= 32:
            return None
        for index in range(count):
            page = pages.getByIndex(index)
            shapes = page.getCount()
            if shapes > 256 or len(boxes) + shapes > 256:
                return None
            for offset in range(shapes):
                original = page.getByIndex(offset)
                kind = original.getShapeType()
                if kind in ('com.sun.star.drawing.TextShape',
                            'com.sun.star.presentation.TitleTextShape',
                            'com.sun.star.presentation.OutlinerShape'):
                    continue
                if kind == 'com.sun.star.drawing.GraphicObjectShape':
                    graphic_objects += 1
                    mixed = True
                    continue
                if kind != 'com.sun.star.drawing.CustomShape':
                    return None
                shape_type = next((entry.Value for entry in original.CustomShapeGeometry
                                   if entry.Name == 'Type'), None)
                if shape_type not in ('mso-spt202', 'rectangle', 'round-rectangle'):
                    return None
                content = original.getString()
                if not isinstance(content, str):
                    return None
                if not content.strip():
                    if shape_type == 'mso-spt202':
                        return None
                    mixed = True  # Empty rectangle may still be a visible shape.
                    continue
                if shape_type != 'mso-spt202':
                    mixed = True
                boxes.append((page, original))
    except Exception:
        return None
    if not boxes:
        return None
    previous_visual = None
    if mixed:
        try:
            previous_visual = aio_xhtml_visual_scope(target)
        except (OSError, UnicodeError, ValueError, AioPresentationUnverified):
            return None
        if (previous_visual.unsupported
                or len(previous_visual.images) != graphic_objects):
            return None
    replacement = target.with_name(target.stem + '__aio_textbox_recovery.xhtml')
    try:
        for page, original in boxes:
            clone = model.createInstance('com.sun.star.drawing.TextShape')
            page.add(clone)
            clone.Position = original.Position
            clone.Size = original.Size
            clone.String = original.getString()
        model.storeToURL(replacement.as_uri(), tuple(export_options))
        aio_verify_presentation_xhtml([replacement], expected)
        if previous_visual is not None:
            current_visual = aio_xhtml_visual_scope(replacement)
            if (current_visual.unsupported
                    or current_visual.images != previous_visual.images
                    or current_visual.links != previous_visual.links
                    or previous_visual.divs - current_visual.divs):
                raise AioPresentationUnverified('ppt_xhtml_visual_subset_changed')
        os.replace(replacement, target)
        return {'check': 'native_ppt_xhtml_textbox_recovery',
                'status': 'checked_subset', 'textPresent': True,
                'scope': ('ppt_text_and_embedded_image_subset' if mixed else
                          'mso_spt202_textboxes_without_other_slide_shapes'),
                'nativeTextboxCount': len(boxes),
                'nativeImageCount': graphic_objects,
                'visualSubsetCompared': mixed,
                'qualityCompared': False,
                'notChecked': ['layout_equivalence', 'master_page_graphics',
                               'animations', 'links_functionality',
                               'all_source_graphics', 'visual_equivalence']}
    finally:
        replacement.unlink(missing_ok=True)


def aio_export_writer_pages(model, prop, target, export_options, filter_name, preservation=None):
    """Export all Writer pages by moving the UNO view cursor in one loaded model.

    The native Writer graphic exporter renders its current page, not a full
    document. Do not re-open the source, create a PDF or request more Guard
    passes. On an uncertain page count/cursor, fail closed rather than mark
    a first-page-only result ready.
    """
    aliases = {
        'writer_png_Export': ('.png',),
        'writer_jpg_Export': ('.jpg', '.jpeg', '.jfif', '.jif', '.jpe'),
        'writer_webp_Export': ('.webp',),
    }
    if target.suffix.lower() not in aliases.get(filter_name, ()):
        return None
    try:
        controller = model.getCurrentController()
        count = preservation.expected if preservation is not None else controller.PageCount
        cursor = controller.getViewCursor()
        initial = cursor.getPage()
    except Exception as error:
        raise AioWriterPagesUnverified('writer_page_cursor_unavailable') from error
    if type(count) is not int or not 1 <= count <= 4096:
        raise AioWriterPagesUnverified('writer_page_count_unconfirmed')
    if count == 1:
        return None
    # A partial Writer export used to leave earlier pages on disk if a later
    # storeToURL, cursor movement or manifest write failed. Reserve the whole
    # task-local namespace before the first export. Overwrite=False is already
    # provided by the native session; never replace a pre-existing result.
    planned = [target if index == 1 else
               target.with_name(target.stem + '__page_%04d' % index + target.suffix)
               for index in range(1, count + 1)]
    manifest = target.parent / 'AIO-pages.json'
    for path in (*planned, manifest):
        if path.exists() or path.is_symlink():
            raise FileExistsError('native_writer_page_output_exists:' + path.name)
    outputs, mapping, owned = [], [], []
    try:
        for index, path in enumerate(planned, 1):
            if not cursor.jumpToPage(index) or cursor.getPage() != index:
                raise AioWriterPagesUnverified('writer_page_cursor_mismatch:' + str(index))
            # storeToURL can create a truncated output and then throw. Record
            # ownership before invoking it, so failure always rolls it back.
            owned.append(path)
            model.storeToURL(path.as_uri(), tuple(export_options))
            if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
                raise AioWriterPagesUnverified('writer_page_output_missing:' + str(index))
            outputs.append(path)
            mapping.append({'page': index, 'file': path.name})
        with manifest.open('x', encoding='utf-8') as stream:
            owned.append(manifest)
            json.dump({'schema': 1, 'pageCount': count, 'pages': mapping},
                      stream, ensure_ascii=False)
        return outputs + [manifest]
    except BaseException:
        # Keep any older collision file intact. The owned output directory is
        # private to the task; locked files remain for its workspace cleanup.
        for path in reversed(owned):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        try:
            cursor.jumpToPage(initial)
        except Exception:
            pass


def aio_export_graphic_pages(model, remote, prop, target, preservation=None):
    """Export each native Draw/Impress page without flattening vector targets.

    Only an already confirmed output route reaches this executor. Capabilities
    come from the running GraphicExportFilter, not an extension-only allowlist.
    Reserve our own paths and roll them back together on an incomplete export.
    """
    target = Path(target)
    mimes = {'.png':('image/png',), '.jpg':('image/jpeg',), '.jpeg':('image/jpeg',),
             '.jfif':('image/jpeg',), '.jif':('image/jpeg',), '.jpe':('image/jpeg',),
             '.bmp':('image/x-MS-bmp', 'image/bmp'), '.gif':('image/gif',),
             '.tif':('image/tiff',), '.tiff':('image/tiff',),
             '.webp':('image/webp',), '.svg':('image/svg+xml',),
             '.emf':('image/x-emf',), '.wmf':('image/x-wmf',)}.get(target.suffix.lower())
    if mimes is None:
        return None  # Do not query a page model for an unrelated output route.
    pages = model.getDrawPages()
    count = preservation.expected if preservation is not None else pages.getCount()
    if type(count) is not int or not 1 <= count <= 4096:
        raise AioPreservationIncomplete('native_graphic_page_count_unconfirmed')
    if count == 1:
        return None  # Preserve the existing single-page native export path.
    exporter = remote.ServiceManager.createInstanceWithContext(
        'com.sun.star.drawing.GraphicExportFilter', remote)
    mime = next((value for value in mimes if exporter.supportsMimeType(value)), None) if exporter is not None else None
    if mime is None:
        raise RuntimeError('NATIVE_PAGE_ENCODER_UNAVAILABLE')
    outputs, mapping, owned = [], [], []
    manifest = target.parent / 'AIO-pages.json'
    try:
        for index in range(count):
            path = target if index == 0 else target.with_name(
                target.stem + '__page_%04d' % (index + 1) + target.suffix)
            # Never overwrite a result from an earlier call, including an
            # existing symlink. The native encoder may replace only our file.
            with path.open('xb'):
                pass
            owned.append(path)
            exporter.setSourceDocument(pages.getByIndex(index))
            if not exporter.filter((prop('URL', path.as_uri()), prop('MediaType', mime))):
                raise RuntimeError('NATIVE_PAGE_EXPORT_FAILED:' + str(index + 1))
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                raise RuntimeError('NATIVE_PAGE_OUTPUT_MISSING:' + str(index + 1))
            outputs.append(path)
            mapping.append({'page': index + 1, 'file': path.name})
        with manifest.open('x', encoding='utf-8') as stream:
            owned.append(manifest)
            json.dump({'schema': 1, 'pageCount': count, 'pages': mapping},
                      stream, ensure_ascii=False)
        return outputs + [manifest]
    except BaseException:
        for path in reversed(owned):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # The owning workspace retains any locked leftovers.
        raise
'''
