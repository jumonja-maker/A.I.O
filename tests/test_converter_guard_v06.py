"""Preflight completes and excludes impossible work before export starts."""
import io
from pathlib import Path
import tempfile
import threading
import unittest

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.routes import registry
from test_converter_guard_v02 import wait_finished, FakeAdapter
from test_converter_guard_v04 import write_registry, D, W, C


class PreflightAdapter(LibreOfficeAdapter):
    def __init__(self, executable):
        super().__init__(executable)
        self.inspections, self.exports = [], []
        self.waiting, self.resume = threading.Event(), threading.Event()

    def preflight(self, source, work, *, cancel, timeout):
        self.inspections.append(source.name)
        value = source.read_bytes()
        if value == b'HOLD':
            self.waiting.set()
            while not self.resume.wait(.01):
                if cancel.is_set():
                    raise GuardError('CANCELLED', 'Stopped.', stage='preflight')
        if value not in (b'D', b'W', b'C'):
            return {'state':'undetermined','code':'PREFLIGHT_IMPORT_UNDETERMINED'}
        return {'state':'detected','documentServices':[{'D':D,'W':W,'C':C}[value.decode()]],
                'basis':'native_preflight_document_kind',
                'registryFingerprint':registry(self.executable).fingerprint,'exported':False}

    def convert(self, source, target, workspace, *, on_document_service=None, **kwargs):
        self.exports.append((source.name,target))
        # Preserve the fake's independent failure/result behavior; no native
        # type proof is learned through export in these preflight contracts.
        return FakeAdapter().convert(source,target,workspace,**kwargs)


class GuardPreflightContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        exe,_ = write_registry(self.temp.name)
        self.adapter = PreflightAdapter(exe)
        self.guard = ConverterGuard(adapter=self.adapter,spool_parent=self.temp.name)

    def tearDown(self):
        self.guard.close()
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],0)
        self.temp.cleanup()

    def prepare(self, sources):
        job=self.guard.create([{'id':str(i),'name':name,'size':len(value)}
            for i,(name,value) in enumerate(sources)],prepare_only=True)
        for i,(_,value) in enumerate(sources):
            self.guard.upload(job['jobId'],str(i),io.BytesIO(value),len(value))
        return job['jobId']

    def test_three_files_have_real_per_file_routes_without_any_export(self):
        identity=self.prepare([('picture.bmp',b'D'),('renamed.bmp',b'W'),('table.xlsx',b'C')])
        report=self.guard.preflight(identity)
        dbf=next(f for f in report['formats'] if f['id']=='dbf')
        self.assertEqual(dbf['verifiedFileIds'],['2'])
        self.assertEqual(dbf['selectableFileIds'],['2'])
        docx=next(f for f in report['formats'] if f['id']=='docx')
        self.assertEqual(docx['verifiedFileIds'],['1'])
        self.assertEqual(self.adapter.exports,[])
        state=self.guard.status(identity,include_trace=True)
        self.assertEqual(state['state'],'prepared')
        self.assertEqual(state['total'],0)
        self.assertEqual(state['attempted'],0)
        self.assertFalse(any(e['libreOfficeStarted'] for e in state['trace']['events']))

    def test_impossible_pairs_are_already_skipped_in_the_start_response(self):
        identity=self.prepare([('image.bmp',b'D'),('word.docx',b'W'),('sheet.xlsx',b'C')])
        self.guard.preflight(identity)
        state=self.guard.start(identity,file_ids=['0','1','2'],formats=['dbf','docx','pdf','png'],only_verified=True)
        # DBF is deliberately first: the old system had to fail it before
        # learning the image model. It must now be excluded before any export.
        task=state['files'][0]['targets'][0]
        self.assertEqual((task['state'],task['stage'],task['libreOfficeStarted']),
                         ('skipped','preflight',False))
        done=wait_finished(self.guard,identity)
        self.assertEqual(done['failed'],0)
        self.assertEqual(done['attempted'],6)
        self.assertEqual(done['filtered'],6)
        self.assertNotIn(('image.bmp','dbf'),self.adapter.exports)
        self.assertNotIn(('word.docx','dbf'),self.adapter.exports)

    def test_only_selected_files_are_started_without_reupload(self):
        identity=self.prepare([('image.bmp',b'D'),('word.docx',b'W')])
        self.guard.preflight(identity)
        self.guard.start(identity,file_ids=['1'],formats=['docx'],only_verified=True)
        done=wait_finished(self.guard,identity)
        self.assertEqual([f['fileId'] for f in done['files']],['1'])
        self.assertEqual(self.adapter.inspections,['image.bmp','word.docx'])
        self.assertEqual(self.adapter.exports,[('word.docx','docx')])

    def test_unknown_source_is_not_automatically_scheduled_or_declared_unsupported(self):
        identity=self.prepare([('unknown.custom',b'?'),('word.docx',b'W')])
        info=self.guard.preflight(identity)
        pdf=next(f for f in info['formats'] if f['id']=='pdf')
        self.assertEqual(pdf['verifiedFileIds'],['1'])
        self.assertEqual(pdf['undeterminedFileIds'],['0'])
        self.assertNotIn('0',pdf['selectableFileIds'])
        self.guard.start(identity,file_ids=['0','1'],formats=['pdf'],only_verified=True)
        done=wait_finished(self.guard,identity)
        self.assertEqual(done['files'][0]['state'],'skipped')
        self.assertEqual(done['files'][0]['targets'][0]['code'],'PREFLIGHT_ROUTE_UNDETERMINED')
        self.assertEqual(self.adapter.exports,[('word.docx','pdf')])

    def test_unknown_source_cannot_bypass_preflight_with_legacy_flag(self):
        identity=self.prepare([('unknown.custom',b'?')])
        self.guard.preflight(identity)
        self.guard.start(identity,file_ids=['0'],formats=['pdf'],only_verified=False)
        done=wait_finished(self.guard,identity)
        self.assertEqual(done['succeeded'],0)
        self.assertEqual(done['filtered'],1)
        self.assertEqual(self.adapter.exports,[])

    def test_invalid_start_does_not_consume_the_prepared_session(self):
        identity=self.prepare([('word.docx',b'W')])
        self.guard.preflight(identity)
        for params in ({'file_ids':['absent'],'formats':['pdf']},
                       {'file_ids':['0'],'formats':[]},
                       {'file_ids':['0'],'formats':['pdf'],'only_verified':'true'}):
            with self.assertRaises(GuardError):self.guard.start(identity,**params)
            self.assertEqual(self.guard.status(identity)['state'],'prepared')
        self.assertEqual(self.adapter.exports,[])

    def test_export_cannot_start_while_the_preflight_import_is_running(self):
        identity=self.prepare([('hold.docx',b'HOLD')])
        errors=[]
        def inspect():
            try:self.guard.preflight(identity)
            except GuardError as e:errors.append(e.code)
        worker=threading.Thread(target=inspect);worker.start()
        self.assertTrue(self.adapter.waiting.wait(2))
        with self.assertRaises(GuardError):
            self.guard.start(identity,file_ids=['0'],formats=['pdf'])
        self.guard.release(identity);worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,['CANCELLED'])
        self.assertEqual(self.adapter.exports,[])
        self.assertEqual(self.guard.storage_stats()['jobs'],0)

    def test_preflight_native_state_is_never_accepted_from_frontend_metadata(self):
        job=self.guard.create([{'id':'x','name':'image.bmp','size':1,
            'nativeRoute':{'documentServices':[C],'basis':'native_preflight_document_kind'}}],prepare_only=True)
        self.assertIsNone(job['files'][0]['nativeRoute'])

    def test_adding_and_removing_files_reuses_completed_native_inspections(self):
        identity=self.prepare([('first.bmp',b'D'),('word.docx',b'W')])
        self.guard.preflight(identity)
        self.guard.update_prepared(identity,[{'id':'1','name':'word.docx','size':1},
                                             {'id':'2','name':'table.xlsx','size':1}])
        self.guard.upload(identity,'2',io.BytesIO(b'C'),1)
        info=self.guard.preflight(identity)
        self.assertEqual(self.adapter.inspections,['first.bmp','word.docx','table.xlsx'])
        self.assertEqual([f['id'] for f in info['files']],['1','2'])
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],2)
        self.assertEqual(self.adapter.exports,[])

    def test_preflight_proof_avoids_export_observer_and_registry_change_invalidates_it(self):
        identity=self.prepare([('word.docx',b'W')])
        info=self.guard.preflight(identity)
        proof=info['files'][0]['preflight']
        self.assertFalse(self.adapter.observation_needed(proof))
        self.assertEqual(self.adapter.route_decision('pdf',proof),'registered')
        self.assertTrue(self.adapter.observation_needed({**proof,'registryFingerprint':'stale'}))
        self.assertEqual(self.adapter.route_decision('pdf',{**proof,'registryFingerprint':'stale'}),
                         'undetermined')


if __name__=='__main__':unittest.main()
