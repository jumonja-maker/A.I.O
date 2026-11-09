"""Production Qt shell/HTTP/Recovery/save; synthetic conversion and file pickers.

This source GUI is NOT the installed Windows app or an OS DPI acceptance run.
"""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog
from app_backend.desktop import MainWindow
from app_backend.converter_guard import ConverterGuard
from test_converter_guard_v02 import FakeAdapter, wait_finished
from test_converter_native_dom import NativeUIDOMAcceptance


def main():
    app = QApplication.instance() or QApplication([])
    fixture = NativeUIDOMAcceptance('test_ui_contract'); fixture.setUp()
    evidence = {'scope':'production source Qt shell/HTTP/save; fake conversion and native pickers',
                'installedGUI':'NOT_TESTED', 'systemDPI':'NOT_TESTED', 'cases':[], 'status':'RUNNING'}
    window = None

    def pump(seconds=.03):
        end = time.monotonic()+seconds
        while time.monotonic()<end:
            app.processEvents(); time.sleep(.003)

    def js(expression):
        answers=[]
        window.page.runJavaScript('JSON.stringify((()=>{return ('+expression+');})())', answers.append)
        until=time.monotonic()+8
        while not answers and time.monotonic()<until: pump(.01)
        if not answers or not answers[0]: raise AssertionError('No WebEngine callback: '+expression[:100])
        return json.loads(answers[0])

    def wait(expression):
        until=time.monotonic()+15
        while time.monotonic()<until:
            if js(expression): return
            pump()
        raise AssertionError('GUI timeout: '+expression+' / '+str(js('document.body.innerText')).strip()[:1500])

    def click(selector):
        js('(()=>{document.querySelector('+json.dumps(selector)+').click();return true;})()')

    try:
        identities=[]
        for name in ('ZIP', 'Folder'):
            producer=ConverterGuard(adapter=FakeAdapter(), recovery_parent=fixture.server.guard._recovery)
            try:
                identity=producer.create([{'id':'f','name':name+'_'+('Длинное имя '*16)+'.docx','size':4}],
                                         ['pdf','html','bad'], retain_sources=True)['jobId']
                producer.upload(identity,'f',io.BytesIO(b'keep'),4)
                producer.start(identity,archive_mode='on_demand');wait_finished(producer,identity)
                identities.append(identity)
            finally: producer.close()
        window=MainWindow(fixture.server,fixture.server.server_port);window.show()
        wait("!!document.querySelector('.nav-item')")
        js("(()=>{[...document.querySelectorAll('.nav-item')].find(n=>n.textContent.includes('Конвертер')).click();return true;})()")
        wait("!!document.querySelector('.aio-guard-state-button')")
        assert not fixture.server.guard._recovery_loaded, 'startup scanned old results'
        click('.aio-guard-state-button'); click('[data-g-scan]')
        wait("document.querySelectorAll('[data-g-jobs] option').length===2")
        wait("!document.querySelector('[data-g-scan]').disabled||document.querySelector('[data-g-scan-status]').textContent.includes('завершён')")
        for theme in ('light','dark'):
            if js("document.documentElement.dataset.theme||'light'")!=theme:
                click('[data-g-close]');click('#theme-toggle');click('.aio-guard-state-button')
            for width,height,zoom in ((1024,700,1),(900,650,1.25),(800,600,1.5),(640,480,2)):
                window.resize(width,height);window.view.setZoomFactor(zoom);pump(.12)
                metric=js("""(()=>{
                  const d=document.querySelector('.aio-guard-popup'),r=d.getBoundingClientRect();
                  return {viewport:[innerWidth,innerHeight],bounds:[r.left,r.top,r.right,r.bottom],
                    overflow:d.scrollWidth>d.clientWidth+2,open:d.open};
                })()""")
                assert metric['open'] and not metric['overflow'], metric
                left,top,right,bottom=metric['bounds'];vw,vh=metric['viewport']
                assert left>=-1 and top>=-1 and right<=vw+1 and bottom<=vh+1, metric
                for selector in ('[data-g-jobs]','[data-g-folder]','[data-g-zip]','[data-g-discard]','[data-g-close]'):
                    hit=js('''(()=>{const e=document.querySelector('''+json.dumps(selector)+''');
                      e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect(),h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                      return h===e||e.contains(h);})()''')
                    assert hit, (selector,metric)
                js("(()=>{document.querySelector('[data-g-close]').focus();return true;})()")
                keyboard = window.view.focusProxy() or window.view
                QTest.keyClick(keyboard,Qt.Key.Key_Tab);pump(.1)
                assert js("document.querySelector('.aio-guard-popup').contains(document.activeElement)"), 'Tab escaped modal'
                QTest.keyClick(keyboard,Qt.Key.Key_Escape);pump(.1)
                assert not js("document.querySelector('.aio-guard-popup').open"), 'Escape failed'
                assert js("document.activeElement===document.querySelector('.aio-guard-state-button')"), 'focus not restored'
                click('.aio-guard-state-button');pump()
                evidence['cases'].append({'theme':theme,'window':[width,height],'zoom':zoom,
                                          'viewport':metric['viewport'],'status':'PASS','keyboardAndScroll':True})
        window.resize(1024,700);window.view.setZoomFactor(1);pump()
        with tempfile.TemporaryDirectory() as external:
            target=Path(external)/'external.zip'
            def choose(identity):
                js('''(()=>{const s=document.querySelector('[data-g-jobs]');s.value='''+json.dumps(identity)+''';
                  s.dispatchEvent(new Event('change'));return true;})()''')
            choose(identities[0]);pump()
            with patch.object(QFileDialog,'getSaveFileName',return_value=(str(target),'ZIP')):
                click('[data-g-zip]')
                wait("document.querySelector('[data-g-current]').textContent.includes('Сохранено файлов')")
            receipt=json.loads(target.with_name(target.name+'.AIO-save-status.json').read_text())
            assert receipt['state']=='completed' and receipt['readbackVerified'] and receipt['metadataVerified']==3, receipt
            with zipfile.ZipFile(target) as archive: assert archive.testzip() is None
            assert all(a.saved for a in fixture.server.guard._jobs[identities[0]].artifacts)
            choose(identities[1]);pump()
            with patch.object(QFileDialog,'getExistingDirectory',return_value=external):
                click('[data-g-folder]')
                wait("document.querySelector('[data-g-current]').textContent.includes('Сохранено файлов')")
            status=json.loads(next(Path(external).glob('AIO_Results_*/AIO-save-status.json')).read_text())
            assert status['state']=='completed' and status['readbackVerified'],status
            assert all(a.saved for a in fixture.server.guard._jobs[identities[1]].artifacts)
            evidence.update(zipSave='PASS',folderSave='PASS',manualRecovery='PASS',
                            crcAndMetadataReceipt='PASS',conversionErrorsPreserved=True)
        evidence['status']='PASS'
    except Exception as error:
        evidence.update(status='FAIL',error=str(error))
        raise
    finally:
        print('AIO_RECOVERY_SOURCE_GUI='+json.dumps(evidence,ensure_ascii=True),flush=True)
        if window:
            # Fixture owns the server shutdown; release only these Qt objects.
            # Destroy the engine page before its profile, then drain deferred
            # QObject deletion before QApplication's Python wrapper disappears.
            window.profile.setParent(None)
            window.view.setPage(None)
            window.page.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            window.profile.deleteLater()
            window._shutdown_done=True;window.close();window.deleteLater();pump(.1)
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        fixture.tearDown()


if __name__=='__main__':main()
