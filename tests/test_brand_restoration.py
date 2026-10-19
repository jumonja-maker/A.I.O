"""Restore exact artwork, valid ICO frames and real packaging/HTTP references."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from app_backend.server import start_server

ROOT=Path(__file__).resolve().parents[1]
BRAND=ROOT/'assets/brand'
EXPECTED={
 'aio-new-brand-source.png':'34b6491b56bae5f5eb7c014e0bcd7ede3151c0daff11d6732904ed225a5e0f37',
 'elva-v13-happy-source.png':'87e2b8dc069bee52ec796ea03d31e618789b64ffeb9c8a66ea50f09f49db42c5',
}


def png_size(data):
    if data[:8]!=b'\x89PNG\r\n\x1a\n':raise AssertionError('not PNG')
    return struct.unpack('>II',data[16:24])


class BrandRestorationTests(unittest.TestCase):
    def test_both_original_pngs_match_owner_digests_and_have_alpha(self):
        for name,digest in EXPECTED.items():
            with self.subTest(name=name):
                data=(BRAND/name).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(),digest)
                self.assertEqual(png_size(data),(1254,1254))
                self.assertEqual(data[25],6)  # PNG truecolour + alpha

    def test_every_manifest_resource_matches_the_committed_bytes(self):
        provenance=json.loads((BRAND/'BRAND_PROVENANCE.json').read_text())
        self.assertFalse(provenance['artworkModified'])
        self.assertFalse(provenance['thumbnailUpscaled'])
        for name,metadata in provenance['derivatives'].items():
            with self.subTest(name=name):
                data=(BRAND/name).read_bytes()
                self.assertEqual(len(data),metadata['bytes'])
                self.assertEqual(hashlib.sha256(data).hexdigest(),metadata['sha256'])

    def test_windows_ico_contains_real_32bit_png_frames_at_all_scales(self):
        data=(BRAND/'icons/aio.ico').read_bytes()
        reserved,kind,count=struct.unpack_from('<HHH',data)
        self.assertEqual((reserved,kind,count),(0,1,8))
        sizes=[]
        for i in range(count):
            width,height,_,_,planes,bits,length,offset=struct.unpack_from('<BBBBHHII',data,6+16*i)
            size=width or 256
            self.assertEqual(height or 256,size)
            self.assertEqual((planes,bits),(1,32))
            self.assertLessEqual(offset+length,len(data))
            self.assertEqual(png_size(data[offset:offset+length]),(size,size))
            sizes.append(size)
        self.assertEqual(sizes,[16,20,24,32,48,64,128,256])

    def test_header_density_candidates_have_sufficient_pixels(self):
        html=(ROOT/'index.html').read_text()
        for size,density in ((64,1),(128,2),(256,4),(512,8)):
            self.assertIn(f'elva-v13-happy-{size}.png {size}w',html)
            self.assertEqual(png_size((BRAND/f'elva-v13-happy-{size}.png').read_bytes()),(size,size))
            self.assertGreaterEqual(size,58*density)
        self.assertNotIn('elva-v13-happy-thumb.webp',html)
        self.assertNotIn('elva-v13-happy-thumb.webp',(ROOT/'src/branding.js').read_text())

    def test_spec_bundles_icon_and_approved_sources_and_sets_exe_resource(self):
        calls={}
        def analysis(*args,**kwargs):
            calls['datas']=kwargs['datas']
            return SimpleNamespace(pure=[],scripts=[],binaries=[],datas=kwargs['datas'])
        def exe(*args,**kwargs):
            calls['icon']=kwargs['icon'];return object()
        namespace={'SPECPATH':str(ROOT),'Analysis':analysis,'PYZ':lambda *a,**k:object(),
                   'EXE':exe,'COLLECT':lambda *a,**k:None,'Tree':lambda *a,**k:[]}
        # Controlled placeholder payload is never executed; this tests asset
        # wiring, not a Windows build or the real LibreOffice distribution.
        with tempfile.TemporaryDirectory() as directory:
            payload=Path(directory)
            (payload/'program').mkdir();(payload/'share').mkdir()
            for name in ('soffice.exe','soffice.bin'):(payload/'program'/name).touch()
            (payload/'LICENSE').write_text('fixture')
            with patch.dict(os.environ,{'AIO_LIBREOFFICE_ROOT':str(payload)}):
                exec(compile((ROOT/'AllInOne.spec').read_text(),'AllInOne.spec','exec'),namespace)
        self.assertEqual(Path(calls['icon']),BRAND/'icons/aio.ico')
        bundled={Path(source).relative_to(ROOT).as_posix() for source,_ in calls['datas']}
        self.assertTrue({'assets/brand/icons/aio.ico','assets/brand/elva-v13-happy-source.png',
                         'assets/brand/aio-new-brand-source.png','assets/brand/elva-v13-happy-128.png'}<=bundled)

    def test_both_installers_reference_the_packaged_icon(self):
        for name in ('AIO-Clean-QA.iss','AIO-LibreOffice-Direct-QA.iss'):
            text=(ROOT/'installer/windows'/name).read_text()
            self.assertIn('SetupIconFile=..\\..\\assets\\brand\\icons\\aio.ico',text)
            self.assertIn('UninstallDisplayIcon={app}\\_internal\\assets\\brand\\icons\\aio.ico',text)
            self.assertEqual(text.count('IconFilename: "{app}\\_internal\\assets\\brand\\icons\\aio.ico"'),2)

    def test_restored_images_and_icon_are_served_as_their_actual_bytes(self):
        server,_=start_server()
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        try:
            base='http://127.0.0.1:'+str(server.server_port)
            for name in ('elva-v13-happy-128.png','icons/aio.ico'):
                with urlopen(base+'/assets/brand/'+name,timeout=5) as response:
                    data=response.read()
                    self.assertEqual(data,(BRAND/name).read_bytes())
                    self.assertTrue(response.headers['Content-Type'].startswith('image/'))
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':
    unittest.main()
