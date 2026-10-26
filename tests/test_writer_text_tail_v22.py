"""Reproduce terminal OOXML table preparation without starting an office."""
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import warnings
import zipfile
from xml.etree import ElementTree as ET

from app_backend.converter_guard import writer_text_tail as tail
from app_backend.converter_guard.errors import GuardError

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
STRICT_W = 'http://purl.oclc.org/ooxml/wordprocessingml/main'
BODY = ('<w:p><w:r><w:t>AIO_TEXT_BODY_22 Русский</w:t></w:r></w:p>'
        '<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid>'
        '<w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>001</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:tcPr/><w:p><w:r><w:t>1.43</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>Текст ячейки</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:tcPr/><w:p><w:r><w:t>AIO_LAST_CELL_22</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')


def seed(path, *, body=BODY, namespace=W, section=True, external=False, main='word/document.xml'):
    """Valid small Word controls, including macro-enabled containers without VBA."""
    ct = ('application/vnd.ms-word.document.macroEnabled.main+xml' if path.suffix == '.docm'
          else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml')
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="' + namespace + '"><w:body>' + body +
                ('<w:sectPr/>' if section else '') + '</w:body></w:document>').encode('utf-8')
    with zipfile.ZipFile(path, 'x', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/' + main + '" ContentType="' + ct + '"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="' + tail.REL + '"><Relationship Id="rId1" Type="' +
            'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' +
            '" Target="' + main + '"' + (' TargetMode="External"' if external else '') + '/></Relationships>')
        z.writestr(main, document)
        z.writestr('word/preserved.bin', b'unchanged package bytes\0\xff')
    return document


class WriterTextTailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()

    def prep(self, source, workspace=None):
        return tail.prepare_writer_text_source(source, workspace or self.root/'private', cancel=self.cancel)

    def test_one_empty_paragraph_only_all_other_payloads_and_original_unchanged(self):
        source = self.root/'document.docx'
        original_xml = seed(source)
        original = source.read_bytes()
        with self.prep(source) as (prepared, detail):
            self.assertNotEqual(source, prepared)
            self.assertEqual(prepared.name, source.name)
            self.assertFalse(detail['originalModified'])
            with zipfile.ZipFile(source) as a, zipfile.ZipFile(prepared) as b:
                self.assertEqual(a.namelist(), b.namelist())
                for n in a.namelist():
                    if n != 'word/document.xml':
                        self.assertEqual(a.read(n), b.read(n))
                xml = b.read('word/document.xml')
                addition = ('<aioTextTail:p xmlns:aioTextTail="' + W + '"/>').encode()
                self.assertEqual(xml.replace(addition, b'', 1), original_xml)
                root = ET.fromstring(xml)
                self.assertEqual([x.tag for x in root.find('{'+W+'}body')][-3:],
                                 ['{'+W+'}tbl', '{'+W+'}p', '{'+W+'}sectPr'])
                self.assertEqual(detail['originalMainXmlSha256'], hashlib.sha256(original_xml).hexdigest())
                self.assertEqual(detail['preparedMainXmlSha256'], hashlib.sha256(xml).hexdigest())
                self.assertIsNone(b.testzip())
            self.assertEqual(source.read_bytes(), original)
        self.assertFalse(prepared.exists())
        self.assertEqual(source.read_bytes(), original)

    def test_macro_enabled_container_is_not_relabelled(self):
        source = self.root/'document.docm'; seed(source)
        with self.prep(source) as (p, info), zipfile.ZipFile(p) as z:
            self.assertEqual(p.suffix, '.docm')
            self.assertIn(b'macroEnabled.main+xml', z.read('[Content_Types].xml'))
            self.assertEqual(info['kind'], 'writer_text_terminal_table')

    def test_content_not_filename(self):
        source = self.root/'document.unlisted'; seed(source)
        with self.prep(source) as (p, info): self.assertIsNotNone(info)

    def test_strict_namespace_and_nonstandard_internal_main_part(self):
        source = self.root/'strict.docx'; seed(source, namespace=STRICT_W, main='custom/main.xml')
        with self.prep(source) as (p, info), zipfile.ZipFile(p) as z:
            self.assertEqual(info['mainPart'], 'custom/main.xml')
            root = ET.fromstring(z.read('custom/main.xml'))
            self.assertEqual(root.find('{'+STRICT_W+'}body')[-2].tag, '{'+STRICT_W+'}p')

    def test_no_section_properties(self):
        source = self.root/'bare.docx'; seed(source, section=False)
        with self.prep(source) as (p, info), zipfile.ZipFile(p) as z:
            self.assertEqual(ET.fromstring(z.read('word/document.xml')).find('{'+W+'}body')[-1].tag, '{'+W+'}p')

    def test_existing_terminal_paragraph_no_extra_copy_or_paragraph(self):
        source = self.root/'healthy.docx'; seed(source, body=BODY+'<w:p/>')
        with self.prep(source) as (p, info):
            self.assertEqual(p, source); self.assertIsNone(info)
            self.assertFalse((self.root/'private').exists())

    def test_plain_source_and_missing_source_retain_native_error_path(self):
        source = self.root/'plain.txt'; source.write_text('plain')
        for p in (source, self.root/'absent.docx'):
            with self.prep(p) as (actual, info):self.assertEqual(actual, p);self.assertIsNone(info)

    def test_no_table_no_staging(self):
        source=self.root/'simple.docx';seed(source,body='<w:p><w:r><w:t>abc</w:t></w:r></w:p>')
        with self.prep(source) as (p, info):self.assertEqual(p,source);self.assertIsNone(info)

    def test_external_relationship_never_followed(self):
        source=self.root/'external.docx';seed(source,external=True)
        with self.prep(source) as (p, info):self.assertEqual(p,source);self.assertIsNone(info)

    def test_duplicate_zip_entries_are_not_rewritten(self):
        source=self.root/'duplicates.docx';doc=seed(source)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(source,'a') as z:z.writestr('word/document.xml',doc)
        with self.prep(source) as (p, info):self.assertEqual(p,source);self.assertIsNone(info)

    def test_dtd_and_unsupported_encoding_not_rewritten(self):
        for kind in ('dtd','utf16'):
            folder=self.root/kind;folder.mkdir();source=folder/'file.docx';xml=seed(source)
            with zipfile.ZipFile(source) as z:parts={n:z.read(n) for n in z.namelist()}
            if kind=='dtd':parts['word/document.xml']=xml.replace(b'<w:document',b'<!DOCTYPE w:document []><w:document',1)
            else:parts['word/document.xml']=xml.decode().replace('UTF-8','UTF-16').encode('utf-16')
            source.unlink()  # Only this synthetic test fixture.
            with zipfile.ZipFile(source,'x') as z:
                for name,data in parts.items():z.writestr(name,data)
            with self.prep(source) as (p, info):self.assertEqual(p,source);self.assertIsNone(info)

    def test_signed_package_is_left_to_native_import_without_rewriting(self):
        source=self.root/'signed.docx';seed(source)
        with zipfile.ZipFile(source,'a') as z:z.writestr('_xmlsignatures/sig1.xml','<Signature/>')
        before=source.read_bytes()
        with self.prep(source) as (p, info):self.assertEqual(p,source);self.assertIsNone(info)
        self.assertEqual(source.read_bytes(),before)

    def test_cancel_before_staging(self):
        source=self.root/'cancel.docx';seed(source);self.cancel.set()
        with self.assertRaises(GuardError) as exc:
            with self.prep(source):pass
        self.assertEqual(exc.exception.code,'CANCELLED');self.assertFalse((self.root/'private').exists())

    def test_cancel_during_copy_cleans_only_own_workspace(self):
        source=self.root/'cancel-copy.docx';seed(source);original=source.read_bytes()
        check=tail._check
        def stop(cancel):
            if (self.root/'private').exists():cancel.set()
            check(cancel)
        with mock.patch.object(tail,'_check',side_effect=stop):
            with self.assertRaises(GuardError):
                with self.prep(source):pass
        self.assertFalse((self.root/'private').exists());self.assertEqual(source.read_bytes(),original)

    def test_preexisting_workspace_is_not_removed_or_overwritten(self):
        source=self.root/'collision.docx';seed(source)
        work=self.root/'private';work.mkdir();marker=work/'unrelated.txt';marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            with self.prep(source):pass
        self.assertEqual(marker.read_text(),'keep')

    def test_known_terminal_source_probe_change_is_not_silently_ignored(self):
        source=self.root/'changed.docx';seed(source);original=source.read_bytes()
        probe=tail._terminal_table
        def wrong_digest(stream,cancel):
            offset,addition,digest=probe(stream,cancel);return offset,addition,'0'*64
        with mock.patch.object(tail,'_terminal_table',side_effect=wrong_digest):
            with self.assertRaises(GuardError) as exc:
                with self.prep(source):pass
        self.assertEqual(exc.exception.code,'SOURCE_CHANGED_DURING_PREPARATION')
        self.assertFalse((self.root/'private').exists());self.assertEqual(source.read_bytes(),original)

    def test_cleanup_on_native_export_exception(self):
        source=self.root/'export-failure.docx';seed(source)
        with self.assertRaisesRegex(RuntimeError,'native failure'):
            with self.prep(source):raise RuntimeError('native failure')
        self.assertFalse((self.root/'private').exists());self.assertTrue(source.exists())

    def test_repeated_targets_use_fresh_private_copy_and_clean_up(self):
        source=self.root/'repeat.docx';seed(source)
        for _ in range(4):
            with self.prep(source) as (p,info):self.assertTrue(p.is_file());self.assertIsNotNone(info)
            self.assertFalse((self.root/'private').exists())


if __name__=='__main__': unittest.main()
