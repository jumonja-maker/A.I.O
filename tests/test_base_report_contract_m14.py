"""M14: recognize real Base report content, never infer report export from .orp."""
from pathlib import Path
import tempfile
import unittest
import zipfile

from app_backend.converter_guard.source_contract import (
    OFFICE, REPORT, inspect_source_contract, source_import_hint)
from app_backend.converter_guard.routes import Registry


MIME = 'application/vnd.sun.xml.report'
RPT = 'http://openoffice.org/2005/report'


def write_report(path, *, mime=MIME, root='document-content', body='report',
                 include_content=True):
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:{root} xmlns:office="{OFFICE}" xmlns:rpt="{RPT}">'
        f'<office:body><office:{body}><rpt:detail/></office:{body}></office:body>'
        f'</office:{root}>'
    )
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', mime, compress_type=zipfile.ZIP_STORED)
        if include_content:
            archive.writestr('content.xml', content, compress_type=zipfile.ZIP_DEFLATED)
    return path


class BaseReportContractM14(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_odf_namespaced_report_matches_legacy_named_mime_without_suffix(self):
        # This schema is taken from the independent user-original ORP header,
        # not the full private document or an invented source export route.
        p = write_report(self.root / 'renamed.unknown')
        result = inspect_source_contract(p)
        self.assertEqual(result['state'], 'recognized')
        self.assertEqual(result['contentClass'], 'base_report_package')
        self.assertEqual(result['expectedServices'], [REPORT])
        self.assertEqual(result['mimetype'], MIME)
        self.assertTrue(result['bodyObserved'])

    def test_report_contract_never_sets_an_import_filter_or_enables_a_route(self):
        contract = inspect_source_contract(write_report(self.root / 'report.orp'))
        native = Registry((), self.root / 'absent-soffice')
        self.assertIsNone(source_import_hint(contract, native))
        self.assertEqual(native.observed_decision('orp',
                         {'state': 'detected', 'sourceContract': contract,
                          'registryFingerprint': native.fingerprint}),
                         'undetermined')

    def test_wrong_report_body_is_a_confirmed_mime_mismatch(self):
        p = write_report(self.root / 'wrong.orp', body='text')
        result = inspect_source_contract(p)
        self.assertEqual(result['state'], 'invalid')
        self.assertEqual(result['code'], 'SOURCE_CONTAINER_INCONSISTENT')
        self.assertEqual(result['reason'], 'base_report_body_mismatch')

    def test_legacy_namespace_and_flat_root_do_not_impersonate_report(self):
        for root in ('document', 'document-styles'):
            with self.subTest(root=root):
                p = write_report(self.root / (root + '.orp'), root=root)
                result = inspect_source_contract(p)
                self.assertEqual(result['state'], 'invalid')
                self.assertEqual(result['reason'], 'base_report_root_mismatch')

    def test_generic_writer_mime_does_not_become_a_report(self):
        p = write_report(self.root / 'not-a-report.orp',
                         mime='application/vnd.oasis.opendocument.text')
        result = inspect_source_contract(p)
        self.assertEqual(result['state'], 'invalid')
        self.assertEqual(result['reason'], 'mimetype_body_mismatch')

    def test_missing_content_xml_is_never_a_valid_report(self):
        p = write_report(self.root / 'incomplete.orp', include_content=False)
        result = inspect_source_contract(p)
        self.assertEqual(result['state'], 'invalid')
        self.assertEqual(result['code'], 'SOURCE_CONTAINER_INCONSISTENT')

    def test_unknown_future_legacy_mime_is_not_rejected_as_bad_report(self):
        p = write_report(self.root / 'unknown.orp',
                         mime='application/vnd.sun.xml.report-other')
        result = inspect_source_contract(p)
        self.assertEqual(result['state'], 'not_applicable')


if __name__ == '__main__':
    unittest.main()
