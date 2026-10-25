"""Disposable analysis process; no conversion or settings mutation here."""
import json
import hashlib
from pathlib import Path
import sys
import time
import os


def main(request_path):
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
        (root/'preview.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
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
        (root/'preview.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        return
    if request.get('mode') == 'preview':
        result = preview_text(source)
        (root/'preview.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        return
    with source.open('rb') as original:
        actual = hashlib.file_digest(original, 'sha256').hexdigest()
    verified = actual == request.get('sourceSha256')
    evidence = collect_source_summary(source, cancel=cancel) if verified else None
    with (root/'outcomes.jsonl').open('x', encoding='utf-8') as stream:
        for pair in request['pairs']:
            while (root/'pause').exists(): time.sleep(.1)
            unchanged = True
            for name in pair['paths']:
                expected = pair.get('sha256', {}).get(name)
                with Path(name).open('rb') as output:
                    observed = hashlib.file_digest(output, 'sha256').hexdigest()
                if expected and observed != expected: unchanged = False
            outcome = (analyze_pair(source, [Path(p) for p in pair['paths']], pair['target'],
                                   cancel=cancel, source_evidence=evidence) if verified and unchanged else
                       {'status':'not_checked','findings':[{'code':'SOURCE_CHANGED' if not verified else 'RESULT_CHANGED','category':'not_checked',
                        'message':'Служебный файл изменился после конвертации. Причину нельзя приписать конвертеру.'}],
                        'checkedFeatures':[],'unknown':['source_identity'],'recipeCandidates':[]})
            stream.write(json.dumps({'taskId':pair['taskId'],'outcome':outcome}, ensure_ascii=False, allow_nan=False)+'\n')
            stream.flush()


if __name__ == '__main__':
    main(sys.argv[1])
