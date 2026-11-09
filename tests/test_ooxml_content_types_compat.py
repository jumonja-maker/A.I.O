"""Namespace interoperability may never rewrite originals or signed packages."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from app_backend.converter_guard import ooxml_content_types as compat
from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.source_contract import WRITER, CALC


class ContentTypesNamespaceCompatibility(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()

    def source(self, *, ct=None, extra=(), rel=None):
        path = self.root/'original.docm'
        ct = ct or ('<p:Types xmlns:p="'+compat.CT+'">'
            '<p:Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<p:Default Extension="xml" ContentType="application/xml"/>'
            '<p:Override PartName="/word/document.xml" ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>'
            '</p:Types>')
        rel = rel or ('<Relationships xmlns="'+compat.RELS+'">'
            '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>')
        with ZipFile(path, 'w') as archive:
            archive.writestr('[Content_Types].xml',ct)
            archive.writestr('_rels/.rels',rel)
            archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>UNCHANGED_00123</w:t></w:r></w:p></w:body></w:document>')
            for name, data in extra:
                archive.writestr(name,data)
        return path

    def prepare(self, source, output='prepared', **kwargs):
        return compat.prepare_content_types_source(source,self.root/output,cancel=self.cancel,
                                                   native_failure_code='PREFLIGHT_IMPORT_UNDETERMINED',**kwargs)

    def test_only_content_types_bytes_change_and_expanded_semantics_remain_equal(self):
        source=self.source(extra=[('word/vbaProject.bin',b'opaque bytes never interpreted here')])
        # The opaque part needs its ordinary finite declaration.
        with ZipFile(source) as archive:
            entries=[(n,archive.read(n)) for n in archive.namelist()]
        with ZipFile(source,'w') as archive:
            for name,data in entries:
                if name=='[Content_Types].xml':
                    data=data.replace(b'</p:Types>',b'<p:Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/></p:Types>')
                archive.writestr(name,data)
        before=source.read_bytes()
        prepared,summary=self.prepare(source)
        self.assertNotEqual(source,prepared)
        self.assertEqual(source.read_bytes(),before)
        with ZipFile(source) as a,ZipFile(prepared) as b:
            self.assertEqual(a.namelist(),b.namelist())
            self.assertEqual([n for n in a.namelist() if a.read(n)!=b.read(n)],['[Content_Types].xml'])
            self.assertEqual(compat._facts(ET.fromstring(a.read('[Content_Types].xml'))),
                             compat._facts(ET.fromstring(b.read('[Content_Types].xml'))))
            self.assertEqual(b.read('word/vbaProject.bin'),b'opaque bytes never interpreted here')
        self.assertTrue(summary['otherPartBytesPreserved'])

    def test_no_preparation_without_an_actual_eligible_native_failure(self):
        source=self.source()
        for code in (None,'PREFLIGHT_TIMEOUT','SOURCE_MODEL_MISMATCH','SOURCE_CONTAINER_INCONSISTENT'):
            with self.subTest(code=code):
                path,summary=compat.prepare_content_types_source(source,self.root/'none',cancel=self.cancel,native_failure_code=code)
                self.assertEqual(path,source);self.assertIsNone(summary)
                self.assertFalse((self.root/'none').exists())

    def test_already_default_namespace_is_untouched(self):
        source=self.source()
        with ZipFile(source) as archive: raw=archive.read('[Content_Types].xml').decode()
        source=self.source(ct=raw.replace('<p:','<').replace('</p:','</').replace('xmlns:p=','xmlns='))
        self.assertEqual(self.prepare(source),(source,None))

    def test_package_signatures_and_vba_signatures_are_not_rewritten(self):
        for name in ('_xmlsignatures/sig1.xml','word/vbaProjectSignature.bin'):
            with self.subTest(name=name):
                source=self.source(extra=[(name,b'signed payload')]);before=source.read_bytes()
                self.assertEqual(self.prepare(source),(source,None))
                self.assertEqual(source.read_bytes(),before)
                self.assertFalse((self.root/'prepared').exists())

    def test_signature_content_type_declines_even_without_a_familiar_part_name(self):
        source=self.source()
        with ZipFile(source) as z: ct=z.read('[Content_Types].xml').decode()
        source=self.source(ct=ct.replace('application/xml','application/vnd.openxmlformats-package.digital-signature-xmlsignature+xml'))
        self.assertEqual(self.prepare(source),(source,None))

    def test_unknown_namespaces_attributes_nodes_and_dtd_are_declined(self):
        source=self.source()
        with ZipFile(source) as z: ct=z.read('[Content_Types].xml').decode()
        variants=(ct.replace('<p:Types','<p:Types unknown="data"'),
                  ct.replace('<p:Types','<p:Types xmlns:q="urn:unknown"'),
                  ct.replace('</p:Types>','<p:Unknown/></p:Types>'),
                  '<!DOCTYPE p:Types [<!ENTITY x "boom">]>'+ct,
                  '<?test instruction?>'+ct)
        for raw in variants:
            with self.subTest(raw=raw[:60]):
                source=self.source(ct=raw)
                self.assertEqual(self.prepare(source),(source,None))

    def test_duplicate_case_alias_and_unsafe_member_names_are_declined(self):
        for name in ('word/document.xml','WORD/DOCUMENT.XML','../outside.xml','word/%64ocument.xml'):
            with self.subTest(name=name):
                source=self.source(extra=[(name,b'other')])
                self.assertEqual(self.prepare(source),(source,None))

    def test_external_relationships_do_not_change_their_base_uri(self):
        rel=('<Relationships xmlns="'+compat.RELS+'"><Relationship Id="external" Type="urn:link" '
             'Target="../source.html" TargetMode="External"/></Relationships>')
        source=self.source(rel=rel)
        self.assertEqual(self.prepare(source),(source,None))

    def test_encrypted_and_budget_exceeded_archives_are_declined(self):
        source=self.source();raw=bytearray(source.read_bytes())
        index=raw.index(b'PK\x01\x02');raw[index+8]|=1;source.write_bytes(raw)
        self.assertEqual(self.prepare(source),(source,None))
        source=self.source()
        with patch.object(compat,'MAX_EXPANDED',1):
            self.assertEqual(self.prepare(source),(source,None))
        with patch.object(compat,'MAX_METADATA',1):
            self.assertEqual(self.prepare(source),(source,None))

    def test_conversion_recreates_identical_copy_and_rejects_changed_original(self):
        source=self.source();prepared,summary=self.prepare(source)
        second,again=self.prepare(source,output='again',expected=summary)
        self.assertEqual(prepared.read_bytes(),second.read_bytes())
        self.assertEqual(summary['preparedSha256'],again['preparedSha256'])
        with ZipFile(source,'a') as archive:archive.writestr('changed.xml',b'<changed/>')
        with self.assertRaises(GuardError) as caught:
            self.prepare(source,output='changed',expected=summary)
        self.assertEqual(caught.exception.code,'SOURCE_CHANGED_DURING_PREPARATION')
        self.assertFalse((self.root/'changed').exists())

    def test_cancellation_propagates_instead_of_becoming_native_success(self):
        source=self.source();self.cancel.set()
        with self.assertRaises(GuardError) as caught:self.prepare(source)
        self.assertEqual(caught.exception.code,'CANCELLED')
        self.assertFalse((self.root/'prepared').exists())

    def test_explicit_spool_budget_covers_private_copy_and_removes_partial_file(self):
        source=self.source();before=source.read_bytes()
        with self.assertRaises(GuardError) as caught:self.prepare(source,copy_budget=lambda:1)
        self.assertEqual(caught.exception.code,'SPOOL_QUOTA')
        self.assertEqual(source.read_bytes(),before)
        self.assertFalse((self.root/'prepared'/source.name).exists())
        with self.assertRaises(GuardError) as caught:self.prepare(source,output='zero',copy_budget=lambda:0)
        self.assertEqual(caught.exception.code,'SPOOL_QUOTA')
        self.assertFalse((self.root/'zero').exists())

    def test_adapter_requires_second_actual_model_and_never_promotes_a_mismatch(self):
        for services,accepted in (([WRITER],True),([CALC],False)):
            with self.subTest(services=services):
                source=self.source();adapter=LibreOfficeAdapter(source)
                contract={'state':'recognized','contentClass':'ooxml_package','expectedServices':[WRITER]}
                first={'state':'undetermined','code':'SOURCE_IMPORT_FAILED','sourceContract':contract}
                second={'state':'detected','basis':'native_preflight_uno_document','modelLoaded':True,
                        'documentServices':services,'registryFingerprint':'same','sourceContract':contract}
                workspace=self.root/('probe-'+str(accepted))
                with patch.object(adapter,'_registry',return_value=SimpleNamespace()), \
                     patch.object(adapter,'_preflight_native',side_effect=[first,second]) as native, \
                     patch('app_backend.converter_guard.qt_media.media_preflight',return_value=None):
                    result=adapter.preflight(source,workspace,cancel=self.cancel,timeout=30)
                self.assertEqual(native.call_count,2)
                self.assertEqual(result['state'],'detected' if accepted else 'undetermined')
                if accepted:self.assertEqual(result['sourcePreparation']['kind'],compat.KIND)
                else:self.assertNotIn('sourcePreparation',result)


if __name__=='__main__':unittest.main()
