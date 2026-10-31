"""Cross-cutting ownership, loss evidence and bounded-work regressions."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from save_test_support import verify_download_for_test
from app_backend.converter_guard import ConverterGuard, GuardError, GuardLimits
from app_backend.converter_guard.core import _html_resource_warning
from app_backend.converter_guard.csv_import import inspect_csv_stream
from app_backend.converter_guard.quality import inspect_document, compare_documents, inspect_isolated
from app_backend.converter_guard.result_store import ResultStore
from app_backend.converter_transport import ConverterTransport, Result
from test_converter_guard_v02 import FakeAdapter, wait_finished


def made(guard, name='source.odt', target='pdf', payload=b'x'):
    identity=guard.create([{'id':'one','name':name,'size':len(payload)}],[target])['jobId']
    guard.upload(identity,'one',io.BytesIO(payload),len(payload))
    guard.start(identity,archive_mode='on_demand')
    return identity,wait_finished(guard,identity)


def package(path, kind='odf', *, table=True, image=True, formula=True, text='AIO marker'):
    with zipfile.ZipFile(path,'w') as archive:
        if kind=='odf':
            archive.writestr('mimetype','application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml','<office xmlns:t="urn:table"><p>'+text+'</p>'+
                ('<table><p>cell</p></table>' if table else '')+
                ('<math/>' if formula else '')+'</office>')
            if image:archive.writestr('Pictures/p.png',b'pixels')
        else:
            archive.writestr('word/document.xml','<doc><p>'+text+'</p>'+
                ('<tbl><p>cell</p></tbl>' if table else '')+
                ('<oMath/>' if formula else '')+'</doc>')
            if image:archive.writestr('word/media/p.png',b'pixels')
    return path


class SlotLifecycle(unittest.TestCase):
    def test_only_saved_jobs_reap_and_unsaved_results_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard=ConverterGuard(adapter=FakeAdapter(),limits=GuardLimits(max_jobs=2),recovery_parent=tmp)
            try:
                one,a=made(guard);two,b=made(guard)
                with self.assertRaises(GuardError) as raised:
                    guard.create([{'id':'new','name':'new.odt','size':1}],['pdf'])
                self.assertEqual(raised.exception.code,'JOB_LIMIT')
                self.assertIn('Состояние',raised.exception.message)
                verify_download_for_test(self, guard, a['result']['ticket'])
                guard.confirm_saved(one,a['result']['ticket'])
                self.assertEqual(guard.status(one)['files'][0]['targets'][0]['state'],'saved')
                root=guard._jobs[one].root
                three=guard.create([{'id':'new','name':'new.odt','size':1}],['pdf'])['jobId']
                self.assertFalse(root.exists());self.assertNotIn(one,guard._jobs)
                with guard.open_result(b['result']['ticket']) as (stream,*_):self.assertEqual(stream.read(),b'data')
                self.assertEqual(guard.storage_stats()['jobs'],2)
            finally:guard.close()

    def test_save_reap_waits_for_download_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard=ConverterGuard(adapter=FakeAdapter(),limits=GuardLimits(max_jobs=1),recovery_parent=tmp)
            try:
                identity,state=made(guard);ticket=state['result']['ticket']
                with guard.open_result(ticket):
                    verify_download_for_test(self, guard, ticket)
                    guard.confirm_saved(identity,ticket)
                    with self.assertRaises(GuardError):guard.create([{'id':'x','name':'x.odt','size':0}],['pdf'])
                guard.create([{'id':'x','name':'x.odt','size':0}],['pdf'])
                self.assertNotIn(identity,guard._jobs)
            finally:guard.close()

    def test_production_owns_path_never_waits_for_state_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=tmp)
            entered=threading.Event();leave=threading.Event()
            def hold():
                with guard._lock:entered.set();leave.wait(2)
            t=threading.Thread(target=hold);t.start();self.assertTrue(entered.wait(1))
            try:
                tick=time.monotonic();self.assertTrue(guard.owns_path(Path(tmp)/'anything'))
                self.assertLess(time.monotonic()-tick,.2)
            finally:leave.set();t.join();guard.close()


class OrphanRescue(unittest.TestCase):
    def test_orphan_is_exportable_without_claiming_conversion_or_deleting_unknown_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'recovery';orphan=root/'aio-guard-broken';out=orphan/'work/3/output/assets'
            out.mkdir(parents=True);(out/'image.png').write_bytes(b'rescued')
            (orphan/'input').mkdir();(orphan/'input/private.docx').write_bytes(b'never-export-source')
            guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=root)
            try:
                found=guard.recoverable();self.assertEqual(len(found['jobs']),1)
                job=found['jobs'][0];self.assertTrue(job['salvaged']);self.assertFalse(job['recoveryDurable'])
                state=guard.status(job['jobId']);self.assertEqual(state['resultAvailability'],'recovered_unverified')
                self.assertEqual(state['result']['outputs'][0]['size'],7)
                destination=Path(tmp)/'saved';destination.mkdir()
                outcome=guard.save_folder(state['result']['ticket'],destination)
                self.assertTrue(outcome['saved']);self.assertFalse(outcome['savedAll'])
                outputs=list(destination.rglob('image.png'));self.assertEqual(outputs[0].read_bytes(),b'rescued')
                self.assertFalse(list(destination.rglob('private.docx')))
                self.assertTrue(orphan.exists());self.assertEqual(len(guard.recoverable()['jobs']),1)
                guard.release(job['jobId'],discard=True);self.assertFalse(orphan.exists())
            finally:guard.close()

    def test_corrupt_journal_salvages_uncommitted_outputs_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'recovery';first=ConverterGuard(adapter=FakeAdapter(),recovery_parent=root)
            identity,state=made(first);folder=first._jobs[identity].root
            first.close();(folder/'results.jsonl').write_text('damaged\n')
            extra=folder/'work/7/output';extra.mkdir(parents=True);(extra/'late.dat').write_bytes(b'late')
            outside=Path(tmp)/'private';outside.write_bytes(b'secret')
            try:(extra/'link.dat').symlink_to(outside)
            except OSError:pass
            second=ConverterGuard(adapter=FakeAdapter(),recovery_parent=root)
            try:
                jobs=second.recoverable()['jobs'];self.assertEqual(len(jobs),1)
                rows=second.status(jobs[0]['jobId'])['result']['outputs']
                self.assertEqual({r['name'] for r in rows},{'source.pdf','late.dat'})
                self.assertTrue(jobs[0]['salvaged']);self.assertTrue(outside.exists())
            finally:second.close()

    def test_other_running_instance_keeps_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'aio-guard-locked';(root/'work/0/output').mkdir(parents=True)
            (root/'work/0/output/a.pdf').write_bytes(b'data')
            owner=ResultStore(root);self.assertTrue(owner.acquire())
            guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=tmp)
            try:self.assertEqual(guard.recoverable()['jobs'],[])
            finally:guard.close();owner.close()


class ResourceAndCsvEvidence(unittest.TestCase):
    def test_full_csv_scan_catches_late_rows_encoding_and_type_risks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'large.csv';path.write_text('id,value\n'+'x,1\n'*18000+'broken,2,extra\n',encoding='utf-8')
            value=inspect_csv_stream(path,delimiter=',')
            self.assertTrue(value['complete']);self.assertEqual(value['raggedRows'],1)
            self.assertEqual(value['rows'],18002)
            with path.open('ab') as f:f.write(b'bad,\xff\n')
            self.assertFalse(inspect_csv_stream(path,delimiter=',')['complete'])
            path.write_text('id,value\n001,=SUM(A1)\n02,1e10\n03,2026-10-22\n')
            risks=inspect_csv_stream(path,delimiter=',')['typeRisks']
            self.assertEqual(risks['leadingZeros'],3);self.assertEqual(risks['formulaLike'],1)
            self.assertEqual(risks['scientificLike'],1);self.assertEqual(risks['dateLike'],1)

    def test_csv_limits_are_incomplete_not_fake_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'a.csv';path.write_text('a,b\n1,2\n')
            self.assertEqual(inspect_csv_stream(path,delimiter=',',max_bytes=2)['status'],'incomplete')
            cancel=threading.Event();cancel.set()
            self.assertFalse(inspect_csv_stream(path,delimiter=',',cancel=cancel)['complete'])

    def test_html_css_escape_base_svg_and_file_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'index.html'
            for markup in (r'<style>.x{background:u\72l(\69mages/a.png)}</style>',
                           '<img src="file:///D:/private/a.png">','<base href="../assets/">',
                           '<svg><image href="logo.png"/></svg>',
                           '<img srcset="one.png 1x,two.png 2x">'):
                with self.subTest(markup=markup):
                    path.write_text(markup)
                    self.assertEqual(_html_resource_warning(path),'HTML_LOCAL_RESOURCES_NOT_UPLOADED')


class StructuralEvidence(unittest.TestCase):
    def test_preserved_and_missing_tables_images_formulas_text_are_distinguished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);before=inspect_document(package(root/'a.odt'))
            same=inspect_document(package(root/'b.docx','ooxml'))
            changed=inspect_document(package(root/'c.docx','ooxml',table=False,image=False,formula=False,text='different'))
            self.assertEqual(compare_documents(before,same)['status'],'checked_subset')
            report=compare_documents(before,changed)
            self.assertEqual(report['status'],'risks_found')
            self.assertEqual({x['feature'] for x in report['risks']},{'tables','images','formulas','text'})
            self.assertIn('visual_layout',report['unknown'])

    def test_unsupported_does_not_claim_quality_and_xml_budget_cannot_authorize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=package(root/'a.odt')
            value=inspect_document(source,max_bytes=5)
            self.assertFalse(value['complete']);self.assertFalse(compare_documents(value,value)['qualityCompared'])
            raw=root/'bad.raw';raw.write_bytes(b'unknown')
            self.assertFalse(inspect_document(raw)['complete'])
            with zipfile.ZipFile(source,'a') as f:
                f.writestr('content.xml','<!DOCTYPE p [<!ENTITY x "bad">]><p>&x;</p>')
            self.assertFalse(inspect_document(source)['complete'])

    def test_csv_xlsx_values_compare_literal_zeros_and_do_not_evaluate_formulas(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);csv=root/'a.csv';csv.write_text('id,value\n001,64.01\n')
            book=Workbook();book.active.append(['id','value']);book.active.append(['001','64.01'])
            path=root/'b.xlsx';book.save(path);book.close()
            before=inspect_document(csv);same=inspect_document(path)
            self.assertEqual(compare_documents(before,same)['status'],'checked_subset')
            book=Workbook();book.active.append(['id','value']);book.active.append([1,64.01])
            path=root/'c.xlsx';book.save(path);book.close()
            report=compare_documents(before,inspect_document(path))
            self.assertIn('CELL_VALUES_DIFFER',[r['code'] for r in report['risks']])

    def test_pdf_page_count_and_isolated_worker_are_real(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'two.pdf';writer=PdfWriter()
            writer.add_blank_page(width=100,height=100);writer.add_blank_page(width=100,height=100)
            with source.open('wb') as out:writer.write(out)
            result=inspect_isolated(source,root/'inspect')
            self.assertTrue(result['complete'],result);self.assertEqual(result['counts']['pages'],2)
            self.assertIn('text',result['unknown'])

    def test_quality_is_opt_in_and_failure_never_discards_native_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=tmp)
            try:
                with patch('app_backend.converter_guard.quality.inspect_isolated',side_effect=AssertionError('unexpected')):
                    identity,state=made(guard)
                    self.assertEqual(state['succeeded'],1)
                with self.assertRaises(GuardError):guard.start(identity,quality_check='yes')
                other=guard.create([{'id':'one','name':'source.odt','size':1}],['pdf'])['jobId']
                guard.upload(other,'one',io.BytesIO(b'x'),1)
                with patch('app_backend.converter_guard.quality.inspect_isolated',side_effect=RuntimeError('reader failed')):
                    guard.start(other,quality_check=True)
                    result=wait_finished(guard,other)
                self.assertEqual(result['succeeded'],1)
                self.assertFalse(result['qualityCompared'])
                self.assertEqual(result['files'][0]['targets'][0]['quality']['status'],'not_checked')
            finally:guard.close()


class DirectCleanupConcurrency(unittest.TestCase):
    def owned_result(self, owner):
        # The cleanup behavior is controlled, but the retained file identity
        # is real: production must never accept a made-up path as saveable.
        backing = tempfile.TemporaryDirectory()
        self.addCleanup(backing.cleanup)
        owner.name = str(Path(backing.name).resolve())
        path = Path(owner.name) / 'file.pdf'
        path.write_bytes(b'owned result')
        return Result(owner, path, path.name, 'application/pdf', size=path.stat().st_size)

    def test_failed_cleanup_schedules_only_bounded_retries_and_close_cancels_timer(self):
        callbacks=[]
        class Timer:
            def __init__(self,delay,work):self.work=work;self.cancelled=False;callbacks.append(self)
            def start(self):pass
            def cancel(self):self.cancelled=True
        class Busy:
            def cleanup(self):raise PermissionError('sharing violation')
        transport=ConverterTransport()
        transport.results['ticket']=self.owned_result(Busy())
        with patch('app_backend.converter_transport.threading.Timer',Timer):
            self.assertFalse(transport.release('ticket'))
            for _ in range(5):
                if transport._cleanup_timer:transport._cleanup_timer.work()
            self.assertEqual(len(callbacks),3)
            self.assertEqual(transport.storage_stats()['cleanupPending'],1)
            transport.close()
            self.assertIsNone(transport._cleanup_timer)

    def test_slow_cleanup_holds_no_transport_lock_and_lease_prevents_early_delete(self):
        entered=threading.Event();leave=threading.Event()
        class Slow:
            calls=0
            def cleanup(self):self.calls+=1;entered.set();leave.wait(2)
        transport=ConverterTransport();temporary=Slow()
        transport.results['ticket']=self.owned_result(temporary)
        with transport.lease('ticket'):
            self.assertFalse(transport.release('ticket'));self.assertEqual(temporary.calls,0)
        # Lease release does cleanup outside the lock; use a new result to hold
        # an actual deletion barrier while unrelated status can still respond.
        temporary=Slow();entered.clear();transport.results['two']=self.owned_result(temporary)
        thread=threading.Thread(target=lambda:transport.release('two'));thread.start()
        try:
            self.assertTrue(entered.wait(1));tick=time.monotonic()
            self.assertIsNone(transport.progress('unrelated'))
            self.assertLess(time.monotonic()-tick,.2)
            self.assertFalse(transport.release('two'));self.assertEqual(temporary.calls,1)
        finally:leave.set();thread.join();transport.close()




class RecoveryCapacityAndChartProof(unittest.TestCase):
    def test_recovery_loads_at_most_slot_limit_and_next_folder_remains_accessible(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=ConverterGuard(adapter=FakeAdapter(),limits=GuardLimits(max_jobs=4),recovery_parent=tmp)
            for _ in range(3):made(old)
            old.close()
            current=ConverterGuard(adapter=FakeAdapter(),limits=GuardLimits(max_jobs=2),recovery_parent=tmp)
            try:
                first=current.recoverable();self.assertEqual(len(first['jobs']),2)
                self.assertEqual(first['pendingRecoveryFolders'],1)
                identity=first['jobs'][0]['jobId'];ticket=current.status(identity)['result']['ticket']
                verify_download_for_test(self, current, ticket)
                current.confirm_saved(identity,ticket)
                second=current.recoverable();self.assertEqual(len(second['jobs']),2)
                self.assertEqual(second['pendingRecoveryFolders'],0)
                self.assertNotIn(identity,[j['jobId'] for j in second['jobs']])
            finally:current.close()

    def test_chart_is_classified_by_native_other_plus_exact_package_without_admitting_pdf(self):
        from app_backend.converter_guard.preflight_native import _odf_chart
        from app_backend.converter_guard.routes import Registry
        from test_converter_guard_v04 import node,component,W
        chart='com.sun.star.chart2.ChartDocument'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'renamed.unknown'
            with zipfile.ZipFile(source,'w') as z:
                z.writestr('mimetype','application/vnd.oasis.opendocument.chart')
                z.writestr('content.xml','<o:document-content xmlns:o="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:c="urn:oasis:names:tc:opendocument:xmlns:chart:1.0"><o:body><o:chart><c:chart/></o:chart></o:body></o:document-content>')
            self.assertTrue(_odf_chart(source))
            registry=root/'registry.xcd'
            registry.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">'+
                component('Types','Types',node('odc',Extensions='odc')+node('pdf',Extensions='pdf'))+
                component('Filter','Filters',node('chart8',Type='odc',DocumentService=chart,Flags='IMPORT EXPORT')+
                          node('writer_pdf',Type='pdf',DocumentService=W,Flags='EXPORT'))+'</oor:data>')
            native=Registry(((str(registry),0,registry.stat().st_size),),registry)
            proof={'state':'detected','basis':'native_preflight_chart_content','nativeKind':4,
                   'chartPackage':True,'documentServices':[chart],'registryFingerprint':native.fingerprint}
            self.assertEqual(native.observed_decision('odc',proof),'registered')
            self.assertEqual(native.observed_decision('pdf',proof),'unavailable')
            self.assertEqual(native.observed_decision('pdf',{**proof,'chartPackage':False}),'undetermined')
            self.assertEqual(native.observed_decision('odc',{**proof,'nativeKind':True}),'undetermined')


if __name__=='__main__':unittest.main()
