"""Narrow native PPTX workaround for theme-free ODF solid RGB fills.

The source is inspected only for this export, never during Guard preflight.
No source/output XML is rewritten. Declared themes and foreign extensions opt
out; the existing native exporter remains responsible for the presentation.
"""
PPTX_COLOUR_SOURCE = r'''
import zipfile as _aio_colour_zip
from xml.parsers import expat as _aio_colour_expat

_AIO_COLOUR_XML_BUDGET = 16 * 1024 * 1024
_AIO_COLOUR_SHAPE_BUDGET = 100000
_AIO_COLOUR_IMPORTS = frozenset((
    'OpenDocument Presentation Flat XML', 'impress8', 'impress8_template'))
_AIO_COLOUR_EXPORTS = frozenset(('Impress MS PowerPoint 2007 XML',))
_AIO_COLOUR_NS = frozenset(
    'urn:oasis:names:tc:opendocument:xmlns:' + name + ':1.0'
    for name in ('office','style','text','table','drawing','presentation',
                 'svg-compatible','xsl-fo-compatible','animation','smil-compatible',
                 'meta','datastyle','chart','dr3d','form','config','script')) | frozenset((
    'http://www.w3.org/XML/1998/namespace', 'http://www.w3.org/1999/xlink',
    'http://purl.org/dc/elements/1.1/'))

class AioPptxColourUnverified(RuntimeError):
    pass

class _AioColourScopeUnknown(Exception):
    pass


def aio_pptx_colour_scope(source, import_filter, export_filter):
    """Prove absence of theme/extension semantics in all native ODF styles.

    Native filter identity, not a filename suffix, selects this inspection.
    Uncertain sources keep their untouched native path and an explicit
    not-checked receipt. Streaming inspection does not expand entities.
    """
    if export_filter not in _AIO_COLOUR_EXPORTS or import_filter not in _AIO_COLOUR_IMPORTS:
        return None
    consumed, rgb_declarations = 0, 0
    def reject(*args):
        raise _AioColourScopeUnknown('doctype_or_entity')
    def inspect(stream, expected_root):
        nonlocal consumed, rgb_declarations
        parser = _aio_colour_expat.ParserCreate(namespace_separator=' ')
        root_seen = False
        def start(name, attrs):
            nonlocal root_seen, rgb_declarations
            if not root_seen:
                if name != 'urn:oasis:names:tc:opendocument:xmlns:office:1.0 ' + expected_root:
                    raise _AioColourScopeUnknown('unexpected_xml_root')
                root_seen = True
            for key in (name, *attrs):
                namespace, _, local = key.rpartition(' ')
                if 'theme' in local.lower() or 'complex-color' in local.lower():
                    raise _AioColourScopeUnknown('declared_theme')
                if namespace not in _AIO_COLOUR_NS:
                    raise _AioColourScopeUnknown('extension_semantics')
            colour = attrs.get('urn:oasis:names:tc:opendocument:xmlns:drawing:1.0 fill-color')
            if colour is not None:
                if len(colour) != 7 or colour[0] != '#' or any(c not in '0123456789abcdefABCDEF' for c in colour[1:]):
                    raise _AioColourScopeUnknown('non_rgb_fill')
                rgb_declarations += 1
        parser.StartElementHandler = start
        parser.StartDoctypeDeclHandler = reject
        parser.EntityDeclHandler = reject
        parser.ExternalEntityRefHandler = reject
        while True:
            block = stream.read(65536)
            consumed += len(block)
            if consumed > _AIO_COLOUR_XML_BUDGET:
                raise _AioColourScopeUnknown('xml_inspection_budget')
            parser.Parse(block, not block)
            if not block:
                break
        if not root_seen:
            raise _AioColourScopeUnknown('empty_xml')
    try:
        if import_filter == 'OpenDocument Presentation Flat XML':
            with Path(source).open('rb') as stream:
                inspect(stream, 'document')
        else:
            with _aio_colour_zip.ZipFile(source) as archive:
                names = archive.namelist()
                for name, root in (('content.xml','document-content'), ('styles.xml','document-styles')):
                    if names.count(name) != 1:
                        raise _AioColourScopeUnknown('missing_or_duplicate_styles')
                    with archive.open(name) as stream:
                        inspect(stream, root)
        if not rgb_declarations:
            raise _AioColourScopeUnknown('no_explicit_rgb_fill')
    except _AioColourScopeUnknown as error:
        return {'eligible': False, 'reason': str(error)}
    except (OSError, ValueError, RuntimeError, _aio_colour_zip.BadZipFile,
            _aio_colour_expat.ExpatError):
        return {'eligible': False, 'reason': 'source_xml_unverified'}
    return {'eligible': True, 'reason': 'theme_free_odf',
            'xmlBytes': consumed, 'rgbDeclarations': rgb_declarations}


def aio_export_theme_free_pptx(model, source, import_filter, target, export_options, export_filter):
    """Use LO's exporter once, clearing only importer-invented theme bindings.

    On theme-free ODF, a theme reference has no source semantics to preserve.
    All candidate properties are read first; RGB/style/transparency are checked
    after clearing. Restore loaded property values even when export fails.
    This is not a visual comparison or a universal colour fidelity claim.
    """
    if Path(target).suffix.lower() != '.pptx':
        return None
    scope = aio_pptx_colour_scope(source, import_filter, export_filter)
    if scope is None:
        return None
    receipt = {'check': 'native_pptx_solid_rgb', 'qualityCompared': False,
               'scope': 'theme_free_odf_solid_fills', 'normalizedFills': 0,
               'status': 'not_checked', 'reason': scope['reason']}
    if not scope['eligible']:
        model.storeToURL(Path(target).as_uri(), tuple(export_options))
        return receipt
    candidates, visited = [], 0
    try:
        for collection in (model.getDrawPages(), model.getMasterPages()):
            # Iterator frames, not a materialized list of all document shapes.
            stack = [(collection, 0, collection.getCount())]
            while stack:
                parent, index, count = stack.pop()
                if type(count) is not int or count < 0 or count > _AIO_COLOUR_SHAPE_BUDGET:
                    raise AioPptxColourUnverified('native_colour_shape_count')
                if index >= count:
                    continue
                stack.append((parent, index + 1, count))
                shape = parent.getByIndex(index)
                visited += 1
                if visited > _AIO_COLOUR_SHAPE_BUDGET:
                    raise AioPptxColourUnverified('native_colour_shape_budget')
                if parent is collection or shape.supportsService('com.sun.star.drawing.GroupShape'):
                    stack.append((shape, 0, shape.getCount()))
                    continue
                info = shape.getPropertySetInfo()
                if not all(info.hasPropertyByName(n) for n in ('FillStyle','FillColor','FillComplexColor','FillTransparence')):
                    continue
                style = shape.getPropertyValue('FillStyle')
                if getattr(style, 'value', None) != 'SOLID':
                    continue
                complex_colour = shape.getPropertyValue('FillComplexColor')
                if complex_colour is None or complex_colour.getThemeColorType() == -1:
                    continue
                rgb = shape.getPropertyValue('FillColor')
                alpha = shape.getPropertyValue('FillTransparence')
                if type(rgb) is not int or not 0 <= rgb <= 0xffffff or type(alpha) is not int or not 0 <= alpha <= 100:
                    raise AioPptxColourUnverified('native_colour_value_unconfirmed')
                candidates.append((shape, complex_colour, rgb, style, alpha))
    except AioPptxColourUnverified:
        raise
    except Exception as error:
        raise AioPptxColourUnverified('native_colour_model_unavailable') from error
    touched = []
    try:
        for shape, original, rgb, style, alpha in candidates:
            touched.append((shape, original, rgb, style, alpha))
            try:
                # Attribute assignment uses the UNO property's interface type.
                # setPropertyValue(..., None) sends a void Any and can silently
                # leave XComplexColor unchanged in PyUNO.
                shape.FillComplexColor = None
            except Exception as error:
                raise AioPptxColourUnverified('native_colour_alignment_unavailable') from error
            after = shape.getPropertyValue('FillComplexColor')
            if ((after is not None and after.getThemeColorType() != -1)
                    or shape.getPropertyValue('FillColor') != rgb
                    or shape.getPropertyValue('FillStyle') != style
                    or shape.getPropertyValue('FillTransparence') != alpha):
                raise AioPptxColourUnverified('native_colour_alignment_failed')
        model.storeToURL(Path(target).as_uri(), tuple(export_options))
        receipt.update(status='checked_subset', normalizedFills=len(candidates),
                       checkedFeatures=['loaded_rgb_unchanged','loaded_fill_style_unchanged',
                                        'loaded_fill_transparency_unchanged'],
                       notChecked=['export_visual_equivalence','gradients','text_colours','line_colours'])
        return receipt
    finally:
        restore_failed = False
        for shape, original, rgb, style, alpha in reversed(touched):
            try:
                shape.setPropertyValue('FillComplexColor', original)
                shape.setPropertyValue('FillColor', rgb)
                if shape.getPropertyValue('FillStyle') != style:
                    shape.setPropertyValue('FillStyle', style)
                if shape.getPropertyValue('FillTransparence') != alpha:
                    shape.setPropertyValue('FillTransparence', alpha)
                restored = shape.getPropertyValue('FillComplexColor')
                if (restored is None or restored.getThemeColorType() != original.getThemeColorType()
                        or shape.getPropertyValue('FillColor') != rgb
                        or shape.getPropertyValue('FillStyle') != style
                        or shape.getPropertyValue('FillTransparence') != alpha):
                    restore_failed = True
            except Exception:
                restore_failed = True
        if restore_failed:
            raise AioPptxColourUnverified('native_colour_restore_failed')
'''
