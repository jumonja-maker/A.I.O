"""Separate source paragraphs require separate output character occurrences."""
from collections import Counter
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.native_fidelity import NATIVE_FIDELITY_SOURCE
from app_backend.converter_guard.postconditions import check_native_output


class PresentationOccurrences(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.worker = {}
        exec(compile(NATIVE_FIDELITY_SOURCE, '<native occurrence regression>', 'exec'), self.worker)

    def source(self, values):
        from xml.sax.saxutils import escape
        path = self.root/'presentation.odp'
        text = ''.join('<draw:frame><draw:text-box><text:p>' + escape(value) +
                       '</text:p></draw:text-box></draw:frame>' for value in values)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.presentation')
            archive.writestr('content.xml',
                '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
                'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                '<office:body><office:presentation><draw:page draw:name="Slide">' + text +
                '</draw:page></office:presentation></office:body></office:document-content>')
        return path

    def output(self, body):
        path = self.root/'presentation.xhtml'
        path.write_text('<html><body>' + body + '</body></html>', encoding='utf-8')
        return path

    def host(self, values, body):
        return check_native_output([self.output(body)], 'xhtml', self.source(values), cancel=self.cancel)

    def native(self, values, body):
        expected = Counter(self.worker['aio_normal_text'](value) for value in values)
        return self.worker['aio_verify_presentation_xhtml']([self.output(body)], expected)

    def test_host_rejects_distinct_paragraphs_reusing_one_output_span(self):
        for values, body in ((['Total', 'Grand Total'], '<p>Grand Total</p>'),
                             (['A B', 'B C'], '<p>A B C</p>')):
            with self.subTest(values=values):
                with self.assertRaises(GuardError) as error:
                    self.host(values, body)
                self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_native_rejects_distinct_paragraphs_reusing_one_output_span(self):
        for values, body in ((['Total', 'Grand Total'], '<p>Grand Total</p>'),
                             (['A B', 'B C'], '<p>A B C</p>')):
            with self.subTest(values=values):
                with self.assertRaises(self.worker['AioPresentationTextLoss']):
                    self.native(values, body)

    def test_valid_allocation_can_require_alternate_phrase_order(self):
        values = ['A B', 'B C']
        result = self.host(values, '<p>A B C A B</p>')
        self.assertEqual(result['additionalChecks'][0]['status'], 'checked_subset')
        self.native(values, '<p>A B C A B</p>')

    def test_inline_runs_and_adjacent_paragraphs_preserve_existing_normalization(self):
        values = ['Total', 'Grand Total']
        result = self.host(values, '<p>Grand <span>Total</span></p><p>Total</p>')
        self.assertEqual(result['additionalChecks'][0]['status'], 'checked_subset')
        self.native(values, '<p>Total</p><p>Grand <span>Total</span></p>')
        self.native(['AIO 00123 42,50 €'], '<p>AIO<span>00123</span> 42,50 <b>€</b></p>')

    def test_repeated_paragraphs_still_require_distinct_occurrences(self):
        with self.assertRaises(GuardError) as error:
            self.host(['Repeat', 'Repeat'], '<p>Repeat</p>')
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')
        with self.assertRaises(self.worker['AioPresentationTextLoss']):
            self.native(['Repeat', 'Repeat'], '<p>Repeat</p>')

    def test_host_allocation_budget_is_incomplete_not_false_loss(self):
        with patch('app_backend.converter_guard.presentation_evidence.MAX_TEXT_ALLOCATION_WORK', 0):
            with self.assertRaises(GuardError) as error:
                self.host(['A B', 'B C'], '<p>A B C A B</p>')
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_native_allocation_budget_is_unverified_not_false_loss(self):
        self.worker['_AIO_PRESENTATION_ALLOCATION_WORK'] = 0
        with self.assertRaises(self.worker['AioPresentationUnverified']):
            self.native(['A B', 'B C'], '<p>A B C A B</p>')

    def test_alternative_graphic_representation_remains_unverified(self):
        result = self.host(['Total', 'Grand Total'],
                           '<svg><rect width="1" height="1"/></svg><p>Grand Total</p>')
        check = result['additionalChecks'][0]
        self.assertEqual(check['status'], 'not_checked')
        with self.assertRaises(self.worker['AioPresentationUnverified']):
            self.native(['Total', 'Grand Total'], '<svg></svg><p>Grand Total</p>')


if __name__ == '__main__':
    unittest.main()
