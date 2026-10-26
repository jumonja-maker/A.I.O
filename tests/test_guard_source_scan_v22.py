"""Content-based source declarations, false-rejection controls and bounded I/O.

No renderer/native import is mocked as successful. A declaration is never route
admission; native model matching remains a separate mandatory check.
"""
from hashlib import sha256
import io
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile
import zlib

from app_backend.converter_guard import source_contract as sc
from app_backend.converter_guard.errors import GuardError

CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
OREL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument'
MANIFEST = 'urn:oasis:names:tc:opendocument:xmlns:manifest:1.0'
CALC_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
WRITER_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
WORKBOOK = '<s:workbook xmlns:s="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'


def encoded(text, encoding='utf-8'):
    if encoding == 'utf-8':
        return text.encode(encoding)
    declaration = 'UTF-16' if encoding.startswith('utf-16') else encoding
    value = ('<?xml version="1.0" encoding="' + declaration + '"?>' + text).encode(encoding)
    return ({'utf-16-le': b'\xff\xfe', 'utf-16-be': b'\xfe\xff'}.get(encoding, b'') + value)


def odf_xml(kind='text', *, flat=False, body=None):
    root = 'document' if flat else 'document-content'
    mime = ' o:mimetype="application/vnd.oasis.opendocument.' + kind + '"' if flat else ''
    return (f'<o:{root} xmlns:o="{sc.OFFICE}"{mime}><o:body>'
            + (body if body is not None else f'<o:{kind}/>') + f'</o:body></o:{root}>')


