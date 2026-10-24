"""Frame-preserving raster conversion using the application's existing Qt.

Qt supplies image decoding and lossless WebP compression. APNG/WebP animation
containers and TIFF/GIF sequence writers retain the complete frame iterator;
no external process, executable, or additional Python dependency is used.
"""
from fractions import Fraction
import math
import os
from pathlib import Path
import secrets
import struct
import tempfile
import zlib

from .errors import GuardError
from .media_fidelity import ALIASES, RASTER, display_dimensions, display_page_dimensions, inspect_media, _cancel


TARGETS = RASTER


def _codec_error(message, diagnostic=''):
    return GuardError('MEDIA_CODEC_UNAVAILABLE', message, stage='result', diagnostic=diagnostic)


def _qt():
    try:
        from PySide6 import QtCore, QtGui
    except ImportError as error:
        raise _codec_error('В составе приложения недоступны штатные средства обработки изображений Qt.') from error
    return QtCore, QtGui


def _rgba(image):
    _, gui = _qt()
    if image.isNull():
        raise GuardError('MEDIA_DECODE_FAILED', 'Не удалось прочитать полный кадр изображения.', stage='result')
    space = gui.QColorSpace(gui.QColorSpace.NamedColorSpace.SRgb)
    if image.colorSpace().isValid() and image.colorSpace() != space:
        image = image.convertedToColorSpace(space)
    image = image.convertToFormat(gui.QImage.Format.Format_RGBA8888)
    image.setColorSpace(space)
    return image


def _decode_file(path, fmt=None):
    core, gui = _qt()
    reader = gui.QImageReader(str(path))
    if fmt:
        reader.setFormat(core.QByteArray(fmt.encode('ascii')))
    reader.setDecideFormatFromContent(True)
    image = reader.read()
    error = reader.errorString()
    del reader
    if image.isNull():
        raise GuardError('MEDIA_DECODE_FAILED', 'Qt не смог прочитать кадр изображения.', stage='result', diagnostic=error)
    return _rgba(image)


def _orient(image, orientation):
    """Apply TIFF/Exif orientation once when a fallback decoder returns raw pixels."""
    _, gui = _qt()
    if orientation == 2:
        return image.mirrored(True,False)
    if orientation == 3:
        return image.transformed(gui.QTransform().rotate(180))
    if orientation == 4:
        return image.mirrored(False,True)
    if orientation == 5:
        return image.transformed(gui.QTransform(0,1,1,0,0,0))
    if orientation == 6:
        return image.transformed(gui.QTransform().rotate(90))
    if orientation == 7:
        return image.transformed(gui.QTransform(0,-1,-1,0,0,0))
    if orientation == 8:
        return image.transformed(gui.QTransform().rotate(270))
    return image


def _write_chunk(stream, kind, data):
    stream.write(struct.pack('>I', len(data))+kind+data+struct.pack('>I', zlib.crc32(kind+data)&0xffffffff))


def _read_exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise GuardError('MEDIA_DECODE_FAILED', 'Поток изображения оборван.', stage='result')
    return data


def _copy_png_data(source, output, size, *, sequence=False, cancel):
    if sequence:
        _read_exact(source, 4)
        size -= 4
    output.write(struct.pack('>I4s', size, b'IDAT'))
    crc = zlib.crc32(b'IDAT')
    while size:
        _cancel(cancel)
        block = _read_exact(source, min(size, 65536))
        output.write(block)
        crc = zlib.crc32(block, crc)
        size -= len(block)
    _read_exact(source, 4)  # Input CRC was checked by inspect_media.
    output.write(struct.pack('>I', crc&0xffffffff))


