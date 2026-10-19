"""Direct LO contract: transport has no conversion policy; the engine decides."""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from app_backend import libreoffice_engine as lo
from app_backend.libreoffice_catalog import catalog, _registered_exports
from app_backend.downloads import download_filter


class LibreOfficeDirectTests(unittest.TestCase):
    def test_any_source_extension_reaches_the_native_adapter(self):
        for name in ('image.bmp', 'image.png', 'scan.pdf', 'macro.docm',
                     'sheet.xlsm', 'slides.pptm', 'unknown.custom', 'without-extension'):
            self.assertIsInstance(lo.source_format(name), str)
        for name in ('../file.docx', 'sub/file.bmp', 'sub\\file.bmp', '', '..', 'a\0.txt'):
            with self.assertRaises(lo.LibreOfficeError):
                lo.source_format(name)

    def test_metadata_only_has_no_size_or_count_cap_and_no_mixed_family_veto(self):
        files = [{'id': str(i), 'name': 'file.' + ('bmp' if i % 2 else 'csv'),
                  'size': 2 ** 40} for i in range(105)]
        with patch.object(lo, 'engine_binary', return_value=Path(__file__)):
            report = lo.inspect_metadata(files)
        self.assertEqual(len(report['files']), 105)
        for target in ('pdf', 'png', 'bmp', 'docx', 'xlsx'):
            row = next(r for r in report['formats'] if r['id'] == target)
            self.assertTrue(row['enabled'])
            self.assertEqual(len(row['compatibleFileIds']), 105)
            self.assertEqual(row['compatibility'], 'native_decision')
        self.assertEqual(report['acceptedExtensions'], '')
        self.assertIsNone(report['policy'])

    def test_missing_engine_has_an_explicit_reason(self):
        with patch.object(lo, 'engine_binary', return_value=None):
            report = lo.inspect_metadata([{'id': 'one', 'name': 'file.bmp', 'size': 0}])
        self.assertTrue(all(not f['enabled'] for f in report['formats']))
        self.assertIn('LibreOffice не найден', report['message'])
        self.assertIn('images', {f['category'] for f in report['formats']})

    def test_registry_export_catalog_is_discovered_and_cached(self):
        content = '''<oor:data xmlns:oor="http://openoffice.org/2001/registry">
<node oor:name="type"><prop oor:name="Extensions"><value>custombmp</value></prop>
<prop oor:name="MediaType"><value>image/custom</value></prop></node>
<node oor:name="native_filter"><prop oor:name="Flags"><value>EXPORT</value></prop>
<prop oor:name="Type"><value>type</value></prop>
<prop oor:name="UIName"><value>Custom native export</value></prop></node>
</oor:data>'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'custom.xcd').write_text(content)
            with patch.dict(os.environ, {'AIO_LIBREOFFICE_REGISTRY': str(root)}):
                first = catalog(Path(__file__))
                with patch('xml.etree.ElementTree.parse', side_effect=AssertionError('reparsed')):
                    second = catalog(Path(__file__))
        row = next(r for r in first if r[0] == 'custombmp')
        self.assertEqual(row[1], 'images')
        self.assertTrue(row[-1])
        self.assertEqual(first, second)

    def test_native_command_preserves_name_and_uses_native_filter_detection(self):
        class FakeTree:
            def __init__(self, command, **kwargs):
                self.command = command
                self.process = type('Process', (), {'poll': lambda self: 0, 'returncode': 0})()
                output = Path(command[command.index('--outdir') + 1])
                source = Path(command[-1])
                (output / (source.stem + '.custom')).write_bytes(b'native-output')
                kwargs['on_started']()
                commands.append(command)
            def __enter__(self): return self
            def __exit__(self, *_): pass
        commands, starts = [], []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Исходное_имя.документ'
            source.write_bytes(b'not a parsed office zip')
            with patch.object(lo, 'ProcessTree', FakeTree):
                outputs = lo.convert_path(source, 'custom', root / 'job',
                    executable=Path(__file__), on_started=lambda: starts.append(True))
            self.assertEqual(outputs[0].read_bytes(), b'native-output')
            self.assertEqual(source.read_bytes(), b'not a parsed office zip')
        command = commands[0]
        self.assertEqual(command[command.index('--convert-to') + 1], 'custom')
        self.assertTrue(command[-1].endswith('Исходное_имя.документ'))
        self.assertEqual(starts, [True])

    def test_missing_native_output_is_an_error(self):
        class FakeTree:
            def __init__(self, command, **kwargs):
                self.process = type('Process', (), {'poll': lambda self: 0, 'returncode': 0})()
            def __enter__(self): return self
            def __exit__(self, *_): pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'broken.bmp'
            source.write_bytes(b'broken')
            with patch.object(lo, 'ProcessTree', FakeTree):
                with self.assertRaises(lo.LibreOfficeError) as error:
                    lo.convert_path(source, 'pdf', root / 'job', executable=Path(__file__))
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_MISSING')

    def test_native_download_accepts_any_output_extension_from_exact_local_ticket(self):
        ticket = 'a' * 43
        for extension in ('bmp', 'svg', 'custom', 'pdf', 'xlsx'):
            self.assertIsNotNone(download_filter(
                f'http://127.0.0.1:8123/api/converter/result?ticket={ticket}',
                'result.' + extension, 8123))
        for url in ('http://127.0.0.1:9999/api/converter/result?ticket=' + ticket,
                    'http://127.0.0.1:8123/src/file.js',
                    'https://example.com/result?ticket=' + ticket,
                    'blob:http://127.0.0.1:8123/unknown'):
            self.assertIsNone(download_filter(url, 'result.bmp', 8123))
        self.assertIsNotNone(download_filter('blob:http://127.0.0.1:8123/excel', 'result.xlsx', 8123))

    def test_live_writer_calc_pdf_with_local_engine(self):
        binary = lo.engine_binary()
        if binary is None:
            if os.environ.get('AIO_LO_QA_MUST_HAVE_ENGINE') == '1':
                self.fail('QA requires an installed LibreOffice engine')
            self.skipTest('LibreOffice not installed')
        width, height = 24, 16
        data = b'\x00\x7f\xff' * width * height
        bmp = (b'BM' + struct.pack('<IHHI', 54 + len(data), 0, 0, 54) +
               struct.pack('<IiiHHIIiiII', 40, width, height, 1, 24, 0,
                           len(data), 3780, 3780, 0, 0) + data)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'actual.bmp'
            image.write_bytes(bmp)
            outputs = lo.convert_path(image, 'pdf', root / 'bmp-pdf', executable=binary)
            self.assertTrue(outputs[0].read_bytes().startswith(b'%PDF-'))
            text = root / 'actual.unlisted'
            text.write_text('Native import decides, not an AIO extension whitelist.')
            outputs = lo.convert_path(text, 'pdf', root / 'unknown-pdf', executable=binary)
            self.assertTrue(outputs[0].read_bytes().startswith(b'%PDF-'))
