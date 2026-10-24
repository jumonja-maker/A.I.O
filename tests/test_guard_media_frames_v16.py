"""Real Qt frame conversion with optional independent Pillow pixel readback."""
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QImageReader
    from PySide6.QtWidgets import QApplication
    QT = True
except ImportError:
    QT = False
try:
    from PIL import Image
except ImportError:
    Image = None

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import check_media_preservation, inspect_media
from app_backend.converter_guard.media_frames import convert_media_frames, iter_media_frames
from test_guard_media_fidelity_v16 import png, gif, chunk, WEBP_ANIMATED


@unittest.skipUnless(QT, 'Existing application Qt dependency unavailable')
class QtFrameConversion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        cls.application = QCoreApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()

    def source(self, ext, data):
        p = self.root/('source.'+ext)
        p.write_bytes(data)
        return p

    def convert(self, source, target):
        [output] = convert_media_frames(source,self.root/target,target,cancel=self.cancel)
        proof = check_media_preservation([output],target,source,cancel=self.cancel)
        return output,proof

    def test_real_animation_routes_keep_frames_timing_loop_canvas_alpha(self):
        for ext,data in (('apng',png(width=3,height=5,frames=8)),('gif',gif(frames=8)),('webp',WEBP_ANIMATED)):
            source = self.source(ext,data)
            old = inspect_media(source)
            for target in ('png','apng','gif','webp','tiff'):
                with self.subTest(source=ext,target=target):
                    # A separate root permits the same source/target route in
                    # every iteration without overwriting an earlier output.
                    [output] = convert_media_frames(source,self.root/(ext+'-'+target),target,cancel=self.cancel)
                    proof = check_media_preservation([output],target,source,cancel=self.cancel)
                    new = proof['result']
                    self.assertEqual(max(new['frames'],new['pages']),old['frames'])
                    self.assertEqual((new['width'],new['height']),(old['width'],old['height']))
                    self.assertTrue(new['alpha'])
                    if target != 'tiff':
                        self.assertEqual(new['durationsMs'],old['durationsMs'])
                        self.assertEqual(new.get('loopCount',1),old.get('loopCount',1))
                    self.assertFalse(proof['qualityCompared'])

    @unittest.skipUnless(Image is not None,'Optional independent Pillow decoder unavailable')
    def test_lossless_cross_routes_have_equal_independent_decoded_pixels_all_frames(self):
        for ext,data in (('apng',png(width=3,height=5,frames=8)),('gif',gif(frames=8)),('webp',WEBP_ANIMATED)):
            source = self.source(ext,data)
            with Image.open(source) as original:
                expected = []
                for i in range(original.n_frames):
                    original.seek(i)
                    expected.append(original.convert('RGBA').tobytes())
            for target in ('apng','png','webp','tiff'):
                with self.subTest(source=ext,target=target):
                    [output] = convert_media_frames(source,self.root/(ext+'-'+target),target,cancel=self.cancel)
                    with Image.open(output) as actual:
                        self.assertEqual(actual.n_frames,len(expected))
                        for i, pixels in enumerate(expected):
                            actual.seek(i)
                            self.assertEqual(actual.convert('RGBA').tobytes(),pixels)

    @unittest.skipUnless(Image is not None,'Optional independent Pillow decoder unavailable')
    def test_apng_subframe_disposal_and_blending_are_composited_before_conversion(self):
        frames = [Image.new('RGBA',(5,4),(220,20,30,100)),
                  Image.new('RGBA',(5,4),(0,200,0,130)),
                  Image.new('RGBA',(5,4),(0,0,220,80))]
        buffer = io.BytesIO()
        frames[0].save(buffer,format='PNG',save_all=True,append_images=frames[1:],
                       duration=[120,220,320],loop=3,disposal=[0,2,1],blend=[0,1,1])
        source = self.source('apng',buffer.getvalue())
        # Use an independent Porter-Duff source-over reference. Pillow's APNG
        # plugin itself pastes semi-transparent OVER frames with a mask, which
        # computes a different alpha; its alpha_composite implements the PNG
        # specification's actual source-over operation.
        expected = [frames[0], Image.alpha_composite(frames[0],frames[1]),
                    Image.alpha_composite(frames[0],frames[2])]
        output,_proof = self.convert(source,'webp')
        with Image.open(output) as actual:
            self.assertEqual(actual.n_frames,len(expected))
            for i,frame in enumerate(expected):
                actual.seek(i)
                # Both decoders round alpha compositing to 8 bits; allow one
                # unit for independently implemented integer rounding.
                self.assertLessEqual(max(abs(a-b) for a,b in zip(frame.tobytes(),actual.convert('RGBA').tobytes())),1)

    @unittest.skipUnless(Image is not None,'Optional independent Pillow decoder unavailable')
    def test_png_to_tiff_preserves_rgba_canvas_and_pixels(self):
        source = self.source('png',png(width=3,height=5,alpha=True))
        output,proof = self.convert(source,'tiff')
        self.assertEqual(proof['result']['pages'],1)
        with Image.open(source) as old,Image.open(output) as new:
            self.assertEqual(old.convert('RGBA').tobytes(),new.convert('RGBA').tobytes())

    @unittest.skipUnless(Image is not None,'Optional independent Pillow decoder unavailable')
    def test_pdf_to_tiff_retains_every_page_and_300dpi(self):
        from pypdf import PdfWriter
        source = self.root/'source.pdf'
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=72,height=144)
        with source.open('wb') as stream:
            writer.write(stream)
        output,proof = self.convert(source,'tiff')
        self.assertEqual(proof['result']['pages'],3)
        with Image.open(output) as actual:
            self.assertEqual(actual.n_frames,3)
            for i in range(3):
                actual.seek(i)
                self.assertEqual(actual.size,(300,600))
                self.assertAlmostEqual(actual.info['dpi'][0],300,delta=.05)

    def test_cli_pdf_preflight_without_gui_application_defers_safely(self):
        from pypdf import PdfWriter
        source = self.root/'source.pdf'
        writer = PdfWriter()
        writer.add_blank_page(width=72,height=96)
        with source.open('wb') as stream:
            writer.write(stream)
        script = ('import sys; from app_backend.converter_guard.qt_media import media_preflight; '
                  'assert media_preflight(sys.argv[1]) is None; print("native_fallback")')
        completed = subprocess.run([sys.executable,'-c',script,str(source)],
            capture_output=True,text=True,check=True,timeout=20)
        self.assertEqual(completed.stdout.strip(),'native_fallback')

    def test_tiff_standard_reader_preserves_all_pages_even_with_a_broken_qt_plugin(self):
        # A plugin accepting the first page must not become a prerequisite for
        # the standard RGB/RGBA strip layouts. Run this on Windows as well.
        class BrokenImageReader:
            allocationLimit = staticmethod(QImageReader.allocationLimit)
            supportedImageFormats = staticmethod(QImageReader.supportedImageFormats)
            def __init__(self, *args, **kwargs):
                raise AssertionError('Supported TIFF must not use the optional Qt decoder')
        from test_guard_media_fidelity_v16 import tiff
        from app_backend.converter_guard.qt_media import media_preflight
        source = self.source('tiff',tiff(pages=3))
        with patch('PySide6.QtGui.QImageReader', BrokenImageReader):
            admission = media_preflight(source, cancel=self.cancel)
            self.assertTrue(admission['decoderAvailable'])
            self.assertEqual(admission['decoder'], 'bounded_tiff_strips')
            self.assertIn('apng', admission['targets'])
            output,proof = self.convert(source,'apng')
        self.assertEqual(proof['result']['frames'],3)
        self.assertTrue(proof['result']['alpha'])
        self.assertIn('page_semantics_as_frames',proof['expectedLosses'])
        self.assertFalse(list(self.root.rglob('*.aio-part-*')))

    def test_cancelled_conversion_leaves_no_partial_output(self):
        source = self.source('apng',png(frames=8))
        self.cancel.set()
        with self.assertRaises(GuardError) as error:
            self.convert(source,'webp')
        self.assertEqual(error.exception.code,'CANCELLED')
        self.assertFalse(list(self.root.rglob('*.aio-part.*')))

    def test_nonraster_source_defers_to_existing_office_route(self):
        source = self.source('odt',b'office input')
        self.assertIsNone(convert_media_frames(source,self.root/'out','png',cancel=self.cancel))

    def test_adapter_uses_actual_qt_frames_without_claiming_a_libreoffice_start(self):
        from app_backend.converter_guard.adapter import LibreOfficeAdapter
        source = self.source('gif',gif(frames=8))
        adapter = LibreOfficeAdapter(self.root/'unused-soffice')
        adapter._local.proof = {'state':'detected','basis':'qt_media_content',
            'registryFingerprint':'native-registry','qtMediaTargets':['webp'],
            'mediaProfile':inspect_media(source)}
        started = []
        with (patch.object(adapter,'_registry',return_value=SimpleNamespace(fingerprint='native-registry')),
              patch.object(adapter,'_convert_native',side_effect=AssertionError('unexpected native export'))):
            [output] = adapter.convert(source,'webp',self.root/'adapter',cancel=self.cancel,
                timeout=5,on_started=lambda:started.append(True))
        self.assertEqual(inspect_media(output)['frames'],8)
        self.assertEqual(adapter.execution_mode(),'qt_media_frames')
        self.assertEqual(started,[])
        self.assertEqual(adapter.export_info()['resultChecks'][0]['check'],'media_properties')

    def test_adapter_timeout_covers_qt_metadata_and_conversion(self):
        from app_backend.converter_guard.adapter import LibreOfficeAdapter
        source = self.source('gif',gif(frames=8))
        adapter = LibreOfficeAdapter(self.root/'unused-soffice')
        adapter._local.proof = {'state':'detected','basis':'qt_media_content',
            'registryFingerprint':'native-registry','qtMediaTargets':['webp'],
            'mediaProfile':inspect_media(source)}
        with patch.object(adapter,'_registry',return_value=SimpleNamespace(fingerprint='native-registry')):
            with self.assertRaises(GuardError) as error:
                adapter.convert(source,'webp',self.root/'adapter',cancel=self.cancel,
                    timeout=1e-12,on_started=lambda:None)
        self.assertEqual(error.exception.code,'ENGINE_TIMEOUT')
        self.assertFalse(list(self.root.rglob('*.aio-part-*')))

    def test_16bit_lossless_target_cannot_be_downsampled_silently(self):
        data = b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,16,6,0,0,0))
        import zlib
        data += chunk(b'IDAT',zlib.compress(b'\0'+b'\x12\x34'*4))+chunk(b'IEND',b'')
        source = self.source('png',data)
        with self.assertRaises(GuardError) as error:
            self.convert(source,'tiff')
        self.assertEqual(error.exception.code,'MEDIA_HIGH_BIT_DEPTH_UNSUPPORTED')


if __name__ == '__main__':
    unittest.main()
