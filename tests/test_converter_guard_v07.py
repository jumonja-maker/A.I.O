"""Strict admission, actual UNO response shape and content-keyed reuse."""
import io
import tempfile
from types import SimpleNamespace
import unittest

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.preflight_uno import UNO_WORKER_SOURCE
from app_backend.converter_guard.routes import registry
from test_converter_guard_v02 import wait_finished
from test_converter_guard_v04 import write_registry, node, component, W, C
from test_converter_guard_v06 import PreflightAdapter


class GuardV07Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.exe, self.registry_file = write_registry(self.temp.name)
        self.adapter = PreflightAdapter(self.exe)
        self.guard = ConverterGuard(adapter=self.adapter, spool_parent=self.temp.name)

    def tearDown(self):
        self.guard.close()
        self.assertEqual(self.guard.storage_stats()['reservedBytes'], 0)
        self.assertFalse(self.guard._proof_cache)
        self.temp.cleanup()

    def prepare(self, content=b'C', name='table.csv', file_id='x'):
        job = self.guard.create([{'id':file_id,'name':name,'size':len(content)}], prepare_only=True)
        self.guard.upload(job['jobId'], file_id, io.BytesIO(content), len(content))
        return job['jobId']

    def test_preflight_retains_the_filter_and_type_used_by_the_loaded_model(self):
        namespace = {'__name__':'uno_worker_contract'}
        exec(compile(UNO_WORKER_SOURCE, 'uno_worker_contract', 'exec'), namespace)
        args = (SimpleNamespace(Name='FilterName', Value='Calc CSV'),
                SimpleNamespace(Name='TypeName', Value='generic_Text'))
        model = SimpleNamespace(getArgs=lambda: args)
        self.assertEqual(namespace['model_metadata'](model),
                         {'typeName':'generic_Text','filterName':'Calc CSV'})
        model.getArgs = lambda: ()
        self.assertEqual(namespace['model_metadata'](model), {'typeName':None,'filterName':None})

    def test_shared_text_type_cannot_admit_both_writer_and_calc_exports(self):
        xml = self.registry_file.read_text()
        extra = component('Types','Types',node('generic_Text',Extensions='csv txt'))
        extra += component('Filter','Filters',
            node('Calc CSV',Type='generic_Text',DocumentService=C,Flags='IMPORT EXPORT') +
            node('Writer Text',Type='generic_Text',DocumentService=W,Flags='IMPORT EXPORT'))
        self.registry_file.write_text(xml.replace('</oor:data>',extra+'</oor:data>'))
        native = registry(self.exe)
        self.assertEqual(native.services_for_type('generic_Text'), ())
        self.assertEqual(native.services_for_type('generic_Text','Calc CSV'), (C,))
        proof = {'state':'detected','basis':'native_preflight_uno_type',
                 'typeName':'generic_Text','filterName':'Calc CSV','documentServices':[C],
                 'registryFingerprint':native.fingerprint}
        self.assertEqual(native.observed_decision('docx',proof),'unavailable')
        self.assertEqual(native.observed_decision('dbf',proof),'registered')
        self.assertEqual(native.observed_decision('docx',{**proof,'documentServices':[W]}),'undetermined')
        self.assertEqual(native.services_for_type('generic_Text','writer_odt'), ())

    def test_unknown_metadata_and_unknown_content_never_enable_a_direction(self):
        hint = self.guard.capabilities([{'id':'x','name':'table.csv','size':1}])
        self.assertFalse(any(f['enabled'] or f['selectableFileIds'] for f in hint['formats']))
        identity = self.prepare(b'?')
        report = self.guard.preflight(identity)
        self.assertFalse(any(f['enabled'] or f['nativeAttemptAvailable'] or f['selectableFileIds']
                             for f in report['formats']))
        self.guard.start(identity,file_ids=['x'],formats=['pdf'],only_verified=False)
        done = wait_finished(self.guard,identity)
        self.assertEqual(done['attempted'],0)
        self.assertEqual(done['filtered'],1)
        self.assertEqual(self.adapter.exports,[])

    def test_proof_reused_after_conversion_and_release_without_retaining_source(self):
        identity = self.prepare()
        self.guard.preflight(identity)
        self.guard.start(identity,file_ids=['x'],plan=[{'fileId':'x','formats':['dbf','pdf']}])
        done = wait_finished(self.guard,identity)
        self.assertEqual((done['total'],done['succeeded'],done['filtered']),(2,2,0))
        self.guard.release(identity)
        self.assertEqual(self.guard.storage_stats()['reservedBytes'],0)
        again = self.prepare(file_id='new_id')
        report = self.guard.preflight(again)
        self.assertEqual(report['preflightCacheHits'],1)
        self.assertEqual(report['preflightChecks'],0)
        self.assertEqual(self.adapter.inspections,['table.csv'])

    def test_same_name_and_size_with_changed_content_is_not_a_cache_hit(self):
        self.guard.preflight(self.prepare())
        identity = self.prepare(b'W')
        report = self.guard.preflight(identity)
        self.assertEqual(report['preflightCacheHits'],0)
        self.assertEqual(report['preflightChecks'],1)
        self.assertEqual(report['files'][0]['preflight']['documentServices'],[W])

    def test_registry_change_and_explicit_retry_invalidate_cached_proof(self):
        identity = self.prepare()
        self.guard.preflight(identity)
        self.guard.preflight(identity,recheck=True)
        self.assertEqual(len(self.adapter.inspections),2)
        self.registry_file.write_text(self.registry_file.read_text()+'\n')
        report = self.guard.preflight(self.prepare())
        self.assertEqual(report['preflightCacheHits'],0)
        self.assertEqual(len(self.adapter.inspections),3)

    def test_extended_retry_only_checks_unknown_files_and_engine_change_checks_all(self):
        identity=self.guard.create([{'id':'ok','name':'ok.csv','size':1},
                                   {'id':'unknown','name':'unknown.csv','size':1}],prepare_only=True)['jobId']
        self.guard.upload(identity,'ok',io.BytesIO(b'C'),1)
        self.guard.upload(identity,'unknown',io.BytesIO(b'?'),1)
        self.guard.preflight(identity)
        before=len(self.adapter.inspections)
        self.guard.preflight(identity,recheck=True,only_undetermined=True,timeout_seconds=1200)
        self.assertEqual(len(self.adapter.inspections)-before,1)
        self.registry_file.write_text(self.registry_file.read_text()+'\n')
        self.guard.preflight(identity)
        self.assertEqual(len(self.adapter.inspections)-before,3)

    def test_loaded_native_model_proof_does_not_enable_a_different_service(self):
        native=registry(self.exe)
        proof={'state':'detected','basis':'native_preflight_uno_document','modelLoaded':True,
               'documentServices':[C],'registryFingerprint':native.fingerprint}
        self.assertEqual(native.observed_decision('dbf',proof),'registered')
        self.assertEqual(native.observed_decision('docx',proof),'unavailable')
        self.assertEqual(native.observed_decision('dbf',{**proof,'modelLoaded':False}),'undetermined')

    def test_timeouts_are_not_cached_and_retry_does_not_upload_again(self):
        self.adapter.preflight = lambda *a,**k: {'state':'undetermined','code':'PREFLIGHT_TIMEOUT',
                                               'diagnosticStage':'uno_type_detection'}
        identity = self.prepare()
        first = self.guard.preflight(identity)
        second = self.guard.preflight(identity,recheck=True)
        self.assertEqual((first['preflightChecks'],second['preflightChecks']),(1,2))
        self.assertEqual(second['files'][0]['preflight']['diagnosticStage'],'uno_type_detection')
        self.assertFalse(self.guard._proof_cache)

    def test_invalid_retry_request_does_not_release_an_unowned_io_lease(self):
        identity = self.prepare()
        self.guard.preflight(identity)
        with self.assertRaises(GuardError):
            self.guard.preflight(identity,recheck='yes')
        self.assertEqual(self.guard._jobs[identity].io,0)
        self.assertEqual(self.guard.status(identity)['state'],'prepared')

    def test_cache_has_fixed_bound_ttl_and_no_negative_entries(self):
        identity = self.prepare()
        report = self.guard.preflight(identity)
        fingerprint = self.adapter.preflight_fingerprint()
        proof = report['files'][0]['preflight']
        with self.guard._lock:
            for index in range(300):
                self.guard._remember_proof((fingerprint,str(index),'table.csv'),proof)
            self.assertEqual(len(self.guard._proof_cache),256)
            key = (fingerprint,'299','table.csv')
            stamp, value = self.guard._proof_cache[key]
            self.guard._proof_cache[key] = (stamp-3600,value)
            self.assertIsNone(self.guard._cached_proof(key))
            self.guard._remember_proof(key,{'state':'undetermined','code':'PREFLIGHT_TIMEOUT'})
            self.assertNotIn(key,self.guard._proof_cache)


if __name__ == '__main__':
    unittest.main()