def _apng_frames(source, info, cancel):
    """Reconstruct PNG subframes with Qt, then apply APNG disposal/blending."""
    core, gui = _qt()
    canvas = gui.QImage(info['width'], info['height'], gui.QImage.Format.Format_RGBA8888)
    canvas.fill(core.Qt.GlobalColor.transparent)
    canvas.setColorSpace(gui.QColorSpace(gui.QColorSpace.NamedColorSpace.SRgb))
    shared, ihdr, control, output = [], None, None, None
    with tempfile.TemporaryDirectory(prefix='aio-apng-') as temporary:
        frame_path = Path(temporary)/'frame.png'

        def finish():
            nonlocal canvas, output
            if output is None or control is None:
                return None
            _write_chunk(output, b'IEND', b'')
            output.close()
            output = None
            image = _decode_file(frame_path, 'png')
            _seq, width, height, x, y, num, den, dispose, blend = control
            if (image.width(), image.height()) != (width, height):
                raise GuardError('MEDIA_DECODE_FAILED', 'Размер кадра APNG не совпал с описанием.', stage='result')
            previous = canvas.copy() if dispose == 2 else None
            painter = gui.QPainter(canvas)
            try:
                painter.setCompositionMode(gui.QPainter.CompositionMode.CompositionMode_Source
                                           if blend == 0 else gui.QPainter.CompositionMode.CompositionMode_SourceOver)
                painter.drawImage(x, y, image)
            finally:
                painter.end()
            result = _orient(canvas.copy(),info.get('orientation',1)), 1000*num/(den or 100)
            if dispose == 1:
                painter = gui.QPainter(canvas)
                try:
                    painter.setCompositionMode(gui.QPainter.CompositionMode.CompositionMode_Source)
                    painter.fillRect(x, y, width, height, core.Qt.GlobalColor.transparent)
                finally:
                    painter.end()
            elif dispose == 2:
                canvas = previous
            return result

        try:
            with Path(source).open('rb') as stream:
                if _read_exact(stream, 8) != b'\x89PNG\r\n\x1a\n':
                    raise GuardError('MEDIA_DECODE_FAILED', 'Неверный контейнер PNG.', stage='result')
                while True:
                    _cancel(cancel)
                    length, kind = struct.unpack('>I4s', _read_exact(stream, 8))
                    if kind == b'IHDR':
                        ihdr = _read_exact(stream, length)
                        _read_exact(stream, 4)
                    elif kind == b'fcTL':
                        frame = finish()
                        if frame:
                            yield frame
                        control = struct.unpack('>IIIIIHHBB', _read_exact(stream, length))
                        _read_exact(stream, 4)
                        output = frame_path.open('wb')
                        output.write(b'\x89PNG\r\n\x1a\n')
                        _write_chunk(output, b'IHDR', struct.pack('>II', control[1], control[2])+ihdr[8:])
                        for shared_kind, data in shared:
                            _write_chunk(output, shared_kind, data)
                    elif kind in (b'IDAT', b'fdAT') and output is not None:
                        _copy_png_data(stream, output, length, sequence=kind == b'fdAT', cancel=cancel)
                    elif kind == b'IEND':
                        _read_exact(stream, length+4)
                        frame = finish()
                        if frame:
                            yield frame
                        break
                    elif kind in (b'PLTE', b'tRNS', b'gAMA', b'cHRM', b'sRGB', b'iCCP', b'cICP', b'pHYs'):
                        if length > 16*1024*1024:
                            raise GuardError('MEDIA_INSPECTION_INCOMPLETE', 'Цветовой профиль PNG превышает бюджет обработки.', stage='result')
                        shared.append((kind, _read_exact(stream, length)))
                        _read_exact(stream, 4)
                    else:
                        stream.seek(length+4, 1)
        finally:
            if output is not None:
                output.close()


