"""Preserve native browser raster media in HTML, including complete animation.

Browser image containers are copied without re-encoding. TIFF document pages
are decoded through the same Qt/bounded-strip path as raster conversion and
written as separate lossless PNG images, in their original page order.
"""
import hashlib
from html import escape
from pathlib import Path
import secrets

from .errors import GuardError
from .media_fidelity import display_dimensions, inspect_media, _cancel
from .media_frames import _write_apng, iter_media_frames

BROWSER_FORMATS = frozenset(('png','gif','webp','jpeg','bmp'))
HTML_TARGETS = frozenset(('html','htm','xhtml'))


def _hash(path,cancel):
    digest,size = hashlib.sha256(),0
    with Path(path).open('rb') as stream:
        while True:
            _cancel(cancel)
            block = stream.read(65536)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return size,digest.hexdigest()


def _copy_image(source,destination,cancel):
    digest,size = hashlib.sha256(),0
    with source.open('rb') as reader,destination.open('xb') as writer:
        while True:
            _cancel(cancel)
            block = reader.read(65536)
            if not block:
                break
            writer.write(block)
            digest.update(block)
            size += len(block)
    expected = size,digest.hexdigest()
    if _hash(destination,cancel) != expected:
        raise GuardError('MEDIA_HTML_COPY_CHANGED','Копия изображения для HTML не совпала с исходными байтами.',stage='result')
    return expected[1]


def _page_markup(number,relative,width,height,*,label=False):
    heading = '<h2>Страница '+str(number)+'</h2>' if label else ''
    return ('<section class="aio-media-page" data-aio-page="'+str(number)+'">'+heading+
            '<img src="'+escape(relative,quote=True)+'" width="'+str(width)+'" height="'+str(height)+
            '" alt="'+('Страница '+str(number) if label else 'Изображение')+'" />'+
            '</section>')


def _document(source,info,pages,target,digest):
    title = escape(source.stem,quote=True)
    prefix = '<?xml version="1.0" encoding="UTF-8"?>\n' if target == 'xhtml' else ''
    namespace = ' xmlns="http://www.w3.org/1999/xhtml"' if target == 'xhtml' else ''
    return (prefix+'<!DOCTYPE html>\n<html'+namespace+' lang="ru"><head>'+
        '<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />'+
        '<meta name="aio-source-sha256" content="'+digest+'" />'+
        '<title>'+title+'</title><style>'+
        'html,body{margin:0;padding:0;background:transparent;color:#202124}'
        'main{display:block}.aio-media-page{display:table;margin:0 auto 16px;break-after:page}'
        '.aio-media-page:last-child{margin-bottom:0}.aio-media-page img{display:block;max-width:none;border:0}'
        '.aio-media-page h2{font:14px system-ui,sans-serif;margin:8px 0}'
        '</style></head><body data-aio-media-format="'+info['format']+'" data-aio-source-frames="'+
        str(info['frames'])+'" data-aio-source-pages="'+str(info['pages'])+'"><main>'+
        ''.join(pages)+'</main></body></html>\n')


def convert_media_html(source,output_dir,target,*,cancel=None,media_profile=None):
    """Return HTML and all companion images after ordinary Guard admission.

    None leaves non-raster or unsupported image layouts to the existing route.
    The caller's media profile is checked again against actual source content.
    """
    if target not in HTML_TARGETS:
        return None
    source,output_dir = Path(source),Path(output_dir)
    info = inspect_media(source,cancel=cancel)
    if info is None or info['format'] not in BROWSER_FORMATS | {'tiff'}:
        return None
    if info['format'] == 'tiff' and info.get('bitDepth',8) > 8:
        return None
    if media_profile is not None:
        if not isinstance(media_profile,dict) or any(media_profile.get(key) != info.get(key)
                for key in ('format','width','height','frames','pages')):
            raise GuardError('MEDIA_SOURCE_CHANGED','Описание изображения изменилось после предварительной проверки.',stage='result')
    if info['format'] == 'tiff':
        from .qt_media import media_preflight
        proof = media_preflight(source,cancel=cancel)
        if not proof or not proof.get('decoderAvailable'):
            return None
    output = output_dir/(source.stem+'.'+target)
    if output.resolve() == source.resolve() or output.exists():
        raise GuardError('MEDIA_OUTPUT_COLLISION','Путь HTML-результата уже занят.',stage='result')
    output_dir.mkdir(parents=True,exist_ok=True)
    assets = output_dir/'media'
    # A task owns a fresh directory. Do not replace a previous output or delete
    # companion files created by another invocation after an interrupted run.
    if assets.exists():
        raise GuardError('MEDIA_OUTPUT_COLLISION','Папка изображений HTML уже существует.',stage='result')
    assets.mkdir()
    created,final_images,pages = [],[],[]
    partial = output.with_name(output.name+'.aio-part-'+secrets.token_hex(6))
    success = False
    try:
        if info['format'] in BROWSER_FORMATS:
            image = assets/('source.'+info['format'])
            created.append(image)
            digest = _copy_image(source,image,cancel)
            copied = inspect_media(image,cancel=cancel)
            if copied != info:
                raise GuardError('MEDIA_HTML_COPY_CHANGED','Свойства копии изображения для HTML изменились.',stage='result')
            width,height = display_dimensions(info)
            pages.append(_page_markup(1,image.relative_to(output_dir).as_posix(),width,height))
            final_images.append(image)
        else:
            digest = _hash(source,cancel)[1]
            frames = iter_media_frames(source,info,cancel=cancel)
            try:
                for number,(image,_duration) in enumerate(frames,1):
                    _cancel(cancel)
                    png = assets/('page-'+str(number).zfill(6)+'.png')
                    created.append(png)
                    width,height = image.width(),image.height()
                    _write_apng(png,iter(((image,0),)),width=width,height=height,count=1,loops=1,
                        cancel=cancel,dots_per_meter=(image.dotsPerMeterX(),image.dotsPerMeterY()))
                    actual = inspect_media(png,cancel=cancel)
                    if not actual or (actual['width'],actual['height'],actual['frames']) != (width,height,1):
                        raise GuardError('MEDIA_HTML_PAGE_CHANGED','Не удалось проверить страницу TIFF в HTML.',stage='result')
                    pages.append(_page_markup(number,png.relative_to(output_dir).as_posix(),width,height,label=True))
                    final_images.append(png)
            finally:
                frames.close()
            if len(final_images) != info['pages']:
                raise GuardError('MEDIA_PAGE_LOSS','HTML не содержит все страницы исходного TIFF.',stage='result')
        _cancel(cancel)
        created.append(partial)
        with partial.open('xb') as stream:
            stream.write(_document(source,info,pages,target,digest).encode('utf-8'))
        _cancel(cancel)
        partial.replace(output)
        success = True
        return [output,*final_images]
    finally:
        if not success:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            assets.rmdir()
