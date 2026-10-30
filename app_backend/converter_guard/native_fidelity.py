"""Export contracts run inside the existing loaded UNO model, not a new pass.

The worker uses the same source string as the unit tests, without importing the
application or extra packages into LibreOffice's embedded Python.
"""
from .preservation_contract import PRESERVATION_POLICY_SOURCE
from .calc_pages import CALC_PAGES_SOURCE

NATIVE_FIDELITY_SOURCE = PRESERVATION_POLICY_SOURCE + CALC_PAGES_SOURCE + r'''
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




def aio_try_ppt_xhtml_textboxes(model, target, export_options, expected):
    """Recover only native text boxes dropped by LibreOffice's PPT XHTML XSLT.

    Restrict to simple old-PPT mso-spt202 boxes and ordinary text shapes.
    Images, diagrams, groups, non-text shapes and ambiguous geometry must keep
    the existing ENGINE_OUTPUT_CONTENT_LOSS refusal. Source is never saved.
    """
    if not expected:
        return None
    boxes = []
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
                if kind != 'com.sun.star.drawing.CustomShape':
                    return None
                shape_type = next((entry.Value for entry in original.CustomShapeGeometry
                                   if entry.Name == 'Type'), None)
                if shape_type != 'mso-spt202' or not original.getString().strip():
                    return None
                boxes.append((page, original))
    except Exception:
        return None  # An uninspectable presentation is never eligible.
    if not boxes:
        return None
    # Never overwrite the original primary until a second native XHTML export
    # exists and its text has passed independent output readback.
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
        os.replace(replacement, target)
        return {'check': 'native_ppt_xhtml_textbox_recovery',
                'status': 'checked_subset', 'textPresent': True,
                'scope': 'mso_spt202_textboxes_without_other_slide_shapes',
                'nativeTextboxCount': len(boxes), 'qualityCompared': False,
                'notChecked': ['layout_equivalence', 'master_page_graphics',
                               'animations', 'links', 'visual_equivalence']}
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
    outputs, mapping = [], []
    try:
        for index in range(1, count + 1):
            if not cursor.jumpToPage(index) or cursor.getPage() != index:
                raise AioWriterPagesUnverified('writer_page_cursor_mismatch:' + str(index))
            path = (target if index == 1 else
                    target.with_name(target.stem + '__page_%04d' % index + target.suffix))
            model.storeToURL(path.as_uri(), tuple(export_options))
            if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
                raise AioWriterPagesUnverified('writer_page_output_missing:' + str(index))
            outputs.append(path)
            mapping.append({'page':index,'file':path.name})
    finally:
        try:
            cursor.jumpToPage(initial)
        except Exception:
            pass
    manifest = target.parent / 'AIO-pages.json'
    manifest.write_text(json.dumps({'schema': 1, 'pageCount':count, 'pages':mapping},
                                   ensure_ascii=False), encoding='utf-8')
    return outputs + [manifest]


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
