"""Bulk XHTML budgets and native model evidence for master/report exporters."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
from app_backend.converter_guard.export_recipe import native_export_recipe
from app_backend.converter_guard.routes import registry as installed_registry
from test_converter_guard_v04 import component, node

WRITER = 'com.sun.star.text.TextDocument'
WEB = 'com.sun.star.text.WebDocument'
MASTER = 'com.sun.star.text.GlobalDocument'
REPORT = 'com.sun.star.report.ReportDefinition'
FIXTURES = Path(__file__).parent / 'fixtures' / 'completion_routes'


class CompletionRouteContracts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.exe = self.root / 'program' / 'soffice.exe'
        self.exe.parent.mkdir()
        self.exe.touch()
        registry = self.root / 'share' / 'registry' / 'native.xcd'
        registry.parent.mkdir(parents=True)
        types = ''.join(node(ext, Extensions=ext) for ext in ('odt', 'html', 'odm', 'otm', 'orp', 'pdf'))
        rows = ''.join(node(name, Type=ext, DocumentService=service, Flags=flags) for name, ext, service, flags in (
            ('writer8', 'odt', WRITER, 'IMPORT EXPORT'),
            ('HTML', 'html', WEB, 'IMPORT EXPORT'),
            ('writerglobal8', 'odm', MASTER, 'IMPORT EXPORT'),
            ('writerglobal8_template', 'otm', MASTER, 'IMPORT EXPORT'),
            ('Base Report', 'orp', REPORT, 'IMPORT EXPORT'),
            ('writer_pdf_Export', 'pdf', WRITER, 'EXPORT'),
            ('writer_globaldocument_pdf_Export', 'pdf', MASTER, 'EXPORT')))
        registry.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">' +
                            component('Types', 'Types', types) +
                            component('Filter', 'Filters', rows) + '</oor:data>')
        self.native = installed_registry(self.exe)

    def proof(self, services, **extra):
        return {'state': 'detected', 'basis': 'native_preflight_uno_document',
                'modelLoaded': True, 'documentServices': services,
                'registryFingerprint': self.native.fingerprint, **extra}

    def test_only_loaded_master_and_report_models_enable_their_own_exporters(self):
        for service, allowed, excluded in ((WRITER, (), ('odm', 'otm', 'orp')),
                                           (WEB, (), ('odm', 'otm', 'orp')),
                                           (MASTER, ('odm', 'otm'), ('orp',)),
                                           (REPORT, ('orp',), ('odm', 'otm'))):
            proof = self.proof([service])
            for target in allowed:
                with self.subTest(service=service, target=target):
                    self.assertEqual(self.native.observed_decision(target, proof), 'registered')
                    self.assertIsNotNone(native_export_recipe(self.native, target, proof))
                    self.assertNotEqual(self.native.observed_decision(
                        target, {**proof, 'registryFingerprint': 'stale'}), 'registered')
            for target in excluded:
                self.assertEqual(self.native.observed_decision(target, proof), 'unavailable')

    def test_lok_writer_union_cannot_advertise_a_master_export(self):
        proof = self.proof([WRITER, WEB, MASTER], basis='native_preflight_document_kind',
                           nativeKind=0, modelLoaded=False)
        self.assertEqual(self.native.observed_decision('pdf', proof), 'registered')
        for target in ('odm', 'otm'):
            self.assertEqual(self.native.observed_decision(target, proof), 'undetermined')
            self.assertEqual(self.native.route_reason(target, proof), 'NATIVE_DOCUMENT_SERVICE_UNCONFIRMED')
            self.assertIsNone(native_export_recipe(self.native, target, proof))
        # Neither a source contract nor narrowing an enum's service list can
        # turn a document-kind observation into an actual loaded master model.
        for services in ([MASTER], [WRITER, WEB, MASTER]):
            declared = {**proof, 'documentServices': services,
                        'sourceContract': {'expectedServices': [MASTER]}}
            self.assertNotEqual(self.native.observed_decision('odm', declared), 'registered')

    def test_type_detection_and_unloaded_uno_do_not_establish_a_report_model(self):
        for service, target, type_name, filter_name in ((MASTER, 'odm', 'odm', 'writerglobal8'),
                                                       (REPORT, 'orp', 'orp', 'Base Report')):
            proof = self.proof([service], basis='native_preflight_uno_type',
                               typeName=type_name, filterName=filter_name)
            self.assertEqual(self.native.observed_decision(target, proof), 'undetermined')
            self.assertIsNone(native_export_recipe(self.native, target, proof))
            proof = self.proof([service], modelLoaded=False)
            self.assertNotEqual(self.native.observed_decision(target, proof), 'registered')

    def test_bulk_xhtml_retains_native_budget_for_an_import_longer_than_fifteen_seconds(self):
        adapter = LibreOfficeAdapter(self.exe)
        guard = ConverterGuard(adapter=adapter, limits=GuardLimits(preflight_timeout=37),
                               recovery_parent=self.root / 'jobs')
        self.addCleanup(guard.close)
        source = (FIXTURES / 'NEW_02_LEVEL_2.xhtml').read_bytes()
        rows = [{'id': 'f%02d' % i, 'name': 'source-%02d.xhtml' % i,
                 'size': len(source)} for i in range(32)]
        job = guard.create(rows, prepare_only=True)['jobId']
        for row in rows:
            guard.upload(job, row['id'], io.BytesIO(source), len(source))
        budgets = []

        def twenty_second_import(path, workspace, *, cancel, timeout):
            # Model the same native import cost without a twenty-second sleep
            # per source. Production native imports are checked by the QA tool.
            budgets.append(timeout)
            return (self.proof([WEB], nativeTextDocument=True) if timeout >= 20 else
                    {'state': 'undetermined', 'code': 'PREFLIGHT_TIMEOUT'})

        with patch.object(adapter, 'preflight', side_effect=twenty_second_import):
            report = guard.preflight(job)
            pdf = next(row for row in report['formats'] if row['id'] == 'pdf')
            self.assertEqual(pdf['verifiedFileIds'], [row['id'] for row in rows])
            self.assertEqual(budgets, [37] * 32)
            self.assertEqual(guard.status(job)['preflightTimeoutSeconds'], 37)
            # Explicit shorter budgets remain effective and cannot inherit a
            # cached positive proof from the preceding native inspection.
            budgets.clear()
            report = guard.preflight(job, recheck=True, timeout_seconds=10)
            pdf = next(row for row in report['formats'] if row['id'] == 'pdf')
            self.assertEqual(pdf['verifiedFileIds'], [])
            self.assertEqual(budgets, [10] * 32)

    def test_preserved_xhtml_sources_match_prior_windows_corpus(self):
        provenance = json.loads((FIXTURES / 'provenance.json').read_text())
        self.assertEqual(len(provenance['sources']), 2)
        for row in provenance['sources']:
            raw = (FIXTURES / row['name']).read_bytes()
            self.assertEqual(len(raw), row['bytes'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row['sha256'])


if __name__ == '__main__':
    unittest.main()