def iter_media_frames(source, info=None, *, cancel=None):
    """Yield fully composited RGBA frames/pages, retaining a bounded frame set."""
    info = info or inspect_media(source, cancel=cancel)
    if info is None:
        raise _codec_error('Этот исходник не является поддерживаемым растровым изображением.')
    if info['format'] == 'png' and info.get('animationContainer'):
        yield from _apng_frames(source, info, cancel)
        return
    core, gui = _qt()
    reader = gui.QImageReader(str(source))
    reader.setFormat(core.QByteArray(info['format'].encode('ascii')))
    reader.setDecideFormatFromContent(True)
    reader.setAutoTransform(True)
    if info['format'] == 'tiff' and not reader.canRead():
        del reader
        from .tiff_frames import iter_tiff_frames
        for index,image in enumerate(iter_tiff_frames(source,cancel=cancel)):
            orientations = info.get('pageOrientations',[])
            image = _orient(image,orientations[index] if index < len(orientations) else 1)
            yield _rgba(image),100
        return
    count = max(info['frames'], info['pages'])
    animation = reader.supportsAnimation()
    for index in range(count):
        _cancel(cancel)
        native_index = info.get('pageImageIndexes',list(range(count)))[index]
        if index and not animation and not reader.jumpToImage(native_index):
            raise _codec_error('Штатный модуль Qt не смог открыть все страницы изображения.', reader.errorString())
        image = reader.read()
        if image.isNull():
            raise _codec_error('Штатный модуль Qt не смог прочитать все кадры изображения.', reader.errorString())
        durations = info.get('durationsMs', [])
        yield _rgba(image), durations[index] if index < len(durations) else 100
    del reader


def _pdf_frames(source, *, cancel):
    core, gui = _qt()
    if not isinstance(core.QCoreApplication.instance(),gui.QGuiApplication):
        raise GuardError('MEDIA_QT_APPLICATION_REQUIRED','Для отрисовки PDF требуется графический Qt.',stage='result')
    try:
        from PySide6.QtPdf import QPdfDocument
    except ImportError as error:
        raise _codec_error('В составе приложения недоступен штатный модуль Qt PDF.') from error
    document = QPdfDocument()
    error = document.load(str(source))
    if error != QPdfDocument.Error.None_:
        raise GuardError('MEDIA_DECODE_FAILED', 'Qt не смог открыть PDF для постраничного экспорта.', stage='result')
    try:
        for page in range(document.pageCount()):
            _cancel(cancel)
            points = document.pagePointSize(page)
            size = core.QSize(max(1, math.ceil(points.width()*300/72)),
                              max(1, math.ceil(points.height()*300/72)))
            image = document.render(page, size)
            _cancel(cancel)
            image = _rgba(image)
            image.setDotsPerMeterX(round(300/0.0254))
            image.setDotsPerMeterY(round(300/0.0254))
            yield image, 0
    finally:
        document.close()


def _apng_delay(milliseconds):
    fraction = Fraction(round(milliseconds), 1000)
    if fraction.numerator <= 65535 and fraction.denominator <= 65535:
        return fraction.numerator, fraction.denominator
    denominator = min(1000, max(1, math.floor(65535000/max(1, milliseconds))))
    numerator = round(milliseconds*denominator/1000)
    if not 0 <= numerator <= 65535:
        raise GuardError('MEDIA_FRAME_DURATION_UNSUPPORTED', 'Длительность кадра не помещается в APNG.', stage='result')
    return numerator, denominator


