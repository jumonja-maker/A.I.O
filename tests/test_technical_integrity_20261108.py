"""Independent result-integrity regressions from the current-tree inspection."""
from pathlib import Path
import json
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
import zlib

from app_backend.converter_guard.content_invariants import check_content_invariants
from app_backend.converter_guard.core import _html_resource_warning
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.postconditions import check_native_output


def png(width=1, height=1):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data +
                struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
    pixels = (b'\0' + b'\xff\0\0' * width) * height
    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


class TechnicalIntegrity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()

    def source(self, name='slides.odp'):
        source = self.root / name
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.presentation')
            archive.writestr('content.xml',
                '<office:document-content '
                'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0">'
                '<office:body><office:presentation><draw:page draw:name="One"/>'
                '<draw:page draw:name="Two"/></office:presentation></office:body>'
                '</office:document-content>')
        return source

    def pages(self, second):
        source = self.source()
        primary = self.root / 'slides.png'; primary.write_bytes(png())
        secondary = self.root / 'slides__page_0002.png'; secondary.write_bytes(second)
        manifest = self.root / 'AIO-pages.json'
        manifest.write_text(json.dumps({'schema': 1, 'pageCount': 2, 'pages': [
            {'page': 1, 'file': primary.name}, {'page': 2, 'file': secondary.name}]}))
        return source, [primary, secondary, manifest]

    def html(self, body):
        source = self.source('document.odp')
        output = self.root / 'document.html'
        output.write_text('<!doctype html><html><body>' + body + '</body></html>')
        return source, output

    def test_every_exported_raster_page_must_have_a_complete_container(self):
        for damaged in (png()[:-7], b'not a PNG container'):
            with self.subTest(payload=damaged[-10:]):
                source, paths = self.pages(damaged)
                with self.assertRaises(GuardError) as error:
                    check_native_output(paths, 'png', source, cancel=self.cancel)
                self.assertEqual(error.exception.code, 'MEDIA_OUTPUT_INVALID')

    def test_valid_secondary_pages_can_have_different_canvas_dimensions(self):
        source, paths = self.pages(png(2, 3))
        result = check_native_output(paths, 'png', source, cancel=self.cancel)
        self.assertEqual(result['status'], 'checked_subset')
        self.assertEqual(result['validatedOutputContainers'], 2)

    def test_svg_filter_image_dependencies_are_checked_in_html_and_companions(self):
        for attribute in ('href', 'xlink:href'):
            for companion in (False, True):
                with self.subTest(attribute=attribute, companion=companion):
                    (self.root / 'photo.png').unlink(missing_ok=True)
                    svg = ('<svg xmlns="http://www.w3.org/2000/svg" '
                           'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
                           '<filter id="photo"><feImage ' + attribute + '="photo.png"/></filter>'
                           '<rect width="100" height="100" filter="url(#photo)"/></svg>')
                    if companion:
                        (self.root / 'figure.svg').write_text(svg)
                    source, output = self.html('<img src="figure.svg">' if companion else svg)
                    self.assertEqual(_html_resource_warning(output),
                                     'HTML_LOCAL_RESOURCES_NOT_UPLOADED')
                    with self.assertRaises(GuardError) as error:
                        check_native_output([output], 'html', source, cancel=self.cancel)
                    self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_RESOURCE_MISSING')
                    (self.root / 'photo.png').write_bytes(png())
                    result = check_native_output([output], 'html', source, cancel=self.cancel)
                    self.assertTrue(result['localResourcesChecked'])

    def test_truncated_resource_attributes_are_not_verified(self):
        attributes = (
            'style="--padding:' + 'a' * 5000 + ';background-image:url(missing.png)"',
            'srcset="data:image/png;base64,' + 'A' * 5000 + ' 1x, missing.png 2x"',
            'src="' + 'a' * 5000 + '.png"',
        )
        for attributes_value in attributes:
            with self.subTest(attribute=attributes_value[:20]):
                source, output = self.html('<img ' + attributes_value + '>visible text')
                with self.assertRaises(GuardError) as error:
                    check_native_output([output], 'html', source, cancel=self.cancel)
                self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_large_inline_data_and_nonresource_attributes_still_work(self):
        source, output = self.html('<img src="data:image/png;base64,' + 'A' * 200000 +
                                   '" title="' + 'x' * 5000 + '">visible text')
        result = check_native_output([output], 'html', source, cancel=self.cancel)
        self.assertTrue(result['localResourcesChecked'])

    def test_long_style_reference_across_stream_blocks_stays_unverified(self):
        source, output = self.html('<div style="--padding:' + 'a' * 140000 +
                                  ';background:url(missing.png)">visible text</div>')
        with self.assertRaises(GuardError) as error:
            check_native_output([output], 'html', source, cancel=self.cancel)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def table(self, values):
        source = self.root / 'table.odt'
        cells = ''.join('<table:table-cell><text:p>' + value +
                        '</text:p></table:table-cell>' for value in values)
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
            archive.writestr('content.xml',
                '<office:document-content '
                'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
                'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                '<office:body><office:text><table:table><table:table-row>' + cells +
                '</table:table-row></table:table></office:text></office:body></office:document-content>')
        return source

    def text_check(self, values, text):
        source = self.table(values)
        output = self.root / 'table.txt'; output.write_text(text)
        return check_content_invariants([output], 'txt', source, cancel=self.cancel)

    def test_distinct_source_cells_cannot_reuse_overlapping_output_text(self):
        for values, text in ((['Total', 'Grand Total'], 'Grand Total'),
                             (['A B', 'B C'], 'A B C')):
            with self.subTest(values=values):
                with self.assertRaises(GuardError) as error:
                    self.text_check(values, text)
                self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_valid_overlapping_phrases_do_not_depend_on_greedy_order(self):
        for values, text in ((['A B', 'B C'], 'A B C A B'),
                             (['B B', 'A B'], 'A B B B')):
            with self.subTest(values=values):
                result = self.text_check(values, text)
                self.assertEqual(result['status'], 'checked_subset')
                self.assertEqual(result['checkedCellOccurrences'], 2)

    def test_interleaved_duplicate_values_keep_separate_occurrences(self):
        values = ['ALPHA', 'BETA'] * 500
        result = self.text_check(values, ' '.join(values))
        self.assertEqual(result['checkedCellOccurrences'], 1000)

    def test_text_allocation_budget_exhaustion_stays_unverified(self):
        with patch('app_backend.converter_guard.content_invariants.MAX_TEXT_ALLOCATION_WORK', 0,
                   create=True):
            with self.assertRaises(GuardError) as error:
                self.text_check(['A B', 'B C'], 'A B C A B')
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')


if __name__ == '__main__':
    unittest.main()
