"""M26: content-confirmed PNM routes must preserve pixels without Draw pages."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app_backend.converter_guard.qt_media import qt_pnm_profile, qt_xbm_xpm_profile, media_preflight


class QtPortableRasterTests(unittest.TestCase):
    def test_three_opaque_pnm_formats(self):
        with TemporaryDirectory() as folder:
            data={
                'pbm': b'P4\n8 3\n'+bytes((0b10101010,0b01010101,0b11110000)),
                'pgm': b'P5\n8 3\n255\n'+bytes(range(24)),
                'ppm': b'P6\n8 3\n255\n'+bytes(range(72)),
            }
            for kind, raw in data.items():
                with self.subTest(kind=kind):
                    path=Path(folder)/('sample.'+kind)
                    path.write_bytes(raw)
                    profile=qt_pnm_profile(path)
                    self.assertIsNotNone(profile)
                    self.assertEqual((profile['width'],profile['height']),(8,3))
                    self.assertEqual(profile['format'],kind)
                    self.assertFalse(profile['alpha'])
                    proof=media_preflight(path)
                    self.assertIn('png',proof['targets'])

    def test_suffix_is_never_sufficient(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'fake.pgm'
            path.write_bytes(b'Untrusted plain text, not a pixel image.')
            self.assertIsNone(qt_pnm_profile(path))

    def test_mismatched_magic_and_extension_is_not_promoted(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'fake.pgm'
            path.write_bytes(b'P6\n2 2\n255\n'+bytes(range(12)))
            self.assertIsNone(qt_pnm_profile(path))


    def test_xbm_xpm_must_have_confirmed_declaration_and_qt_decoder(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            xbm = root / '01_LEVEL_1.xbm'
            xbm.write_bytes(
                b'#define 01_LEVEL_1_width 8\n#define 01_LEVEL_1_height 3\n'
                b'static char 01_LEVEL_1_bits[] = { 0xaa, 0x55, 0xf0 };\n')
            xpm = root / '01_LEVEL_1.xpm'
            xpm.write_bytes(
                b'/* XPM */\nstatic char *_1_LEVEL_1[] = {\n'
                b'"8 3 2 1",\n". c #000000",\n"  c #FFFFFF",\n'
                b'"........",\n"        ",\n"....    "\n};\n')
            for kind, path in (('xbm', xbm), ('xpm', xpm)):
                with self.subTest(kind=kind):
                    profile = qt_xbm_xpm_profile(path)
                    self.assertIsNotNone(profile, 'Qt image codec must read original fixture')
                    self.assertEqual((profile['width'], profile['height']), (8, 3))
                    self.assertEqual(profile['format'], kind)
                    self.assertFalse(profile['alpha'])
                    proof = media_preflight(path)
                    self.assertIn('png', proof['targets'])

    def test_legacy_extension_or_header_alone_is_not_admission(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for kind, data in (
                ('xbm', b'#define bad_width 10\nnot-an-image'),
                ('xpm', b'/* XPM */\n"8 3 999 1"\n'),
            ):
                with self.subTest(kind=kind):
                    source = root / ('fake.' + kind)
                    source.write_bytes(data)
                    self.assertIsNone(qt_xbm_xpm_profile(source))


if __name__=='__main__':
    unittest.main()
