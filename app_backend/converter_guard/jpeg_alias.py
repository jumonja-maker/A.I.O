"""Validate native JPEG aliases using the application's existing Qt codec."""
from pathlib import Path
from .errors import GuardError


def verify_native_jpeg_alias(paths, target, *, cancel):
    from .qt_media import _qt, _bounds
    primaries = [Path(p) for p in paths if Path(p).suffix.lower().lstrip('.') in (target,'jpg','jpeg')]
    if not primaries:
        raise GuardError('ENGINE_OUTPUT_MISSING', 'Не создан JPEG запрошенного формата.', stage='result')
    reader_type = _qt()[5]
    entries = []
    for path in primaries:
        if cancel.is_set():
            raise GuardError('CANCELLED','Проверка JPEG отменена.',stage='result')
        if path.is_symlink() or not path.is_file():
            raise GuardError('ENGINE_OUTPUT_INVALID','Недоступен JPEG-результат.',stage='result')
        with path.open('rb') as stream:
            if stream.read(3) != b'\xff\xd8\xff':
                raise GuardError('ENGINE_OUTPUT_INVALID','Результат не является JPEG.',stage='result')
        reader=reader_type(str(path))
        # Aliases are not all recognized as filename suffixes by Qt. The
        # signature has already established JPEG; bind that codec explicitly.
        # Qt can clear format() after read(), so it is not a post-decode test.
        reader.setFormat(b'jpeg')
        size=reader.size();_bounds(size.width(),size.height())
        image=reader.read()
        if image.isNull():
            raise GuardError('MEDIA_DECODE_FAILED','Qt не смог декодировать JPEG-результат.',
                             stage='result',diagnostic=reader.errorString())
        _bounds(image.width(),image.height())
        entries.append({'file':path.name,'width':image.width(),'height':image.height()})
    return {'check':'native_jpeg_alias','status':'checked_subset',
            'checkedFeatures':['jpeg_signature','qt_jpeg_decode','pixel_dimensions'],
            'outputs':entries,'qualityCompared':False,
            'unknownReasons':['document_visual_fidelity_not_compared']}
