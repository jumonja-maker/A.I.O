"""Independent hand-authored PICT pixels and guarded unsupported variants."""
from pathlib import Path
import struct
import tempfile
import threading
import unittest

from PIL import Image

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.media_fidelity import check_media_preservation
from app_backend.converter_guard.pict_bitmap import inspect_pict_bitmap
from app_backend.converter_guard.qt_media import convert_media_frames, media_preflight


PIXELS = bytes((1,2,3,4,5,6,250,128,9,16,32,64))


def bitmap():
    frame = struct.pack('>4h',0,0,2,2)
    resolution = struct.pack('>II',72*65536,100*65536)
    header = bytes(512)+bytes(2)+frame+b'\0\x11\x02\xff\x0c\0\xff\xfe\0\0'+resolution+frame+bytes(4)
    clip = b'\0\1\0\x0a'+frame
    pixmap = struct.pack('>H',0x8008)+frame+struct.pack('>HHI',0,4,0)+resolution+struct.pack('>4H',16,32,3,8)+bytes(12)
    rows = b'\x08\x80\x05\x01\x04\x02\x05\x03\x06'+b'\x07\x05\xfa\x10\x80\x20\x09\x40'
    raw = header+clip+b'\0\x9a\0\0\0\xff'+pixmap+frame+frame+struct.pack('>H',64)+rows
    return raw+bytes(len(raw)%2)+b'\0\xff'


class PictBitmap(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()

    def write(self, raw=None, name='source.pct'):
        source = self.root/name
        source.write_bytes(bitmap() if raw is None else raw)
        return source

    def test_handwritten_planar_pixels_nop_and_physical_resolution_survive(self):
        source = self.write()
        original = source.read_bytes()
        profile, pixels = inspect_pict_bitmap(source, decode=True)
        self.assertEqual(pixels, PIXELS)
        self.assertEqual(profile['dpi'],[72,100])
        self.assertEqual(media_preflight(source)['decoder'],'quickdraw_rgb_packbits')
        for target in ('png','apng','tiff','bmp'):
            with self.subTest(target=target):
                outputs = convert_media_frames(source,self.root/target,target,cancel=self.cancel)
                check = check_media_preservation(outputs,target,source,cancel=self.cancel)
                self.assertEqual(check['expectedLosses'],[])
                with Image.open(outputs[0]) as actual:
                    self.assertEqual(actual.size,(2,2))
                    self.assertEqual(actual.convert('RGB').tobytes(),PIXELS)
                    self.assertAlmostEqual(actual.info['dpi'][0],72,delta=.05)
                    self.assertAlmostEqual(actual.info['dpi'][1],100,delta=.05)
        self.assertEqual(source.read_bytes(),original)

    def test_packbits_repeat_has_exact_planar_semantics(self):
        raw = bitmap()
        row_start = 552+12+2+4+46+18
        replacement = b'\x02\xfb\x07'  # Six repeated planar bytes: two RGB7 pixels.
        raw = raw[:row_start]+replacement+raw[row_start+9:]
        source = self.write(raw)
        self.assertEqual(inspect_pict_bitmap(source,decode=True)[1],bytes((7,)*6)+PIXELS[6:])

    def test_renamed_content_gets_the_same_exact_rgb_and_runtime_contract(self):
        normal, renamed = self.write(), self.write(name='renamed.dat')
        original = renamed.read_bytes()
        self.assertEqual(inspect_pict_bitmap(normal,decode=True),inspect_pict_bitmap(renamed,decode=True))
        self.assertEqual(media_preflight(renamed)['decoder'],'quickdraw_rgb_packbits')
        outputs = convert_media_frames(renamed,self.root/'renamed-output','png',cancel=self.cancel)
        check = check_media_preservation(outputs,'png',renamed,cancel=self.cancel)
        self.assertTrue(check['exactSourceRgbPixelsVerified'])
        self.assertEqual(renamed.read_bytes(),original)

    def test_unknown_drawing_comment_clip_transform_or_dpi_stays_native(self):
        original = bitmap()
        variants = []
        changed = bytearray(original); changed[564:566] = b'\0\xa1'; variants.append(changed)
        changed = bytearray(original); struct.pack_into('>h',changed,558,1); variants.append(changed)
        changed = bytearray(original); struct.pack_into('>H',changed,632,1); variants.append(changed)
        changed = bytearray(original); struct.pack_into('>I',changed,588,96*65536); variants.append(changed)
        variants.append(original+b'\0\x20')
        for index,raw in enumerate(variants):
            with self.subTest(index=index):
                self.assertIsNone(inspect_pict_bitmap(self.write(raw,'%d.pct'%index)))

    def test_truncated_rows_overflow_and_incomplete_planes_fail(self):
        raw = bitmap(); row_start = 552+12+2+4+46+18
        variants = [raw[:-1],raw[:row_start]+b'\x02\xf9\x07'+raw[row_start+9:],
                    raw[:row_start]+b'\x02\x00\x07'+raw[row_start+9:]]
        for index,value in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(GuardError) as caught:
                inspect_pict_bitmap(self.write(value,'%d.pct'%index))
            self.assertEqual(caught.exception.code,'MEDIA_SOURCE_INVALID')

    def test_large_canvas_and_cancel_fail_before_pixel_allocation(self):
        raw = bytearray(bitmap())
        struct.pack_into('>4h',raw,514,0,0,5000,5000)
        with self.assertRaises(GuardError) as caught:
            inspect_pict_bitmap(self.write(raw))
        self.assertEqual(caught.exception.code,'MEDIA_CANVAS_BUDGET')
        self.cancel.set()
        with self.assertRaises(GuardError) as caught:
            inspect_pict_bitmap(self.root/'missing.pct',cancel=self.cancel)
        self.assertEqual(caught.exception.code,'CANCELLED')

    def test_same_canvas_corrupt_rgb_or_alpha_is_rejected_before_publication(self):
        source = self.write()
        for name,mode,pixels in [('wrong','RGB',bytes((0,)*12)),
                                 ('alpha','RGBA',bytes((1,2,3,0))*4)]:
            output = self.root/name/'source.png'; output.parent.mkdir()
            Image.frombytes(mode,(2,2),pixels).save(output,dpi=(72,100))
            with self.assertRaises(GuardError) as caught:
                check_media_preservation([output],'png',source,cancel=self.cancel)
            self.assertEqual(caught.exception.code,'MEDIA_PIXEL_LOSS')


if __name__ == '__main__':
    unittest.main()
