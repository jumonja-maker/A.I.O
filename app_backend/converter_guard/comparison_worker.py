"""Disposable analysis process; no conversion or settings mutation here."""
import json
import hashlib
from pathlib import Path
import sys
import time
import os

MAX_WORKER_MEMORY = 768 * 1024 * 1024


def _fingerprint(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _stamp(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _write_preview(root, source, request, result):
    # Decoders open the path themselves. Check again after they finish, so a
    # changed file can never be shown as the bytes identified by an old report.
    expected = request.get('expectedSha256') or request.get('_previewObservedSha256')
    try:
        unchanged = expected is not None and _fingerprint(source) == expected
    except OSError:
        unchanged = False
    if not unchanged:
        (root / 'preview.png').unlink(missing_ok=True)
        result = {'kind': 'unavailable',
            'code': 'SOURCE_CHANGED' if request.get('previewSide') == 'source' else 'RESULT_CHANGED',
            'note': 'Файл изменился во время просмотра. Запустите анализ заново.'}
    if unchanged and request.get('previewSide') == 'result' and request.get('expectedSha256') is None:
        result['provenance'] = {'comparisonScope': 'current_bytes', 'originalExportIdentity': 'unavailable'}
        result['note'] = (result.get('note', '') + ' Показаны текущие байты; SHA первоначального результата неизвестен.').strip()
    (root / 'preview.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def _not_checked_identity(code):
    return {'status': 'not_checked', 'findings': [{'code': code, 'category': 'not_checked',
        'message': 'Служебный файл изменился после конвертации. Причину нельзя приписать конвертеру.'}],
        'checkedFeatures': [], 'unknown': ['source_identity' if code == 'SOURCE_CHANGED' else 'result_identity'],
        'recipeCandidates': []}


def _with_export_provenance(outcome, *, historical_identity):
    """Separate facts about current bytes from attribution to an old export."""
    outcome = dict(outcome)
    outcome['provenance'] = {
        'comparisonScope': 'retained_export_bytes' if historical_identity else 'current_bytes',
        'originalExportIdentity': 'verified' if historical_identity else 'unavailable'}
    if historical_identity:
        return outcome
    outcome['unknown'] = list(dict.fromkeys([*outcome.get('unknown', ()), 'original_export_identity']))
    findings = []
    for value in outcome.get('findings', ()):
        finding = dict(value)
        if finding.get('category') == 'confirmed_loss':
            finding['observedCategory'] = 'confirmed_loss'
            finding['category'] = 'uncertain'
            finding['certainty'] = 'conversion_attribution_unverified'
            finding['message'] = (finding.get('message', '') +
                ' Это различие текущих байтов. SHA результата при первоначальном экспорте не сохранён; '
                'приписать изменение конвертации нельзя.')
        findings.append(finding)
    outcome['findings'] = findings
    if outcome.get('status') == 'confirmed_loss':
        outcome['observedStatus'] = 'confirmed_loss'
        outcome['status'] = 'uncertain'
    # A historical candidate cannot be assessed against an unidentified export.
    outcome['recipeCandidates'] = []
    return outcome


def _apply_memory_budget(limit=MAX_WORKER_MEMORY):
    """Bound only disposable inspection workers, never LibreOffice exports.

    Windows receives an opt-in process commit limit from its parent's Job
    Object. POSIX sets its data limit here, after spawning, avoiding unsafe
    preexec callbacks in the multithreaded application.
    """
    if os.name != 'nt':
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
        desired = min(limit, hard) if hard != resource.RLIM_INFINITY else limit
        if soft != resource.RLIM_INFINITY:
            desired = min(desired, soft)
        resource.setrlimit(resource.RLIMIT_DATA, (desired, hard))


def main(request_path):
    _apply_memory_budget()
    from .comparison_analysis import collect_source_summary, analyze_pair, preview_text
    request_path = Path(request_path)
    if request_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('comparison_request_budget')
    request = json.loads(request_path.read_text(encoding='utf-8'))
    root = request_path.parent
    class Cancellation:
        def is_set(self): return False  # Parent owns the killable process deadline.
    cancel = Cancellation()
    source = Path(request['source'])
    if request.get('mode') in ('preview','pdf_preview','image_preview'):
        expected = request.get('expectedSha256')
        if expected is not None:
            import re
            if not isinstance(expected,str) or re.fullmatch(r'[0-9a-f]{64}',expected) is None:
                raise ValueError('invalid_preview_fingerprint')
        actual = _fingerprint(source)
        if expected is None:
            request['_previewObservedSha256'] = actual
        else:
            if actual != expected:
                code = 'SOURCE_CHANGED' if request.get('previewSide') == 'source' else 'RESULT_CHANGED'
                result = {'kind':'unavailable','code':code,
                    'note':'Файл изменился после конвертации. Сохранённый отчёт относится к прежним байтам; запустите анализ заново.'}
                (root/'preview.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
                return
    if request.get('mode') == 'image_preview':
        if os.name != 'nt' and not os.environ.get('DISPLAY'):
            os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtGui import QGuiApplication, QImageReader
        from PySide6.QtCore import QSize
        app = QGuiApplication.instance() or QGuiApplication([])
        QImageReader.setAllocationLimit(64)  # MiB, disposable viewer only.
        reader = QImageReader(str(source))
        reader.setAutoTransform(True)
        size = reader.size()
        result = {'kind':'unavailable','note':'Изображение не удалось показать в пределах ресурсов просмотра. Конвертация и сохранение доступны.'}
        if size.width() > 0 and size.height() > 0 and size.width()*size.height() <= 100_000_000:
            factor = min(1,1400/size.width(),1800/size.height())
            if factor < 1:
                reader.setScaledSize(QSize(max(1,int(size.width()*factor)),max(1,int(size.height()*factor))))
            image = reader.read()
            if not image.isNull():
                # Auto-transform may swap axes after the decoder's scaling.
                from PySide6.QtCore import Qt
                if image.width() > 1400 or image.height() > 1800:
                    image = image.scaled(1400,1800,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
                if image.save(str(root/'preview.png')):
                    result = {'kind':'image','note':'Уменьшенный просмотр изображения; для анимации показан первый кадр.'}
        _write_preview(root, source, request, result)
        return
    if request.get('mode') == 'pdf_preview':
        if os.name != 'nt' and not os.environ.get('DISPLAY'):
            os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtGui import QGuiApplication, QImage, QPainter
        from PySide6.QtCore import QSize
        from PySide6.QtPdf import QPdfDocument
        app = QGuiApplication.instance() or QGuiApplication([])
        document = QPdfDocument(None)
        document.load(str(source))
        count = document.pageCount()
        page = request.get('page',0)
        result = {'kind':'unavailable','note':'Не удалось отобразить выбранную страницу PDF.'}
        if type(page) is int and 0 <= page < count:
            points = document.pagePointSize(page)
            if points.width() > 0 and points.height() > 0:
                factor = min(1400/points.width(),1800/points.height())
                image = document.render(page,QSize(max(1,int(points.width()*factor)),max(1,int(points.height()*factor))))
                if not image.isNull():
                    # QtPdf may leave the page background transparent. PDF paper
                    # stays white in both app themes; preserve real image alpha elsewhere.
                    paper = QImage(image.size(), QImage.Format.Format_RGB32)
                    paper.fill(0xffffffff)
                    painter = QPainter(paper)
                    painter.drawImage(0,0,image)
                    painter.end()
                    image = paper
                if not image.isNull() and image.save(str(root/'preview.png')):
                    result = {'kind':'image','pages':count,'page':page,'note':'Постраничный просмотр PDF через QtPdf.'}
        document.close()
        _write_preview(root, source, request, result)
        return
    if request.get('mode') == 'preview':
        result = preview_text(source)
        _write_preview(root, source, request, result)
        return
    with source.open('rb') as original:
        actual = hashlib.file_digest(original, 'sha256').hexdigest()
    verified = actual == request.get('sourceSha256')
    evidence = collect_source_summary(source, cancel=cancel) if verified else None
    # Bind the reusable summary to the expected bytes too. Subsequent pairs
    # reuse it instead of re-reading/re-hashing a large source for every target.
    verified = verified and _fingerprint(source) == request.get('sourceSha256')
    source_stamp = _stamp(source) if verified else None
    with (root/'outcomes.jsonl').open('x', encoding='utf-8') as stream:
        for pair in request['pairs']:
            while (root/'pause').exists(): time.sleep(.1)
            unchanged, observed_outputs = True, {}
            historical_identity = bool(pair['paths']) and all(
                isinstance(pair.get('sha256', {}).get(name), str) and
                len(pair['sha256'][name]) == 64 and
                all(c in '0123456789abcdef' for c in pair['sha256'][name])
                for name in pair['paths'])
            try:
                for name in pair['paths']:
                    expected = pair.get('sha256', {}).get(name)
                    observed = _fingerprint(Path(name))
                    observed_outputs[name] = observed
                    if expected and observed != expected: unchanged = False
            except OSError:
                unchanged = False
            outcome = (analyze_pair(source, [Path(p) for p in pair['paths']], pair['target'],
                                   cancel=cancel, source_evidence=evidence) if verified and unchanged else
                       _not_checked_identity('SOURCE_CHANGED' if not verified else 'RESULT_CHANGED'))
            # A reader may have observed a replacement after the first hash.
            # Revalidate every output after inspection before publishing proof.
            if verified and unchanged:
                try:
                    unchanged = all(_fingerprint(Path(name)) == observed_outputs[name]
                                    for name in pair['paths'])
                except OSError:
                    unchanged = False
                if not unchanged:
                    outcome = _not_checked_identity('RESULT_CHANGED')
                try:
                    source_unchanged = _stamp(source) == source_stamp
                except OSError:
                    source_unchanged = False
                if not source_unchanged:
                    outcome = _not_checked_identity('SOURCE_CHANGED')
            if verified and unchanged and outcome.get('status') != 'not_checked':
                outcome = _with_export_provenance(outcome, historical_identity=historical_identity)
            stream.write(json.dumps({'taskId':pair['taskId'],'outcome':outcome}, ensure_ascii=False, allow_nan=False)+'\n')
            stream.flush()


if __name__ == '__main__':
    main(sys.argv[1])
