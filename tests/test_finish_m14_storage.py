"""Report storage import failures and content-proved native master routes."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from app_backend.converter_guard.preflight_native import _odf_master
from app_backend.converter_guard.report_storage import REPORT_STORAGE_SOURCE
import test_completion_routes as route_contracts

MASTER = 'com.sun.star.text.GlobalDocument'


class ReportStorageImportContract(unittest.TestCase):
    def setUp(self):
        self.api = {}
        exec(compile(REPORT_STORAGE_SOURCE, '<report-storage-worker>', 'exec'), self.api)
        self.prop = lambda name, value: SimpleNamespace(Name=name, Value=value)
        self.storage = Mock()
        self.model = Mock()
        self.model.attachResource.return_value = True
        self.model.supportsService.return_value = True
        self.model.queryInterface.return_value = True
        self.importer = Mock()
        self.importer.filter.return_value = True
        self.filters = Mock()
        self.filters.hasByName.return_value = True
        self.filters.getByName.return_value = tuple(self.prop(k, v) for k, v in {
            'DocumentService':'com.sun.star.report.ReportDefinition', 'Flags':3,
            'FilterService':'com.sun.star.comp.report.OReportFilter'}.items())
        self.factory = Mock()
        self.factory.createInstanceWithArguments.return_value = self.storage
        self.remote = SimpleNamespace(ServiceManager=Mock())
        services = {'com.sun.star.document.FilterFactory':self.filters,
                    'com.sun.star.embed.StorageFactory':self.factory,
                    'com.sun.star.report.ReportDefinition':self.model,
                    'com.sun.star.comp.report.OReportFilter':self.importer}
        self.remote.ServiceManager.createInstanceWithContext.side_effect = lambda name, _: services[name]
        self.uno = patch.dict(sys.modules, {'uno':SimpleNamespace(getTypeByName=lambda name:name)})
        self.uno.start()
        self.addCleanup(self.uno.stop)

    def load(self):
        return self.api['aio_load_report_storage'](self.remote, self.prop, Path('/report.orp'), ())

    def test_native_filter_false_never_admits_partially_loaded_report(self):
        self.importer.filter.return_value = False
        with self.assertRaisesRegex(self.api['AioReportStorageUnverified'], 'report_native_import_failed'):
            self.load()
        self.model.dispose.assert_called_once_with()
        self.storage.dispose.assert_called_once_with()

    def test_success_retains_live_model_and_disables_no_source_controls(self):
        self.assertIs(self.load(), self.model)
        self.model.dispose.assert_not_called()
        self.storage.dispose.assert_not_called()
        self.importer.setTargetDocument.assert_called_once_with(self.model)
        arguments = {p.Name:p.Value for p in self.importer.filter.call_args.args[0]}
        self.assertEqual(arguments['FilterName'], 'StarOffice XML (Base) Report')
        self.assertIs(arguments['Storage'], self.storage)
        self.assertEqual(self.factory.createInstanceWithArguments.call_args.args[0][1], 1)

    def test_absent_native_import_flag_refuses_before_opening_storage(self):
        self.filters.getByName.return_value = (
            self.prop('DocumentService', 'com.sun.star.report.ReportDefinition'), self.prop('Flags', 2))
        with self.assertRaisesRegex(self.api['AioReportStorageUnverified'], 'report_native_filter_unconfirmed'):
            self.load()
        self.factory.createInstanceWithArguments.assert_not_called()

    def test_resource_attach_refusal_disposes_owned_native_objects(self):
        self.model.attachResource.return_value = False
        with self.assertRaisesRegex(self.api['AioReportStorageUnverified'], 'report_native_resource_unconfirmed'):
            self.load()
        self.importer.filter.assert_not_called()
        self.model.dispose.assert_called_once_with()
        self.storage.dispose.assert_called_once_with()


class MasterPackageParser(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def package(self, *, mime='application/vnd.oasis.opendocument.text-master', body='text', prefix=''):
        path = self.root/'renamed.bin'
        with ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', mime)
            archive.writestr('content.xml', prefix + '<office:document-content '
                'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0">'
                '<office:body><office:'+body+'/></office:body></office:document-content>')
        return path

    def test_master_content_is_confirmed_independent_of_extension(self):
        self.assertTrue(_odf_master(self.package()))

    def test_regular_writer_and_wrong_body_do_not_impersonate_master(self):
        self.assertFalse(_odf_master(self.package(mime='application/vnd.oasis.opendocument.text')))
        self.assertFalse(_odf_master(self.package(body='spreadsheet')))

    def test_dtd_is_not_accepted_as_master_content(self):
        self.assertFalse(_odf_master(self.package(prefix='<!DOCTYPE document-content [<!ENTITY x "text">]>')))


class MasterContentRoutes(unittest.TestCase):
    def setUp(self):
        route_contracts.CompletionRouteContracts.setUp(self)

    def test_real_native_other_kind_and_validated_master_contract_are_both_required(self):
        proof = {'state':'detected', 'basis':'native_preflight_master_content',
                 'nativeKind':4, 'masterPackage':True, 'documentServices':[MASTER],
                 'registryFingerprint':self.native.fingerprint,
                 'sourceContract':{'state':'recognized','expectedServices':[MASTER],
                   'mimetype':'application/vnd.oasis.opendocument.text-master'}}
        for target in ('pdf','odm','otm'):
            self.assertEqual(self.native.observed_decision(target, proof), 'registered')
        for changed in ({'nativeKind':0}, {'nativeKind':True}, {'masterPackage':False},
                        {'registryFingerprint':'stale'}, {'sourceContract':{}},
                        {'documentServices':['com.sun.star.text.TextDocument']}):
            with self.subTest(changed=changed):
                self.assertNotEqual(self.native.observed_decision('odm', {**proof, **changed}), 'registered')


if __name__ == '__main__':
    unittest.main()
