"""WordML regional text/resources survive the native XHTML omission."""
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zlib
from unittest.mock import patch

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.wordml_page_regions import restore_wordml_page_regions, W, H


def document(regions='', binary=''):
    return ('<?xml version="1.0" encoding="UTF-8"?><w:wordDocument xmlns:w="'+W+'" '
            'xmlns:v="urn:schemas-microsoft-com:vml"><w:body><w:p><w:r><w:t>BODY</w:t></w:r></w:p>'
            '<w:sectPr>'+regions+'</w:sectPr>'+binary+'</w:body></w:wordDocument>')


def region(kind, text, variant='odd'):
    from xml.sax.saxutils import escape
    return '<w:'+kind+' w:type="'+variant+'"><w:p><w:r><w:t>'+escape(text)+'</w:t></w:r></w:p></w:'+kind+'>'


def image_png():
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,6,0,0,0))+
            chunk(b'IDAT',zlib.compress(b'\0\xff\0\0\xff'))+chunk(b'IEND',b''))


class WordMLPageRegions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.output=self.root/'output';self.output.mkdir()
        self.source=self.root/'A & B.xml';self.primary=self.output/'A & B.htm'
        self.before=b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>native</title></head><body><p>BODY</p></body></html>'
        self.primary.write_bytes(self.before);self.cancel=threading.Event()

    def convert(self, text):
        self.source.write_text(text,encoding='utf-8');original=self.source.read_bytes()
        paths,check=restore_wordml_page_regions(self.source,[self.primary],'htm',cancel=self.cancel)
        self.assertEqual(self.source.read_bytes(),original)
        return paths,check

    def test_text_regions_and_primary_bytes_are_preserved_with_visible_link(self):
        paths,check=self.convert(document(region('hdr','Верхний Я🙂')+region('ftr','Нижний <script>alert(1)</script>','even')))
        self.assertEqual(check['regionCount'],2);self.assertEqual(check['sourceSha256'],hashlib.sha256(self.source.read_bytes()).hexdigest())
        companion=self.output/check['companion'];text=''.join(ET.parse(companion).getroot().itertext())
        self.assertIn('Верхний Я🙂',text);self.assertIn('Нижний <script>alert(1)</script>',text)
        self.assertNotIn('<script>',companion.read_text())
        modified=self.primary.read_bytes()
        self.assertIn(b'A%20%26%20B.aio-page-regions.html',modified)
        self.assertEqual(re.sub(br'<div xmlns="http://www.w3.org/1999/xhtml" id="aio-wordml-page-regions-link".*?</div>',b'',modified),self.before)
        self.assertEqual(len(paths),3)
        metadata=[json.loads(line) for line in (self.output/check['manifest']).read_text().splitlines()]
        self.assertEqual(sum(line.get('recordType')=='region' for line in metadata),2)

    def test_links_are_preserved_without_fetching_or_activating_local_scripts(self):
        body='<w:hdr><w:p><w:hlink w:dest="https://example.com/path?a=1&amp;b=2"><w:r><w:t>Web link</w:t></w:r></w:hlink>'
        body+='<w:hlink w:dest="javascript:alert(1)"><w:r><w:t>Unsafe target kept as text</w:t></w:r></w:hlink></w:p></w:hdr>'
        _,check=self.convert(document(body))
        companion=(self.output/check['companion']).read_text()
        self.assertIn('href="https://example.com/path?a=1&amp;b=2"',companion)
        parsed=ET.parse(self.output/check['companion']).getroot()
        self.assertFalse(any(e.get('href','').startswith('javascript:') for e in parsed.iter('{'+H+'}a')))
        self.assertIn('data-original-href="javascript:alert(1)"',companion)
        self.assertEqual(check['linkCount'],2)

    def test_embedded_image_declared_after_header_is_copied_byte_exact(self):
        image=image_png()
        header='<w:hdr><w:p><w:r><w:t>Picture header</w:t></w:r><w:pict><v:imagedata src="wordml://image"/></w:pict></w:p></w:hdr>'
        binary='<w:binData w:name="wordml://image">'+base64.b64encode(image).decode()+'</w:binData>'
        paths,check=self.convert(document(header,binary))
        images=[p for p in paths if p.suffix=='.png']
        self.assertEqual(len(images),1);self.assertEqual(images[0].read_bytes(),image)
        self.assertEqual(check['imageCount'],1)
        companion=ET.parse(self.output/check['companion']).getroot()
        self.assertEqual(len(list(companion.iter('{'+H+'}img'))),1)

    def test_corrupt_embedded_image_is_not_a_successful_preview(self):
        image=bytearray(image_png());image[-6]^=1
        header='<w:hdr><w:pict><v:imagedata src="wordml://bad"/></w:pict></w:hdr>'
        binary='<w:binData w:name="wordml://bad">'+base64.b64encode(image).decode()+'</w:binData>'
        self.source.write_text(document(header,binary))
        with self.assertRaises(GuardError):
            restore_wordml_page_regions(self.source,[self.primary],'htm',cancel=self.cancel)
        self.assertEqual(self.primary.read_bytes(),self.before)

    def test_unknown_vector_bytes_are_retained_as_download_not_active_markup(self):
        image=b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        header='<w:hdr><w:pict><v:imagedata src="wordml://vector"/></w:pict></w:hdr>'
        binary='<w:binData w:name="wordml://vector">'+base64.b64encode(image).decode()+'</w:binData>'
        paths,check=self.convert(document(header,binary))
        opaque=next(p for p in paths if p.suffix=='.bin')
        self.assertEqual(opaque.read_bytes(),image)
        self.assertEqual(check['imagePreviewUnavailable'],1)
        self.assertIn('download=',(self.output/check['companion']).read_text())

    def test_no_regions_creates_no_companions_or_body_change(self):
        paths,check=self.convert(document())
        self.assertIsNone(check);self.assertEqual(paths,[self.primary]);self.assertEqual(self.primary.read_bytes(),self.before)
        self.assertEqual(len(list(self.output.iterdir())),1)

    def test_entities_remote_images_and_cancellation_fail_without_changing_native_body(self):
        for text in ['<!DOCTYPE w:wordDocument [<!ENTITY x "bad">]>'+document(region('hdr','text')),
                     document('<w:hdr><w:pict><v:imagedata src="https://example.com/image.png"/></w:pict></w:hdr>')]:
            self.source.write_text(text)
            with self.assertRaises(GuardError):
                restore_wordml_page_regions(self.source,[self.primary],'htm',cancel=self.cancel)
            self.assertEqual(self.primary.read_bytes(),self.before)
            self.assertEqual(len(list(self.output.iterdir())),1)
        self.cancel.set();self.source.write_text(document(region('hdr','text')))
        with self.assertRaises(GuardError) as error:
            restore_wordml_page_regions(self.source,[self.primary],'htm',cancel=self.cancel)
        self.assertEqual(error.exception.code,'CANCELLED')

    def test_atomic_replace_failure_removes_companions_and_keeps_native_output(self):
        self.source.write_text(document(region('hdr','text')))
        with patch('app_backend.converter_guard.wordml_page_regions.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                restore_wordml_page_regions(self.source,[self.primary],'htm',cancel=self.cancel)
        self.assertEqual(self.primary.read_bytes(),self.before)
        self.assertEqual(list(self.output.iterdir()),[self.primary])

    def test_unrendered_vector_only_region_is_not_silently_reported_preserved(self):
        self.source.write_text(document('<w:hdr><w:pict><v:rect style="width:100pt;height:50pt"/></w:pict></w:hdr>'))
        with self.assertRaises(GuardError) as error:
            restore_wordml_page_regions(self.source,[self.primary],'htm',cancel=self.cancel)
        self.assertEqual(error.exception.code,'WORDML_PAGE_REGIONS_INVALID')
        self.assertEqual(self.primary.read_bytes(),self.before)


if __name__=='__main__':
    unittest.main()