def _write_apng(path, frames, *, width, height, count, loops, cancel, dots_per_meter=None):
    with Path(path).open('xb') as output:
        output.write(b'\x89PNG\r\n\x1a\n')
        _write_chunk(output, b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
        _write_chunk(output, b'sRGB', b'\0')
        if dots_per_meter and all(0 < value <= 0xffffffff for value in dots_per_meter):
            _write_chunk(output,b'pHYs',struct.pack('>IIB',*dots_per_meter,1))
        animated = count > 1 or Path(path).suffix.lower() == '.apng'
        if animated:
            _write_chunk(output, b'acTL', struct.pack('>II', count, loops))
        sequence = 0
        written = 0
        for index, (image, duration) in enumerate(frames):
            _cancel(cancel)
            if (image.width(), image.height()) != (width, height):
                raise GuardError('MEDIA_CANVAS_CHANGED', 'Кадры имеют разные размеры холста.', stage='result')
            image = _rgba(image)
            if animated:
                num, den = _apng_delay(duration)
                _write_chunk(output, b'fcTL', struct.pack('>IIIIIHHBB', sequence, width, height, 0, 0, num, den, 0, 0))
                sequence += 1
            compressor = zlib.compressobj(6)
            pixels = memoryview(image.constBits())
            stride = image.bytesPerLine()
            def compressed(data):
                nonlocal sequence
                for start in range(0, len(data), 65536):
                    block = data[start:start+65536]
                    if index == 0 or not animated:
                        _write_chunk(output, b'IDAT', block)
                    else:
                        _write_chunk(output, b'fdAT', struct.pack('>I', sequence)+block)
                        sequence += 1
            for row in range(height):
                _cancel(cancel)
                compressed(compressor.compress(b'\0'+pixels[row*stride:row*stride+width*4].tobytes()))
            compressed(compressor.flush())
            written += 1
        if written != count:
            raise GuardError('MEDIA_ANIMATION_LOSS', 'Получены не все кадры исходника.', stage='result')
        _write_chunk(output, b'IEND', b'')


def _riff_chunk(output, kind, data):
    output.write(kind+struct.pack('<I', len(data))+data)
    if len(data)&1:
        output.write(b'\0')


def _encode_webp_frame(image):
    core, gui = _qt()
    buffer = core.QBuffer()
    buffer.open(core.QIODevice.OpenModeFlag.WriteOnly)
    writer = gui.QImageWriter(buffer, core.QByteArray(b'webp'))
    writer.setQuality(100)  # Qt's WebP plugin selects lossless at quality 100.
    if not writer.write(_rgba(image)):
        raise _codec_error('Штатный модуль Qt не смог записать WebP без потерь.', writer.errorString())
    data = bytes(buffer.data())
    buffer.close()
    if data[:4] != b'RIFF' or data[8:12] != b'WEBP':
        raise GuardError('MEDIA_OUTPUT_INVALID', 'Qt вернул неправильный контейнер WebP.', stage='result')
    chunks = []
    offset = 12
    while offset+8 <= len(data):
        kind, length = struct.unpack('<4sI', data[offset:offset+8])
        end = offset+8+length+(length&1)
        if end > len(data):
            raise GuardError('MEDIA_OUTPUT_INVALID', 'Qt вернул оборванный WebP.', stage='result')
        if kind in (b'ALPH', b'VP8 ', b'VP8L'):
            chunks.append(data[offset:end])
        offset = end
    if not any(part[:4] == b'VP8L' for part in chunks):
        raise GuardError('MEDIA_OUTPUT_INVALID', 'Модуль Qt не подтвердил WebP без потерь.', stage='result')
    return data, b''.join(chunks)


def _u24(value):
    if not 0 <= value <= 0xffffff:
        raise GuardError('MEDIA_CONTAINER_RANGE', 'Параметр изображения не помещается в контейнер WebP.', stage='result')
    return value.to_bytes(3, 'little')


def _gif_transparency(source,info,cancel):
    """Reserve a transparent logical background before the first GIF frame."""
    if not info['alpha']:
        return False
    frames = iter_media_frames(source,info,cancel=cancel)
    try:
        for image,_duration in frames:
            pixels,stride = memoryview(image.constBits()),image.bytesPerLine()
            for row in range(image.height()):
                _cancel(cancel)
                if any(value < 128 for value in pixels[row*stride+3:row*stride+image.width()*4:4]):
                    return True
    finally:
        frames.close()
    return False


def _write_webp(path, frames, *, width, height, count, loops, cancel):
    if count == 1:
        image, _duration = next(frames)
        _cancel(cancel)
        data, _chunks = _encode_webp_frame(image)
        with Path(path).open('xb') as output:
            output.write(data)
        return
    if not 0 <= loops <= 65535:
        raise GuardError('MEDIA_LOOP_UNSUPPORTED', 'Число повторов не помещается в WebP.', stage='result')
    with Path(path).open('xb') as output:
        output.write(b'RIFF\0\0\0\0WEBP')
        _riff_chunk(output, b'VP8X', b'\x12\0\0\0'+_u24(width-1)+_u24(height-1))
        _riff_chunk(output, b'ANIM', b'\0\0\0\0'+struct.pack('<H', loops))
        written = 0
        for image, duration in frames:
            _cancel(cancel)
            if (image.width(), image.height()) != (width, height):
                raise GuardError('MEDIA_CANVAS_CHANGED', 'Кадры имеют разные размеры холста.', stage='result')
            _data, chunks = _encode_webp_frame(image)
            header = b'\0'*6+_u24(width-1)+_u24(height-1)+_u24(round(duration))+b'\x02'
            _riff_chunk(output, b'ANMF', header+chunks)
            if output.tell()-8 > 0xffffffff:
                raise GuardError('MEDIA_CONTAINER_RANGE', 'Результат превышает размер контейнера WebP.', stage='result')
            written += 1
        if written != count:
            raise GuardError('MEDIA_ANIMATION_LOSS', 'Получены не все кадры исходника.', stage='result')
        size = output.tell()-8
        output.seek(4)
        output.write(struct.pack('<I', size))


def convert_media_frames(source, output_dir, target, *, cancel=None):
    """Convert admitted raster frames/pages using existing Qt; otherwise None."""
    if target not in TARGETS:
        return None
    source, output_dir = Path(source), Path(output_dir)
    info = inspect_media(source, cancel=cancel)
    pdf = False
    if info is None:
        if target not in ('tif', 'tiff'):
            return None
        with source.open('rb') as stream:
            pdf = stream.read(5) == b'%PDF-'
        if not pdf:
            return None
        frames = _pdf_frames(source, cancel=cancel)
        count, loops = None, 1
    else:
        if info.get('bitDepth', 8) > 8 and target in ('png', 'apng', 'tif', 'tiff'):
            raise GuardError('MEDIA_HIGH_BIT_DEPTH_UNSUPPORTED',
                             'Этот маршрут Qt пока не сохраняет глубину цвета больше 8 бит. Исходник оставлен без изменений.',
                             stage='result')
        source_info = info
        frames = iter_media_frames(source, source_info, cancel=cancel)
        count, loops = max(info['frames'], info['pages']), info.get('loopCount', 1)
        if target in ('png', 'apng', 'gif', 'webp') and info.get('pageDimensions'):
            if any(size != list(display_dimensions(info)) for size in display_page_dimensions(info)):
                raise GuardError('MEDIA_CANVAS_CHANGED', 'Страницы TIFF имеют разные размеры; общий холст не выбран.', stage='result')
        info = {**info,'width':display_dimensions(info)[0],'height':display_dimensions(info)[1]}
    _qt()  # Fail with a specific capability reason before creating partial output.
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir/(source.stem+'.'+target)
    if output.resolve() == source.resolve() or output.exists():
        close = getattr(frames, 'close', None)
        if close:
            close()
        raise GuardError('MEDIA_OUTPUT_COLLISION', 'Путь результата уже занят; исходник и предыдущий результат оставлены.', stage='result')
    partial = output.with_name(output.stem+'.aio-part-'+secrets.token_hex(6)+'.'+target)
    try:
        if target in ('tiff', 'tif'):
            from .raster_writers import write_tiff
            write_tiff(partial, frames, cancel=cancel)
        elif target == 'gif':
            from .raster_writers import write_gif
            write_gif(partial, frames, width=info['width'], height=info['height'],
                      loop=None if loops == 1 else (loops-1 if loops else 0), cancel=cancel,
                      reserve_transparency=_gif_transparency(source,source_info,cancel))
        elif target in ('png', 'apng'):
            _write_apng(partial, frames, width=info['width'], height=info['height'],
                        count=count, loops=loops, cancel=cancel)
        elif target == 'webp':
            _write_webp(partial, frames, width=info['width'], height=info['height'],
                        count=count, loops=loops, cancel=cancel)
        else:
            core,gui = _qt()
            image,_duration = next(frames)
            _cancel(cancel)
            normal = ALIASES.get(target,target)
            if normal in ('jpeg','bmp') and image.hasAlphaChannel():
                opaque = gui.QImage(image.size(),gui.QImage.Format.Format_RGB888)
                opaque.fill(core.Qt.GlobalColor.white)
                painter = gui.QPainter(opaque)
                try:
                    painter.drawImage(0,0,image)
                finally:
                    painter.end()
                image = opaque
            writer = gui.QImageWriter(str(partial),core.QByteArray(normal.encode('ascii')))
            writer.setQuality(100)
            if not writer.write(image):
                raise _codec_error('Штатный модуль Qt не смог записать изображение.',writer.errorString())
            del writer
        _cancel(cancel)
        os.replace(partial, output)
        return [output]
    finally:
        close = getattr(frames, 'close', None)
        if close:
            close()
        partial.unlink(missing_ok=True)


def convert_tiff_pdf(source,output_dir,*,cancel=None):
    """Assemble every decoded TIFF page at its physical size using existing Qt."""
    core,gui = _qt()
    if not isinstance(core.QCoreApplication.instance(),gui.QGuiApplication):
        raise GuardError('MEDIA_QT_APPLICATION_REQUIRED','Для сохранения PDF требуется графический Qt.',stage='result')
    source,output_dir = Path(source),Path(output_dir)
    info = inspect_media(source,cancel=cancel)
    if info is None or info['format'] != 'tiff':
        return None
    output_dir.mkdir(parents=True,exist_ok=True)
    output = output_dir/(source.stem+'.pdf')
    if output.exists():
        raise GuardError('MEDIA_OUTPUT_COLLISION','Путь результата уже занят.',stage='result')
    partial = output.with_name(output.stem+'.aio-part-'+secrets.token_hex(6)+'.pdf')
    writer,painter,frames = None,None,iter_media_frames(source,info,cancel=cancel)
    try:
        writer = gui.QPdfWriter(str(partial))
        writer.setResolution(72)
        count = 0
        for index,(image,_delay) in enumerate(frames):
            _cancel(cancel)
            page_dpi = info.get('pageDpi',[])
            dpi = page_dpi[index] if index < len(page_dpi) else None
            if not dpi or any(value <= 0 for value in dpi):
                dpi = [image.dotsPerMeterX()*0.0254 or 72,image.dotsPerMeterY()*0.0254 or 72]
            orientations = info.get('pageOrientations',[])
            if index < len(orientations) and orientations[index] >= 5:
                dpi = dpi[::-1]
            width,height = image.width()*72/dpi[0],image.height()*72/dpi[1]
            size = gui.QPageSize(core.QSizeF(width,height),gui.QPageSize.Unit.Point)
            if not writer.setPageSize(size) or not writer.setPageMargins(core.QMarginsF(0,0,0,0)):
                raise GuardError('MEDIA_ENCODE_FAILED','Qt не принял размеры страницы PDF.',stage='result')
            if painter is None:
                painter = gui.QPainter(writer)
                painter.setRenderHint(gui.QPainter.RenderHint.LosslessImageRendering,True)
                if not painter.isActive():
                    raise GuardError('MEDIA_ENCODE_FAILED','Qt не смог начать сохранение PDF.',stage='result')
            elif not writer.newPage():
                raise GuardError('MEDIA_ENCODE_FAILED','Qt не смог добавить страницу PDF.',stage='result')
            painter.drawImage(core.QRectF(0,0,width,height),image)
            count += 1
        if painter is not None:
            painter.end()
            painter = None
        del writer
        writer = None
        if count != info['pages']:
            raise GuardError('MEDIA_PAGE_LOSS','Не удалось сохранить все страницы TIFF.',stage='result')
        from pypdf import PdfReader
        with partial.open('rb') as stream:
            if len(PdfReader(stream).pages) != count:
                raise GuardError('MEDIA_PAGE_LOSS','Число страниц результата PDF не совпало.',stage='result')
        _cancel(cancel)
        os.replace(partial,output)
        return [output]
    finally:
        if painter is not None:
            painter.end()
        if writer is not None:
            del writer
        frames.close()
        partial.unlink(missing_ok=True)
