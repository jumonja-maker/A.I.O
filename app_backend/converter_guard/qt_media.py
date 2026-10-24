"""Qt media capability proofs and compatibility entry points.

All conversion delegates to media_frames/raster_writers. The public functions
stay stable for Guard's adapter and existing acceptance tools.
"""
import math
from pathlib import Path

from .errors import GuardError
from .media_fidelity import ALIASES, RASTER, _cancel, inspect_media


def _qt():
    try:
        from PySide6 import QtCore, QtGui
    except ImportError as error:
        raise GuardError('MEDIA_QT_UNAVAILABLE', 'Компонент Qt для изображений недоступен.', stage='result') from error
    return QtCore, QtGui


def _tiff_support(source, cancel):
    try:
        from .tiff_frames import inspect_tiff_decode_support
    except ImportError:
        return False
    return inspect_tiff_decode_support(source, cancel=cancel).get('supported', False)


def supported_media_targets(info, *, source=None, cancel=None):
    """Advertise actual codecs and exact byte preservation independently."""
    core, gui = _qt()
    if info['format'] == 'pdf':
        try:
            from PySide6.QtPdf import QPdfDocument  # noqa: F401
        except ImportError:
            return ()
        return ('tif', 'tiff')
    kind = info['format']
    targets = set()
    if kind in ('png', 'gif', 'webp', 'tiff'):
        targets.add(kind)
        if kind == 'tiff':
            targets.add('tif')
        if info.get('animationContainer'):
            targets.add('apng')
    if kind in ('png','gif','webp','jpeg','bmp'):
        targets.update(('html','htm','xhtml'))
    readers = {bytes(value).decode('ascii') for value in gui.QImageReader.supportedImageFormats()}
    writers = {bytes(value).decode('ascii') for value in gui.QImageWriter.supportedImageFormats()}
    decoder = kind in readers or kind == 'tiff' and source is not None and _tiff_support(source, cancel)
    if not decoder:
        return tuple(sorted(targets))
    targets.add('gif')
    if info.get('bitDepth', 8) <= 8:
        targets.update(('png', 'apng', 'tif', 'tiff'))
    targets.update(writers.intersection(('jpeg', 'jpg', 'bmp', 'webp')))
    if kind == 'tiff':
        targets.add('pdf')
        if info.get('bitDepth',8) <= 8:
            targets.update(('html','htm','xhtml'))
    return tuple(sorted(targets))


def media_preflight(source, *, cancel=None):
    """Return content and actual loaded-codec capability, without exporting."""
    source = Path(source)
    _cancel(cancel)
    core, gui = _qt()
    info = inspect_media(source, cancel=cancel)
    if info is None:
        with source.open('rb') as stream:
            if stream.read(5) != b'%PDF-':
                return None
        # PDFium may consult Qt's font database during document loading.
        # CLI callers without a main-thread GUI application keep the existing
        # LibreOffice preflight route; never instantiate QtPdf in that state.
        if not isinstance(core.QCoreApplication.instance(),gui.QGuiApplication):
            return None
        try:
            from PySide6.QtPdf import QPdfDocument
        except ImportError:
            return None
        document = QPdfDocument()
        try:
            if document.load(str(source)) != QPdfDocument.Error.None_ or document.pageCount() <= 0:
                return None
            size = document.pagePointSize(0)
            info = {'format': 'pdf', 'pages': document.pageCount(), 'frames': 1,
                    'width': math.ceil(size.width()*300/72), 'height': math.ceil(size.height()*300/72),
                    'alpha': False, 'durationsMs': [], 'rasterDpi': 300}
        finally:
            document.close()
        return {'source': info, 'targets': ['tif', 'tiff'], 'decoder': 'qt_pdf_pages',
                'decoderAvailable': True, 'exported': False}
    decoder = 'manual_apng_qt_png' if info.get('animationContainer') else 'qt_image_reader'
    # Match the conversion iterator: a first-page Qt canRead() result does not
    # prove that its optional TIFF plugin can seek every later IFD.
    if info['format'] == 'tiff' and _tiff_support(source, cancel):
        can_read, decoder = True, 'bounded_tiff_strips'
    else:
        reader = gui.QImageReader(str(source))
        try:
            reader.setFormat(core.QByteArray(info['format'].encode('ascii')))
            reader.setDecideFormatFromContent(True)
            can_read = reader.canRead()
        finally:
            device = reader.device()
            if device is not None:
                device.close()
            del reader
    targets = supported_media_targets(info, source=source, cancel=cancel)
    if not can_read:
        kind = info['format']
        targets = tuple(t for t in targets if (ALIASES.get(t, t) == kind
                        and (t != 'apng' or info.get('animationContainer')))
                        or kind in ('png','gif','webp','jpeg','bmp') and t in ('html','htm','xhtml'))
    _cancel(cancel)
    return {'source': info, 'targets': list(targets), 'decoder': decoder,
            'decoderAvailable': can_read, 'exported': False}


def media_frame_route(source, target, *, cancel=None):
    if target not in RASTER:
        return None
    info = inspect_media(source, cancel=cancel)
    return None if info is None else {'source': info, 'target': ALIASES.get(target, target),
                                     'executionMode': 'qt_media_frames'}


def has_transparent_pixels(source, *, cancel=None, alpha_threshold=255):
    from .media_frames import iter_media_frames
    info = inspect_media(source, cancel=cancel)
    if not info or not info['alpha']:
        return False
    frames = iter_media_frames(source, info, cancel=cancel)
    try:
        for image, _duration in frames:
            pixels, stride = memoryview(image.constBits()), image.bytesPerLine()
            for row in range(image.height()):
                _cancel(cancel)
                if any(value < alpha_threshold for value in pixels[row*stride+3:row*stride+image.width()*4:4]):
                    return True
    finally:
        frames.close()
    return False


def convert_media_frames(source, output_dir, target, *, cancel=None, on_started=None):
    from .media_frames import convert_media_frames as convert
    # on_started belongs to LibreOffice process accounting. Qt's execution
    # mode and metrics are recorded separately by the adapter.
    return convert(source, output_dir, target, cancel=cancel)


def convert_document_pages(source, output_dir, target, *, cancel=None, on_started=None):
    source = Path(source)
    info = inspect_media(source, cancel=cancel)
    if target == 'pdf' and info and info['format'] == 'tiff':
        from .media_frames import convert_tiff_pdf
        return convert_tiff_pdf(source, output_dir, cancel=cancel)
    if target in ('tif', 'tiff') and info is None:
        with source.open('rb') as stream:
            if stream.read(5) == b'%PDF-':
                from .media_frames import convert_media_frames as convert
                return convert(source, output_dir, target, cancel=cancel)
    return None
