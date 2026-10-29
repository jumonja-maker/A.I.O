"""Native Calc print-page raster export, embedded in the existing UNO worker.

No PDF staging, new office process, source reload, or Python pixel transport.
The printed layout, not every hidden/unprinted cell, is the contract scope.
"""
CALC_PAGES_SOURCE = r'''
import json
import math
from pathlib import Path

_AIO_CALC_RASTER_FILTERS = {
    'calc_png_Export': (('png',), 'image/png'),
    'calc_jpg_Export': (('jpg', 'jpeg', 'jfif', 'jif', 'jpe'), 'image/jpeg'),
}
_AIO_CALC_MAX_PIXELS = 16 * 1024 * 1024
_AIO_CALC_MAX_PAGES = 4096


def aio_calc_page_dimensions(size, device_info):
    """Use the native device resolution; never clamp or distort a paper size."""
    width, height = getattr(size, 'Width', None), getattr(size, 'Height', None)
    if (type(width) is not int or type(height) is not int
            or width <= 0 or height <= 0):
        raise AioPreservationIncomplete('calc_page_size_unconfirmed')
    ppm = (device_info.PixelPerMeterX, device_info.PixelPerMeterY)
    if any(type(value) not in (float, int) or not math.isfinite(value)
           or not 100 <= value <= 100000 for value in ppm):
        raise AioPreservationIncomplete('calc_device_resolution_unconfirmed')
    pixels = tuple(int(math.floor(length * density / 100000 + 0.5))
                   for length, density in zip((width, height), ppm))
    if min(pixels) <= 0 or pixels[0] * pixels[1] > _AIO_CALC_MAX_PIXELS:
        raise AioPreservationIncomplete('calc_page_pixel_budget')
    return pixels


def aio_export_calc_pages(model, remote, prop, target, filter_name):
    """Render every Calc print page from the model already opened by the worker.

    A native XDevice is captured through GraphicProvider(Bitmap), so neither
    raw pixels nor temporary PDFs cross into the application. Hidden sheets,
    cells outside print areas and nonprinting objects follow Calc print rules.
    Returned plan describes that scope only. Uncertainty never selects page 1.
    """
    target = Path(target)
    kind = target.suffix.lower().lstrip('.')
    spec = _AIO_CALC_RASTER_FILTERS.get(filter_name)
    if spec is None or kind not in spec[0]:
        return None
    service = remote.ServiceManager.createInstanceWithContext
    toolkit = service('com.sun.star.awt.Toolkit', remote)
    provider = service('com.sun.star.graphic.GraphicProvider', remote)
    if toolkit is None or provider is None:
        raise AioPreservationIncomplete('calc_native_renderer_unavailable')
    controller = model.getCurrentController()
    probe_device = toolkit.createScreenCompatibleDevice(32, 32)
    if controller is None or probe_device is None:
        raise AioPreservationIncomplete('calc_render_context_unavailable')
    info = probe_device.getInfo()

    def options(device, *, first=False, last=False):
        return tuple(prop(key, value) for key, value in (
            ('IsPrinter', True), ('RenderDevice', device), ('View', controller),
            ('RenderToGraphic', True), ('PrintContent', 0), ('PrintRange', 0),
            ('PageRange', ''), ('IsSuppressEmptyPages', True),
            ('IsFirstPage', first), ('IsLastPage', last)))

    render_options = options(probe_device)
    count = model.getRendererCount(model, render_options)
    if type(count) is not int or not 1 <= count <= _AIO_CALC_MAX_PAGES:
        raise AioPreservationIncomplete('calc_print_page_count_unconfirmed')
    plan = AioPreservationPlan('native_all_calc_print_pages_v1', 'pages', count,
                               kind, 'loaded_calc_print_layout')
    sheet_names = model.getSheets().getElementNames()
    outputs, mapping, owned = [], [], []
    manifest = target.parent / 'AIO-pages.json'
    try:
        for index in range(count):
            properties = {p.Name: p.Value for p in
                          model.getRenderer(index, model, render_options)}
            size = properties.get('PageSize')
            width, height = aio_calc_page_dimensions(size, info)
            address = properties.get('SourceRange')
            fields = [getattr(address, key, None) for key in
                      ('Sheet', 'StartColumn', 'StartRow', 'EndColumn', 'EndRow')]
            if (any(type(value) is not int or value < 0 for value in fields)
                    or fields[0] >= len(sheet_names) or fields[3] < fields[1]
                    or fields[4] < fields[2]):
                raise AioPreservationIncomplete('calc_page_source_range_unconfirmed')
            path = (target if index == 0 else target.with_name(
                target.stem + '__page_%04d' % (index + 1) + target.suffix))
            # This directory belongs to one task. Still never overwrite a
            # pre-existing result/symlink, including on a failed retry.
            with path.open('xb'):
                pass
            owned.append(path)
            device = toolkit.createScreenCompatibleDevice(width, height)
            if device is None:
                raise AioPreservationIncomplete('calc_page_device_unavailable')
            graphics = device.createGraphics()
            graphics.setFillColor(0xffffff)
            graphics.setLineColor(0xffffff)
            graphics.drawRect(0, 0, width, height)
            # Preserve the device map mode across Calc's renderer, which
            # changes physical units and page scaling. Capture in pixels only
            # after restoring the native state, including when render fails.
            graphics.push()
            try:
                model.render(index, model, options(device, first=index == 0, last=index == count - 1))
            finally:
                graphics.pop()
            bitmap = device.createBitmap(0, 0, width, height)
            if bitmap is None:
                raise AioPreservationIncomplete('calc_page_bitmap_unavailable')
            observed = bitmap.getSize()
            if (observed.Width, observed.Height) != (width, height):
                raise AioPreservationIncomplete('calc_render_mapping_unconfirmed')
            graphic = provider.queryGraphic((prop('Bitmap', bitmap),))
            if graphic is None:
                raise AioPreservationIncomplete('calc_page_graphic_unavailable')
            provider.storeGraphic(graphic, (prop('URL', path.as_uri()),
                                            prop('MimeType', spec[1])))
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise AioPreservationLoss('calc_page_output_missing:%d' % (index + 1))
            # Do not retain the preceding page's canvas during next allocation.
            del graphic, bitmap, graphics, device
            outputs.append(path)
            mapping.append({'page': index + 1, 'file': path.name,
                            'sheetIndex': fields[0], 'sheetName': sheet_names[fields[0]],
                            'sourceRange': fields[1:], 'pixelSize': [width, height],
                            'paperSize100mm': [size.Width, size.Height]})
        with manifest.open('x', encoding='utf-8') as stream:
            owned.append(manifest)
            json.dump({'schema': 1, 'pageCount': count, 'pages': mapping,
                       'scope': 'loaded_calc_print_layout',
                       'dpi': [round(info.PixelPerMeterX * .0254, 3),
                               round(info.PixelPerMeterY * .0254, 3)],
                       'respectsPrintSettings': True,
                       'qualityCompared': False}, stream, ensure_ascii=False)
        return outputs + [manifest], plan
    except BaseException:
        # Never delete an existing path that this call did not create.
        for path in reversed(owned):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # The owning workspace handles any locked leftovers.
        raise
'''
