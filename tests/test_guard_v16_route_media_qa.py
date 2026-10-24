"""Independent QA readers must detect lost tables and changed raster pixels."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('route_media_qa', ROOT / 'tools/qa/guard_v16_route_media_acceptance.py')
qa = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(qa)


class TargetedAcceptanceReaders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def html_control(self, image=True):
        rows = ''.join('<tr>' + ''.join('<td>' + marker + '</td>' for marker in qa.MARKERS[row * 4:row * 4 + 4]) + '</tr>' for row in range(12))
        picture = '<img src="picture.png" />' if image else ''
        return '<html><head/><body><table>' + rows + '</table>' + picture + '</body></html>'

    def epub_control(self, body=None, *, spine='chapter', referenced_image=True):
        path = self.root / 'control.epub'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            archive.writestr('OPS/book.opf', '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="' + spine + '"/></spine></package>')
            archive.writestr('OPS/chapter.xhtml', body if body is not None else self.html_control(referenced_image))
            archive.writestr('OPS/orphan.xhtml', self.html_control())
            archive.writestr('OPS/picture.png', qa._media_fixture_helpers().png(width=3, height=5))
        return path

    def test_epub_spine_table_and_referenced_pixels_positive(self):
        check = qa.document_check([self.epub_control()], 'epub', image_required=True)
        self.assertEqual((check['tableCells'], check['images']), (48, 1))

    def test_epub_orphan_content_and_dangling_spine_never_supply_markers(self):
        for path in (self.epub_control(body='<html><body><!-- ' + ' '.join(qa.MARKERS) + ' --></body></html>'),):
            with self.assertRaises(AssertionError): qa.document_check([path], 'epub', image_required=True)
        path = self.epub_control(spine='missing')
        with self.assertRaises(AssertionError): qa.document_check([path], 'epub', image_required=True)

    def test_epub_unused_image_is_not_a_visible_document_image(self):
        path = self.epub_control(referenced_image=False)
        with self.assertRaises(AssertionError): qa.document_check([path], 'epub', image_required=True)

    def test_html_requires_real_ordered_cells_and_decodable_referenced_image(self):
        path = self.root / 'control.html'
        (self.root / 'picture.png').write_bytes(qa._media_fixture_helpers().png(width=3, height=5))
        path.write_text(self.html_control())
        self.assertEqual(qa.document_check([path], 'html', image_required=True)['tableCells'], 48)
        valid = self.html_control()
        broken = [
            '<html><body><!-- ' + ' '.join(qa.MARKERS) + ' --><img src="picture.png"/></body></html>',
            '<html><body><p>' + ' '.join(qa.MARKERS) + '</p><img src="picture.png"/></body></html>',
            valid.replace('AIO_V16_R0C0', 'TEMP').replace('AIO_V16_R0C1', 'AIO_V16_R0C0').replace('TEMP', 'AIO_V16_R0C1'),
            valid.replace('<table>', '<table hidden="hidden">'),
            valid.replace('<head/>', '<head><style>table {display:none}</style></head>'),
            valid.replace('picture.png', 'data:image/png;base64,AA=='),
        ]
        for text in broken:
            path.write_text(text)
            with self.subTest(text=text[:70]), self.assertRaises(AssertionError): qa.document_check([path], 'html', image_required=True)
        path.write_text(valid)
        (self.root / 'picture.png').write_bytes(qa._media_fixture_helpers().png(width=3, height=5, alpha=False))
        with self.assertRaises(AssertionError): qa.document_check([path], 'html', image_required=True)

    def test_html_valid_embedded_png_and_text_only_txt_are_allowed(self):
        path = self.root / 'control.html'
        data = base64.b64encode(qa._media_fixture_helpers().png(width=3, height=5)).decode('ascii')
        path.write_text(self.html_control().replace('picture.png', 'data:image/png;base64,' + data))
        self.assertEqual(qa.document_check([path], 'html', image_required=True)['images'], 1)
        text = self.root / 'control.txt'; text.write_text(' '.join(qa.MARKERS))
        self.assertEqual(qa.document_check([text], 'txt', image_required=False)['markers'], 48)

    def test_odt_only_body_frame_referenced_images_count(self):
        path = self.root / 'control.odt'
        xml = qa.writer_seed().replace('xmlns:table=', 'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" xmlns:table=')
        for visible in (False, True):
            body = xml.replace('</office:text>', '<text:p><draw:frame svg:width="1cm" svg:height="1cm"><draw:image xlink:href="Pictures/control.png"/></draw:frame></text:p></office:text>') if visible else xml
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('content.xml', body)
                archive.writestr('Pictures/control.png', qa._media_fixture_helpers().png(width=3, height=5))
            if visible: self.assertEqual(qa.document_check([path], 'odt', image_required=True)['images'], 1)
            else:
                with self.assertRaises(AssertionError): qa.document_check([path], 'odt', image_required=True)

    def test_ooxml_and_flat_xml_require_the_control_in_actual_cells(self):
        word = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        rows = ''.join('<w:tr>' + ''.join('<w:tc><w:p><w:r><w:t>' + marker + '</w:t></w:r></w:p></w:tc>'
            for marker in qa.MARKERS[row * 4:row * 4 + 4]) + '</w:tr>' for row in range(12))
        path = self.root / 'control.docm'
        for structured in (True, False):
            content = '<w:tbl>' + rows + '</w:tbl>' if structured else '<w:p><w:r><w:t>' + ' '.join(qa.MARKERS) + '</w:t></w:r></w:p>'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml', '<w:document xmlns:w="' + word + '"><w:body>' + content + '</w:body></w:document>')
                archive.writestr('[Content_Types].xml', '<Types><Override PartName="/word/document.xml" ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>')
            if structured: self.assertEqual(qa.document_check([path], 'docm', image_required=False)['tableCells'], 48)
            else:
                with self.assertRaises(AssertionError): qa.document_check([path], 'docm', image_required=False)
        flat = self.root / 'control.xml'; flat.write_text(qa.writer_seed())
        self.assertEqual(qa.document_check([flat], 'xml', image_required=False)['tableCells'], 48)
        flat.write_text(qa.writer_seed().replace('<office:body>', '<office:body><office:annotation>').replace('</office:body>', '</office:annotation></office:body>'))
        with self.assertRaises(AssertionError): qa.document_check([flat], 'xml', image_required=False)

    def test_mixed_dbf_cli_mapping_metadata_repeats_and_typed_raw_scalars(self):
        path = self.root / 'mixed.dbf'; path.write_bytes(b'controlled')
        mapping = path.with_name(path.name + '.aio-columns.jsonl')
        header = {'memoColumns': [0], 'numericColumnsRemainTyped': True}
        records = [header, {'sheet': 'Sheet1'},
            {'row': 1, 'rowRepeat': 1, 'column': 0, 'columnRepeat': 1, 'sourceType': 'float', 'sourceValue': '7', 'exportedText': '7'},
            {'row': 2, 'rowRepeat': 2, 'column': 0, 'columnRepeat': 1, 'sourceType': 'string', 'sourceValue': None, 'exportedText': 'TEXT'}]
        def write(): mapping.write_text('\n'.join(json.dumps(r) for r in records))
        with patch.object(qa, 'table_check', return_value={'status': 'pass'}), patch.object(qa, 'dbf_descriptor', return_value=(None, None, None, None, [('MIXED', 'M', 10, 0)])):
            write(); self.assertEqual(qa.mixed_dbf_check([path], [], [7, 'TEXT', 'TEXT'])['reconstructedStringValues'], 2)
            records[-1]['sourceValue'] = 'WRONG'; write()
            with self.assertRaises(AssertionError): qa.mixed_dbf_check([path], [], [7, 'TEXT', 'TEXT'])
            records[-1]['sourceValue'] = 'TEXT'; records[2]['sourceValue'] = '8'; write()
            with self.assertRaises(AssertionError): qa.mixed_dbf_check([path], [], [7, 'TEXT', 'TEXT'])

    def test_dbf_truncated_record_is_not_a_positive_fixture(self):
        data = bytearray(33); data[0] = 3
        struct.pack_into('<IHH', data, 4, 2, 33, 4); data[32] = 13
        path = self.root / 'truncated.dbf'; path.write_bytes(data)
        with self.assertRaises(ValueError): qa.dbf_records(path)

    def test_dbf_memo_preserves_long_utf8_and_requires_sidecar(self):
        text = 'Я🙂' * 200
        data = bytearray(65 + 11 + 1); data[0] = 0x83
        struct.pack_into('<IHH', data, 4, 1, 65, 11)
        data[32:36] = b'TEXT'; data[43] = ord('M'); data[48] = 10
        data[64] = 13; data[65:76] = b' 0000000001'; data[76] = 26
        path = self.root / 'memo.dbf'; path.write_bytes(data)
        path.with_suffix('.dbt').write_bytes(bytes(512) + text.encode('utf-8') + b'\x1a\x1a')
        self.assertEqual(qa.dbf_records(path), [['TEXT'], [text]])
        self.assertEqual(qa.dbf_import_rows(path)[0], ['TEXT,M'])
        path.with_suffix('.dbt').unlink()
        with self.assertRaises(FileNotFoundError): qa.dbf_records(path)

    def test_dif_reads_numeric_values_and_quoted_commas(self):
        path = self.root / 'table.dif'
        path.write_text('TABLE\n0,1\n"Sheet"\nVECTORS\n0,2\n""\nTUPLES\n0,1\n""\n'
            'DATA\n0,0\n""\n-1,0\nBOT\n1,0\n"comma, text"\n0,123.5\nV\n-1,0\nEOD\n')
        self.assertEqual(qa.dif_records(path), [['comma, text', qa.Decimal('123.5')]])

    def test_malformed_dif_numeric_reports_exact_raw_line_without_locale_guessing(self):
        path = self.root / 'localized.dif'
        path.write_text('TABLE\n0,1\n"Sheet"\nDATA\n0,0\n""\n-1,0\nBOT\n0,123,5\nV\n-1,0\nEOD\n')
        with self.assertRaisesRegex(AssertionError, "DIF file='localized.dif'.*pair='0,123,5'.*rawHex="):
            qa.dif_records(path)

    def test_malformed_dbf_numeric_reports_field_offset_and_bytes(self):
        data = bytearray(65 + 8 + 1); data[0] = 3
        struct.pack_into('<IHH', data, 4, 1, 65, 8)
        data[32:38] = b'AMOUNT'; data[43] = ord('N'); data[48] = 7; data[49] = 2
        data[64] = 13; data[65:73] = b' 123,50 '; data[73] = 26
        path = self.root / 'localized.dbf'; path.write_bytes(data)
        with self.assertRaisesRegex(AssertionError, "DBF file='localized.dbf'.*field='AMOUNT'.*offset=66.*rawHex="):
            qa.dbf_records(path)

    def test_canonical_native_numbers_ignore_localized_display_but_reject_localized_values(self):
        path = self.root / 'control.fods'
        office = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
        table = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
        text = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
        xml = '<office:document xmlns:office="' + office + '" xmlns:table="' + table + '" xmlns:text="' + text + '">' + \
            '<office:body><office:spreadsheet><table:table><table:table-row>' + \
            '<table:table-cell office:value-type="string"><text:p>ALPHA</text:p></table:table-cell>' + \
            '<table:table-cell office:value-type="float" office:value="123.5"><text:p>123,50</text:p></table:table-cell>' + \
            '<table:table-cell table:number-columns-repeated="16382"/></table:table-row>' + \
            '<table:table-row table:number-rows-repeated="1048575"><table:table-cell/></table:table-row>' + \
            '</table:table></office:spreadsheet></office:body></office:document>'
        path.write_text(xml)
        self.assertEqual(qa.canonical_fods_rows(path), [['ALPHA', qa.Decimal('123.5')]])
        path.write_text(xml.replace('office:value="123.5"', 'office:value="123,5"'))
        with self.assertRaisesRegex(AssertionError, 'not canonical'):
            qa.canonical_fods_rows(path)

    def test_native_and_raw_table_comparison_uses_numeric_values_not_decimal_repr(self):
        path = self.root / 'table.dif'; path.write_bytes(b'controlled')
        expected = [['NAME', 'AMOUNT'], ['ALPHA', qa.Decimal('123.5')]]
        raw = [['NAME', 'AMOUNT'], ['ALPHA', qa.Decimal('123.50')]]
        flat = self.root / 'table.fods'
        with patch.object(qa, 'dif_records', return_value=raw), patch.object(
                qa, 'native_table_rows', return_value=([expected], [flat])), patch(
                'guard_v17_native_acceptance.check_native_table_readback', return_value={'allFound': True}):
            result = qa.table_check([path], 'dif', [expected], native_work=self.root / 'readback')
            self.assertEqual(result['sheets'], 1)
            raw[1][1] = qa.Decimal('123.51')
            with self.assertRaisesRegex(AssertionError, 'disagrees'):
                qa.table_check([path], 'dif', [expected], native_work=self.root / 'changed')

    def test_dif_charset_declaration_covers_exact_saved_unicode_outputs(self):
        path = self.root / 'table.dif'
        path.write_bytes('TABLE\n0,1\n"Проверка"\nDATA\n0,0\n""\n-1,0\nEOD\n'.encode('utf-8'))
        declaration = self.root / 'table.aio-encoding.json'
        metadata = {'format': 'DIF', 'encoding': 'UTF-8', 'bom': False, 'dataBytesChanged': False,
            'libreOfficeImport': {'FilterName': 'DIF', 'FilterOptions': '76'},
            'files': [{'name': path.name, 'bytes': path.stat().st_size, 'sha256': qa.digest(path)}]}
        def write(): declaration.write_text(json.dumps(metadata), encoding='utf-8')
        write()
        self.assertEqual(qa.dif_charset_check([path, declaration])['encodingDeclaredFiles'], 1)
        with self.assertRaises(AssertionError): qa.dif_charset_check([path])
        metadata['files'][0]['sha256'] = '0' * 64; write()
        with self.assertRaises(AssertionError): qa.dif_charset_check([path, declaration])
        metadata['files'][0]['sha256'] = qa.digest(path)
        metadata['libreOfficeImport']['FilterOptions'] = '1252'; write()
        with self.assertRaises(AssertionError): qa.dif_charset_check([path, declaration])

    def test_missing_second_sheet_fails_independent_cell_check(self):
        from openpyxl import Workbook
        path = self.root / 'lost.xlsx'; book = Workbook()
        for row in qa.TABLE_ROWS: book.active.append([float(v) if isinstance(v, qa.Decimal) else v for v in row])
        book.save(path)
        second = [list(row) for row in qa.TABLE_ROWS]; second[1][0] = 'SECOND_ALPHA'
        with self.assertRaises(AssertionError): qa.table_check([path], 'xlsx', [qa.TABLE_ROWS, second])
        self.assertEqual(qa.table_check([path], 'xlsx', [qa.TABLE_ROWS])['sheets'], 1)

    def test_independent_tiff_reader_decodes_real_rgba_strips(self):
        helper = qa._media_fixture_helpers()
        path = self.root / 'three-pages.tiff'; path.write_bytes(helper.tiff(pages=3, width=3, height=5))
        proof = qa.uncompressed_tiff_frame_hash(path)
        self.assertEqual(proof['rgbaSha256'], hashlib.sha256(bytes((50, 100, 200, 128)) * 15).hexdigest())
        self.assertEqual((proof['width'], proof['height'], proof['alpha']), (3, 5, True))

    def test_independent_tiff_reader_rejects_truncated_pixels(self):
        helper = qa._media_fixture_helpers()
        path = self.root / 'truncated.tiff'; path.write_bytes(helper.tiff()[:-3])
        with self.assertRaises(ValueError): qa.uncompressed_tiff_frame_hash(path)

    def test_cross_format_tiff_requires_successful_independent_pixel_decode(self):
        helper = qa._media_fixture_helpers()
        source = self.root / 'source.png'; source.write_bytes(helper.png(width=3, height=5))
        data = bytearray(helper.tiff(width=3, height=5))
        for index in range(struct.unpack_from('<H', data, 8)[0]):
            position = 10 + 12 * index
            if struct.unpack_from('<H', data, position)[0] == 259:
                struct.pack_into('<I', data, position + 8, 5)  # Claims LZW; strips are not encoded.
        result = self.root / 'undecodable.tiff'; result.write_bytes(data)
        with self.assertRaises((AssertionError, ValueError)):
            qa.media_check(source, [result], 'tiff')

    def test_decodes_all_generated_apng_gif_webp_frames(self):
        try:
            import PySide6.QtGui
        except ImportError:
            self.skipTest('Packaged Qt is required for independent frame pixel decoding')
        helper = qa._media_fixture_helpers()
        for name, data, count in [('eight.apng', helper.png(frames=8), 8),
                                  ('eight.gif', helper.gif(frames=8), 8),
                                  ('three.webp', helper.WEBP_ANIMATED, 3)]:
            path = self.root / name; path.write_bytes(data)
            self.assertEqual(len(qa.decoded_frame_hashes(path)), count)

    def test_pdf_unused_shared_image_resource_is_not_counted_as_painted(self):
        try:
            from pypdf import PdfReader, PdfWriter
            from pypdf.generic import DictionaryObject, NameObject, NumberObject, DecodedStreamObject
        except ImportError:
            self.skipTest('pypdf structural PDF inspector unavailable')
        writer = PdfWriter()
        image = DecodedStreamObject(); image.set_data(b'\xff\0\0')
        image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
            NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
            NameObject('/BitsPerComponent'): NumberObject(8), NameObject('/ColorSpace'): NameObject('/DeviceRGB')})
        reference = writer._add_object(image)
        for painted in (False, True):
            page = writer.add_blank_page(width=72, height=72)
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'):
                DictionaryObject({NameObject('/Im0'): reference})})
            contents = DecodedStreamObject(); contents.set_data(b'q 20 0 0 20 0 0 cm /Im0 Do Q' if painted else b'')
            page[NameObject('/Contents')] = writer._add_object(contents)
        path = self.root / 'shared.pdf'
        with path.open('wb') as stream: writer.write(stream)
        pdf = PdfReader(path)
        self.assertEqual([qa.painted_pdf_images(p, pdf) for p in pdf.pages], [0, 1])

    def test_draw_html_checks_order_original_png_and_active_or_external_resources(self):
        try:
            import PySide6.QtGui
        except ImportError:
            self.skipTest('Packaged Qt image reader unavailable')
        payload = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
        markers = ['AIO_PAGE_ONE', 'AIO_PAGE_TWO']
        source = {'pageMarkers': markers, 'imageSha256': hashlib.sha256(payload).hexdigest()}
        primary = self.root / 'drawing.html'
        primary.write_text('<html><body><object data="one.svg"/><object data="two.svg"/></body></html>')
        def svg(marker):
            return '<svg xmlns="http://www.w3.org/2000/svg"><rect width="5" height="5"/>' + \
                '<text>' + marker + '</text><image href="data:image/png;base64,' + base64.b64encode(payload).decode() + '"/></svg>'
        one, two = self.root / 'one.svg', self.root / 'two.svg'
        one.write_text(svg(markers[0])); two.write_text(svg(markers[1]))
        self.assertEqual(qa.draw_html_check([primary, one, two], 'html', source)['pages'], 2)
        two.write_text(svg(markers[0]))
        with self.assertRaises(AssertionError): qa.draw_html_check([primary, one, two], 'html', source)

        two.write_text(svg(markers[1])); one.write_text(svg(markers[0]).replace('<rect', '<script/> <rect'))
        with self.assertRaises(AssertionError): qa.draw_html_check([primary, one, two], 'html', source)
        one.write_text(svg(markers[0]).replace('data:image/png;base64,', 'https://remote.invalid/'))
        with self.assertRaises(AssertionError): qa.draw_html_check([primary, one, two], 'html', source)

    def test_svm_first_page_only_cannot_satisfy_two_page_acceptance(self):
        path = self.root / 'first.svm'; path.write_bytes(b'VCLMTF')
        with self.assertRaisesRegex(AssertionError, 'page count'):
            qa.svm_check([path], self.root / 'reimport', self.root / 'native', 1, expected_pages=2)


if __name__ == '__main__': unittest.main()
