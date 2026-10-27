from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest

from app_backend.converter_guard.legacy_import import legacy_import_hint, validate_options
from app_backend.converter_guard.export_recipe import native_export_recipe
from app_backend.converter_guard.routes import Registry
from app_backend.libreoffice_engine import validated_outputs, LibreOfficeError
from test_converter_guard_v04 import component, node, C, W


def cp1251_dbf(level=1, *, driver=0xC9, utf8=False):
    count=level+2;header=bytearray(129);header[0]=3;header[1:4]=bytes((126,10,27))
    struct.pack_into('<IHH',header,4,count,129,80);header[29]=driver
    for i,(name,kind,width,decimals) in enumerate((('ID','C',3,0),('TEXT','C',64,0),('AMOUNT','N',12,2))):
        at=32+i*32;header[at:at+len(name)]=name.encode();header[at+11]=ord(kind)
        header[at+16]=width;header[at+17]=decimals
    header[128]=13
    records=[]
    for index in range(1,count+1):
        text=f'Строка {index} уровень {level}'.encode('utf-8' if utf8 else 'cp1251')
        records.append(b' '+f'{index:03d}'.encode()+text.ljust(64)+f'{index*1.25:12.2f}'.encode())
    return bytes(header)+b''.join(records)+b'\x1a'


class NativeRouteContracts(unittest.TestCase):
    def test_c9_hint_requires_content_layout_and_declared_cyrillic(self):
        native=SimpleNamespace(filters={'dBase':(C,{'dbf'},{'IMPORT'})})
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'renamed.bin'
            for level in range(1,9):
                original=cp1251_dbf(level);p.write_bytes(original)
                self.assertEqual(legacy_import_hint(p,native),
                    {'filter':'dBase','options':'34','basis':'content_confirmed_legacy'})
                self.assertEqual(p.read_bytes(),original)
            for damaged in (cp1251_dbf(driver=0),cp1251_dbf(utf8=True),cp1251_dbf()[:-25]):
                p.write_bytes(damaged);self.assertIsNone(legacy_import_hint(p,native))
            for at in (8,10,48):
                damaged=bytearray(cp1251_dbf());damaged[at]=0
                p.write_bytes(damaged);self.assertIsNone(legacy_import_hint(p,native))
            self.assertTrue(validate_options('dBase','34',native))
            self.assertFalse(validate_options('dBase','1251',native))
            self.assertFalse(validate_options('dBase','34,unsafe',native))

    def test_writer_web_alias_uses_installed_writer_jpeg_only_with_interface_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);exe=root/'soffice';exe.touch();reg=root/'registry.xcd'
            web='com.sun.star.text.WebDocument'
            types=node('jpeg',Extensions='jpg jpeg jfif jif jpe')+node('html',Extensions='html htm')
            filters=(node('writer_jpg_Export',Type='jpeg',DocumentService=W,Flags='EXPORT')+
                node('writer_web_jpg_Export',Type='jpeg',DocumentService=web,Flags='EXPORT')+
                node('HTML',Type='html',DocumentService=web,Flags='IMPORT EXPORT'))
            reg.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">'+
                component('Types','Types',types)+component('Filter','Filters',filters)+'</oor:data>')
            native=Registry(((str(reg),0,reg.stat().st_size),),exe)
            proof={'state':'detected','basis':'native_preflight_uno_document','modelLoaded':True,
                   'registryFingerprint':native.fingerprint,'documentServices':[web],'nativeTextDocument':True}
            for target in ('jpg','jpeg','jfif','jif','jpe'):
                self.assertEqual(native_export_recipe(native,target,proof).filter_name,'writer_jpg_Export')
            proof['nativeTextDocument']=False
            self.assertEqual(native_export_recipe(native,'jfif',proof).filter_name,'writer_web_jpg_Export')

    def test_alias_never_accepts_an_html_shell_or_wrong_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'input.html';source.write_text('<p>source</p>')
            output=root/'output';output.mkdir()
            for target in ('jfif','jif','jpe'):
                p=output/('result.'+target);p.write_bytes(b'<html>not JPEG</html>')
                with self.assertRaises(LibreOfficeError):validated_outputs(source,target,output)
                p.unlink()


if __name__=='__main__':unittest.main()