class CountingStream(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.bytes_read = 0
        self.reads = []

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        self.reads.append(size)
        return data


class SourceScanV22(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'renamed.unknown'

    def package(self, entries):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.source, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in entries:
                    archive.writestr(name, data)
        return self.source

    def opc(self, *, content=WORKBOOK, mime=CALC_TYPE, target='xl/workbook.xml',
            member=None, extra=(), encoding='utf-8', declarations=None, relationships='',
            mode='', reltype=OREL):
        member = member or target.lstrip('/')
        declarations = declarations if declarations is not None else (
            f'<Override PartName="/{member}" ContentType="{mime}"/>')
        return self.package([
            ('[Content_Types].xml', encoded(f'<Types xmlns="{CT}">{declarations}</Types>', encoding)),
            ('_rels/.rels', encoded(f'<Relationships xmlns="{REL}"><Relationship Id="r1" '
                                   f'Type="{reltype}" Target="{target}" {mode}/>'
                                   f'{relationships}</Relationships>', encoding)),
            (member, encoded(content, encoding) if isinstance(content, str) else content),
            *extra])

    def odf(self, *, xml=None, kind='text', extra=(), encoding='utf-8'):
        xml = odf_xml(kind) if xml is None else xml
        return self.package([('mimetype', 'application/vnd.oasis.opendocument.' + kind),
                             ('content.xml', encoded(xml, encoding) if isinstance(xml, str) else xml), *extra])

    def scan(self, path=None, **kwargs):
        path = path or self.source
        before = sha256(path.read_bytes()).digest()
        result = sc.inspect_source_contract(path, **kwargs)
        self.assertEqual(sha256(path.read_bytes()).digest(), before, 'scanner changed source bytes')
        return result

    def test_all_known_xml_main_types_and_models(self):
        # Every standard/template/macro-enabled declaration remains supported.
        for mime, family in sc._XML_MAIN_TYPES.items():
            for strict in (False, True):
                with self.subTest(mime=mime, strict=strict):
                    local, service = {'wordprocessingml': ('document', sc.WRITER),
                                      'spreadsheetml': ('workbook', sc.CALC),
                                      'presentationml': ('presentation', sc.IMPRESS)}[family]
                    ns = (f'http://purl.oclc.org/ooxml/{family}/main' if strict else
                          f'http://schemas.openxmlformats.org/{family}/2006/main')
                    self.opc(mime=mime, content=f'<m:{local} xmlns:m="{ns}"/>')
                    result = self.scan()
                    self.assertEqual(result['state'], 'recognized')
                    self.assertEqual(result['expectedServices'], [service])
                    self.assertFalse(sc.source_model_matches(result, []))
                    self.assertTrue(sc.source_model_matches(result, [service]))

    def test_registered_mime_casing_is_not_a_false_rejection(self):
        self.opc(mime='application/vnd.ms-excel.sheet.macroEnabled.main+xml')
        self.assertEqual(self.scan()['expectedServices'], [sc.CALC])

    def test_binary_workbook_is_deferred_not_parsed_as_xml_or_admitted(self):
        self.opc(mime='application/vnd.ms-excel.sheet.binary.macroEnabled.main',
                 target='xl/workbook.bin', content=b'\x83\x01\x00\x00\xff\x00BIFF12')
        with patch.object(sc, '_xml_root', side_effect=AssertionError('binary XML parse')):
            result = self.scan()
        self.assertEqual(result['state'], 'not_checked')
        self.assertEqual(result['reason'], 'source_contract_binary_main')
        self.assertTrue(sc.source_model_matches(result, [sc.CALC]))
        self.assertFalse(sc.source_model_matches(result, [sc.WRITER]))
        self.assertFalse(sc.source_model_matches(result, []))
        self.assertIsNone(sc.source_import_hint(result, SimpleNamespace(valid=True, filters={})))

    def test_binary_workbook_still_requires_unique_internal_main_part(self):
        for extra, target, member in [
                ([('XL/WORKBOOK.BIN', b'x')], 'xl/workbook.bin', 'xl/workbook.bin'),
                ([], '../workbook.bin', 'xl/workbook.bin'),
                ([], 'missing.bin', 'xl/workbook.bin')]:
            with self.subTest(target=target, extra=extra):
                self.opc(mime=sc._BINARY_CALC_MAIN, content=b'\xff', target=target,
                         member=member, extra=extra)
                self.assertEqual(self.scan()['state'], 'invalid')

    def test_unknown_or_lookalike_type_is_not_a_guessed_family(self):
        for mime in ['application/x-wordprocessingml.fake', 'application/x-ms-excel.blob',
                     'application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml',
                     'application/x-future-workbook']:
            with self.subTest(mime=mime):
                self.opc(mime=mime, content=b'\xff\x00not-xml')
                result = self.scan()
                self.assertEqual(result['state'], 'not_checked')
                self.assertNotIn('expectedServices', result)

    def test_known_type_with_wrong_root_or_namespace_is_rejected(self):
        for xml in ['<s:workbook xmlns:s="urn:fake"/>', '<sheet/>',
                    '<document xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>']:
            with self.subTest(xml=xml):
                self.opc(content=xml)
                self.assertEqual(self.scan()['reason'], 'ooxml_main_type_namespace_mismatch')

    def test_utf16_ooxml_declarations_and_main_are_inspected(self):
        for encoding in ('utf-16', 'utf-16-le', 'utf-16-be'):
            with self.subTest(encoding=encoding):
                self.opc(encoding=encoding)
                self.assertEqual(self.scan()['expectedServices'], [sc.CALC])

    def test_utf16_odf_and_flat_odf_are_inspected(self):
        for encoding in ('utf-16', 'utf-16-le', 'utf-16-be'):
            for kind, service in [('text', sc.WRITER), ('spreadsheet', sc.CALC),
                                  ('presentation', sc.IMPRESS), ('drawing', sc.DRAW)]:
                # graphics is the MIME family; drawing is the XML body element.
                mime_kind = 'graphics' if kind == 'drawing' else kind
                with self.subTest(encoding=encoding, kind=kind):
                    self.odf(kind=mime_kind, xml=odf_xml(kind), encoding=encoding)
                    self.assertEqual(self.scan()['expectedServices'], [service])
                    xml = odf_xml(mime_kind, flat=True, body=f'<o:{kind}/>')
                    self.source.write_bytes(encoded(xml, encoding))
                    self.assertEqual(self.scan()['expectedServices'], [service])

    def test_flat_alias_retains_filter_and_native_model_requirements(self):
        for family, service, filter_name in [
                ('text', sc.WRITER, 'OpenDocument Text Flat XML'),
                ('presentation', sc.IMPRESS, 'OpenDocument Presentation Flat XML')]:
            with self.subTest(family=family):
                self.source.write_bytes(encoded(odf_xml(family + '-flat-xml', flat=True,
                                                       body=f'<o:{family}/>'), 'utf-16'))
                result = self.scan()
                self.assertTrue(result['nativeImportRequired'])
                self.assertIsNone(sc.source_import_hint(result, SimpleNamespace(valid=True, filters={})))
                native = SimpleNamespace(valid=True, filters={filter_name: (service, (), {'IMPORT'})})
                self.assertEqual(sc.source_import_hint(result, native)['filter'], filter_name)
                self.assertFalse(sc.source_model_matches(result, [sc.CALC]))
                self.assertFalse(sc.source_model_matches(result, []))

    def test_wrong_flat_alias_body_cannot_be_excused_by_encoding(self):
        for body in ['<o:spreadsheet/>', '', '<x:text xmlns:x="urn:fake"/>']:
            with self.subTest(body=body):
                self.source.write_bytes(encoded(odf_xml('text-flat-xml', flat=True, body=body), 'utf-16'))
                self.assertEqual(self.scan()['state'], 'invalid')

    def test_standalone_xml_constrains_models_by_exact_namespace(self):
        for root, (content_class, service) in sc._STANDALONE_XML.items():
            namespace, local = root[1:].split('}')
            for encoding in ('utf-8', 'utf-16-be'):
                with self.subTest(root=root, encoding=encoding):
                    self.source.write_bytes(encoded(f'<r:{local} xmlns:r="{namespace}"/>', encoding))
                    result = self.scan()
                    self.assertEqual(result['contentClass'], content_class)
                    self.assertEqual(result['expectedServices'], [service])
                    self.assertFalse(sc.source_model_matches(result, []))
                    self.assertFalse(sc.source_model_matches(result, [sc.DRAW]))
        for xml in ['<math/>', '<Workbook xmlns="urn:fake"/>', '<wordDocument/>']:
            self.source.write_text(xml)
            self.assertEqual(self.scan()['state'], 'not_applicable')

    def test_long_xml_prolog_no_longer_hides_odf_declaration(self):
        self.source.write_text('<!--' + 'x' * 70000 + '-->' + odf_xml(flat=True))
        self.assertEqual(self.scan()['expectedServices'], [sc.WRITER])

    def test_utf8_bom_and_namespace_text_inside_comment(self):
        self.source.write_bytes(b'\xef\xbb\xbf' + encoded('<!--' + sc.OFFICE + '-->' + odf_xml(flat=True)))
        self.assertEqual(self.scan()['expectedServices'], [sc.WRITER])
        self.source.write_text('<!--' + sc.OFFICE + '--><notOffice/>')
        self.assertEqual(self.scan()['state'], 'not_applicable')

    def test_literal_dtd_markers_in_comments_are_not_declarations(self):
        comment = '<!-- literal <!DOCTYPE fake> and <!ENTITY x "test"> -->'
        self.odf(xml=comment + odf_xml())
        self.assertEqual(self.scan()['state'], 'recognized')
        self.opc(declarations=comment + f'<Default Extension="xml" ContentType="{CALC_TYPE}"/>')
        self.assertEqual(self.scan()['state'], 'recognized')

    def test_actual_dtds_and_entities_are_deferred_before_expansion(self):
        for doctype in ['<!DOCTYPE x SYSTEM "file:///not-read">',
                        '<!DOCTYPE x [<!ENTITY a "expanded">]>',
                        '<!DOCTYPE x [<!ENTITY % a SYSTEM "https://not-fetched.invalid/">%a;]>']:
            for encoding in ('utf-8', 'utf-16-be'):
                with self.subTest(doctype=doctype, encoding=encoding):
                    self.odf(xml=doctype + odf_xml(), encoding=encoding)
                    self.assertEqual(self.scan()['reason'], 'source_contract_dtd_entities_or_encoding')
                    self.opc(declarations='', encoding=encoding)
                    # Exercise the metadata parser, not just the root probe.
                    with self.assertRaises(sc._UntrustedXML):
                        sc._metadata_xml(encoded(doctype + '<Types/>', encoding))

    def test_split_doctype_and_entity_declarations_remain_deferred(self):
        # Put the actual declaration across the first 4096-byte read boundary.
        prefix = '<!--' + 'x' * 4080 + '-->'
        self.source.write_text(prefix + '<!DOCTYPE x [<!ENTITY y "z">]>' + odf_xml(flat=True))
        self.assertEqual(self.scan()['state'], 'not_checked')

    def test_unknown_and_utf32_encodings_are_not_claimed_corrupt(self):
        for xml in [encoded(odf_xml(), 'utf-32'),
                    b'<?xml version="1.0" encoding="not-an-encoding"?><x/>',
                    b'<?xml version="1.0" encoding="shift_jis"?><x/>']:
            with self.subTest(prefix=xml[:60]):
                self.odf(xml=xml)
                self.assertEqual(self.scan()['state'], 'not_checked')
                with self.assertRaises(sc._UntrustedXML):
                    sc._metadata_xml(xml)

    def test_encrypted_odf_has_explicit_unknown_not_corrupt_verdict(self):
        manifest = (f'<m:manifest xmlns:m="{MANIFEST}"><m:file-entry m:full-path="content.xml">'
                    '<m:encryption-data/></m:file-entry></m:manifest>')
        self.odf(xml=b'\xff\x00\x81encrypted', extra=[('META-INF/manifest.xml', manifest)])
        self.assertEqual(self.scan()['reason'], 'source_contract_encrypted')

    def test_foreign_or_unrelated_encryption_marker_does_not_excuse_corruption(self):
        for namespace, name in [('urn:fake', 'content.xml'), (MANIFEST, 'Pictures/photo.png')]:
            manifest = (f'<m:manifest xmlns:m="{namespace}"><m:file-entry m:full-path="{name}">'
                        '<m:encryption-data/></m:file-entry></m:manifest>')
            with self.subTest(namespace=namespace, name=name):
                self.odf(xml=b'\xffbroken', extra=[('META-INF/manifest.xml', manifest)])
                self.assertEqual(self.scan()['state'], 'invalid')

    def test_ordinary_odf_never_reads_manifest_or_media(self):
        self.odf(extra=[('META-INF/manifest.xml', b'not XML'), ('Pictures/huge.bin', b'x' * 200000)])
        calls = []
        original = zipfile.ZipFile.open
        def observed(archive, name, *args, **kwargs):
            calls.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
            return original(archive, name, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', observed):
            self.assertEqual(self.scan()['state'], 'recognized')
        self.assertEqual(calls, ['mimetype', 'content.xml'])

    def test_password_protected_zip_is_unknown_not_corrupt(self):
        self.odf()
        data = bytearray(self.source.read_bytes())
        # Mark only the content member as password-protected in both headers.
        for signature, flag_offset, name_offset in [(b'PK\x03\x04', 6, 30), (b'PK\x01\x02', 8, 46)]:
            start = 0
            while (pos := data.find(signature, start)) >= 0:
                if data[pos + name_offset:pos + name_offset + 11] == b'content.xml':
                    flags = struct.unpack_from('<H', data, pos + flag_offset)[0]
                    struct.pack_into('<H', data, pos + flag_offset, flags | 1)
                start = pos + 4
        self.source.write_bytes(data)
        self.assertEqual(self.scan()['reason'], 'source_contract_archive_unavailable')

    def test_unavailable_decompressor_is_unknown_not_corrupt(self):
        self.odf()
        original = zipfile.ZipFile.open
        def unavailable(archive, name, *args, **kwargs):
            if name == 'content.xml':
                raise NotImplementedError('unsupported method')
            return original(archive, name, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', unavailable):
            self.assertEqual(self.scan()['state'], 'not_checked')

    def test_bad_deflate_and_eof_are_structural_failures_not_uncaught_exceptions(self):
        for error in [zlib.error('invalid deflate'), EOFError('truncated')]:
            self.odf()
            original = zipfile.ZipFile.open
            def broken(archive, name, *args, **kwargs):
                if name == 'content.xml':
                    raise error
                return original(archive, name, *args, **kwargs)
            with patch.object(zipfile.ZipFile, 'open', broken):
                self.assertEqual(self.scan()['state'], 'invalid')

    def test_bad_xml_before_declaration_is_rejected(self):
        self.odf(xml='<unclosed')
        self.assertEqual(self.scan()['state'], 'invalid')

    def test_root_probe_is_not_whole_document_validation(self):
        self.opc(content='<s:workbook xmlns:s="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                         + '<badly-closed>')
        result = self.scan()
        self.assertEqual(result['state'], 'recognized')
        self.assertFalse(sc.source_model_matches(result, [sc.WRITER]))
        # The native importer (outside this structural scanner) must still load it.

    def test_odf_mimetype_and_body_mismatch_stays_invalid(self):
        self.odf(kind='spreadsheet', xml=odf_xml('text'))
        self.assertEqual(self.scan()['reason'], 'mimetype_body_mismatch')

    def test_formula_package_and_legacy_namespace_remain_supported(self):
        self.odf(kind='formula', xml=f'<math xmlns="{sc.MATHML}"><mi>x</mi></math>')
        self.assertEqual(self.scan()['expectedServices'], [sc.FORMULA])
        self.package([('mimetype', 'application/vnd.sun.xml.writer'),
                      ('content.xml', f'<o:document-content xmlns:o="{sc.LEGACY_OFFICE}"><o:body/></o:document-content>')])
        self.assertEqual(self.scan()['expectedServices'], [sc.WRITER])

    def test_duplicate_critical_members_are_rejected(self):
        for name, content in [('mimetype', 'application/vnd.oasis.opendocument.text'),
                              ('content.xml', odf_xml())]:
            self.odf(extra=[(name, content)])
            self.assertEqual(self.scan()['state'], 'invalid')
        for name, content in [('[Content_Types].xml', '<Types/>'), ('_rels/.rels', '<Relationships/>'),
                              ('xl/workbook.xml', WORKBOOK), ('XL/WORKBOOK.XML', WORKBOOK)]:
            with self.subTest(name=name):
                self.opc(extra=[(name, content)])
                self.assertEqual(self.scan()['state'], 'invalid')

    def test_duplicate_epub_mimetype_is_not_a_clean_recognition(self):
        self.package([('mimetype', 'application/epub+zip'), ('mimetype', 'application/epub+zip')])
        self.assertEqual(self.scan()['state'], 'invalid')

    def test_unrelated_mimetype_does_not_mask_ooxml_type_checks(self):
        self.opc(extra=[('mimetype', 'application/x-extra')])
        self.assertEqual(self.scan()['expectedServices'], [sc.CALC])
        self.opc(mime=WRITER_TYPE, extra=[('mimetype', 'application/x-extra')])
        self.assertEqual(self.scan()['state'], 'invalid')

    def test_unsafe_relationship_targets_remain_rejected(self):
        for target in ['../xl/workbook.xml', '//host/xl/workbook.xml', 'https://host/xl/workbook.xml',
                       'file:///xl/workbook.xml', '/xl/workbook.xml#', '/xl/workbook.xml?',
                       'xl\\workbook.xml', '/xl/%2fworkbook.xml', '/xl/%2e%2e/workbook.xml',
                       '/xl/%00.xml', '/xl/%09.xml', '/xl/%bad%xx.xml']:
            with self.subTest(target=target):
                self.opc(target=target, member='xl/workbook.xml')
                self.assertEqual(self.scan()['state'], 'invalid')
        self.opc(mode='TargetMode="External"')
        self.assertEqual(self.scan()['state'], 'invalid')

    def test_legal_aliases_dot_resolution_and_encoded_parts_remain_supported(self):
        for target, member in [('/XL/WORKBOOK.XML', 'xl/workbook.xml'),
                               ('./xl/workbook.xml', 'xl/workbook.xml'),
                               ('unused/../xl/workbook.xml', 'xl/workbook.xml'),
                               ('/xl/sheet%20data.xml', 'xl/sheet%20data.xml'),
                               ('/xl/%c3%a9.xml', 'xl/%C3%A9.xml')]:
            with self.subTest(target=target):
                self.opc(target=target, member=member)
                self.assertEqual(self.scan()['expectedServices'], [sc.CALC])

    def test_override_precedence_and_duplicate_declarations(self):
        self.opc(declarations=f'<Default Extension="XML" ContentType="{WRITER_TYPE}"/>'
                             f'<Override PartName="/XL/WORKBOOK.XML" ContentType="{CALC_TYPE}"/>')
        self.assertEqual(self.scan()['contentTypeBasis'], 'override')
        for declaration in [f'<Default Extension="xml" ContentType="{CALC_TYPE}"/>',
                            f'<Override PartName="/xl/workbook.xml" ContentType="{CALC_TYPE}"/>']:
            self.opc(declarations=declaration * 2)
            self.assertEqual(self.scan()['state'], 'invalid')

    def test_relationship_ids_and_main_uniqueness_remain_mandatory(self):
        for second in [f'<Relationship Id="r2" Type="{OREL}" Target="xl/workbook.xml"/>',
                       '<Relationship Id="r1" Type="urn:other" Target="other.xml"/>']:
            self.opc(relationships=second)
            self.assertEqual(self.scan()['state'], 'invalid')
        self.opc(reltype='urn:fake/officeDocument')
        self.assertEqual(self.scan()['state'], 'invalid')

    def test_budget_is_unknown_not_invalid_or_compatibility(self):
        for budget in (0, 1, 10):
            self.opc()
            self.assertEqual(self.scan(budget=budget)['state'], 'not_checked')
            self.odf()
            self.assertEqual(self.scan(budget=budget)['state'], 'not_checked')
        self.source.write_text('<!--' + 'x' * 70000 + '-->' + odf_xml(flat=True))
        self.assertEqual(self.scan(budget=4096)['state'], 'not_checked')

    def test_depth_budget_does_not_become_corruption(self):
        xml = f'<o:document-content xmlns:o="{sc.OFFICE}">' + '<x>' * 300 + '</x>' * 300 + '</o:document-content>'
        self.odf(xml=xml)
        self.assertEqual(self.scan()['reason'], 'source_contract_budget')

    def test_cancel_is_observed_even_for_unknown_plain_text(self):
        cancelled = threading.Event(); cancelled.set()
        self.source.write_text('plain text')
        with self.assertRaises(GuardError) as error:
            self.scan(cancel=cancelled)
        self.assertEqual(error.exception.code, 'CANCELLED')

    def test_cancel_is_observed_during_large_prolog(self):
        cancelled = threading.Event()
        class CancelStream(CountingStream):
            def read(self, size=-1):
                data = super().read(size)
                cancelled.set()
                return data
        stream = CancelStream(('<!--' + 'x' * 70000 + '-->' + odf_xml()).encode())
        with self.assertRaises(GuardError):
            sc._root_and_body(stream, cancel=cancelled, budget=4 * 1024 * 1024)
        self.assertLessEqual(stream.bytes_read, 4096)

    def test_header_probe_does_not_read_or_build_full_document(self):
        for xml, probe in [(WORKBOOK[:-2] + '>' + '<x/>' * 100000 + '</s:workbook>', sc._xml_root),
                           (odf_xml(body='<o:text>' + '<p>x</p>' * 100000 + '</o:text>'), sc._root_and_body)]:
            stream = CountingStream(xml.encode())
            probe(stream, cancel=None, budget=4 * 1024 * 1024)
            self.assertLessEqual(stream.bytes_read, 4096)
            self.assertEqual(len(stream.reads), 1)

    def test_budget_caps_actual_stream_reads(self):
        stream = CountingStream(('<!--' + 'x' * 10000 + '-->' + odf_xml()).encode())
        with self.assertRaises(sc._Budget):
            sc._root_and_body(stream, cancel=None, budget=100)
        self.assertEqual(stream.bytes_read, 100)

    def test_flat_probe_reuses_prefix_without_seek_or_second_read(self):
        stream = CountingStream(odf_xml(flat=True).encode())
        with patch.object(Path, 'open', return_value=stream):
            self.assertEqual(sc.inspect_source_contract(self.source)['expectedServices'], [sc.WRITER])
        self.assertEqual(len(stream.reads), 1)

    def test_plain_text_scan_does_not_invoke_xml_or_archive(self):
        self.source.write_bytes(b'plain text ' * 100000)
        with patch.object(sc, '_probe_xml', side_effect=AssertionError('XML invoked')), \
             patch.object(sc.zipfile, 'ZipFile', side_effect=AssertionError('ZIP invoked')):
            self.assertEqual(self.scan()['state'], 'not_applicable')

    def test_missing_source_is_unavailable_not_corrupt(self):
        self.assertEqual(sc.inspect_source_contract(self.source)['reason'], 'source_contract_unavailable')


if __name__ == '__main__':
    unittest.main()
