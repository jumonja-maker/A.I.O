"""Targeted QA reads sheet data rather than counting companion artifacts."""
import importlib.util
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

FILE = Path(__file__).resolve().parents[1] / 'tools/qa/guard_v17_native_acceptance.py'
SPEC = importlib.util.spec_from_file_location('guard_v17_native_reader', FILE)
qa = importlib.util.module_from_spec(SPEC)
with patch.object(sys, 'path', [str(FILE.parent), *sys.path]):
    SPEC.loader.exec_module(qa)


class NativeTableReader(unittest.TestCase):
    def test_sidecars_do_not_become_sheets_and_reimport_has_explicit_charset(self):
        for target, companion, native_import in (
                ('dif', 'book.aio-encoding.json', 'DIF:76'),
                ('dbf', 'book.dbt', 'dBase:76')):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'book.ods'
                primaries = [root / ('book.' + target), root / ('book__sheet_002.' + target)]
                sidecar = root / companion
                for path in primaries + [sidecar]: path.write_bytes(b'fixture')
                calls = []
                def reopen(path, fmt, work, **kwargs):
                    calls.append((path.name, kwargs['import_filter']))
                    self.assertEqual(fmt, 'fods')
                    work.mkdir(exist_ok=True)
                    flat = work / 'sheet.fods'
                    document = ET.Element('{%s}document' % qa.OFFICE)
                    table = ET.SubElement(document, '{%s}table' % qa.TABLE)
                    row = ET.SubElement(table, '{%s}table-row' % qa.TABLE)
                    text = ET.SubElement(row, '{%s}table-cell' % qa.TABLE,
                        {'{%s}value-type' % qa.OFFICE: 'string'})
                    ET.SubElement(text, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p').text = (
                        'Привет' if len(calls) == 1 else 'Второй')
                    number = ET.SubElement(row, '{%s}table-cell' % qa.TABLE,
                        {'{%s}value-type' % qa.OFFICE: 'float', '{%s}value' % qa.OFFICE: str(len(calls))})
                    ET.SubElement(number, '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p').text = str(len(calls))
                    ET.ElementTree(document).write(flat, encoding='utf-8')
                    return [flat]
                with patch.object(qa, 'table_markers', return_value=[['Привет'], ['Второй']]), \
                        patch.object(qa, 'numeric_pairs', return_value=[[('Привет', Decimal('1'))], [('Второй', Decimal('2'))]]), \
                        patch.object(qa, 'mixed_numeric_control_column', return_value=False), \
                        patch.object(qa, 'prepare_profile'), \
                        patch.object(qa, 'convert_path', side_effect=reopen):
                    result = qa.check_case({'kind': 'table', 'source': source}, target,
                        [sidecar, primaries[1], primaries[0]], root, root / 'soffice', 30)
                self.assertEqual(calls, [(p.name, native_import) for p in primaries])
                self.assertEqual(len(result['sheets']), 2)
                self.assertTrue(all(sheet['numericValues'] == 1 for sheet in result['sheets']))
                self.assertEqual(result['companionFiles'], [sidecar.name])

    def test_a_sidecar_cannot_hide_a_missing_primary_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(qa, 'table_markers', return_value=[['first'], ['second']]), \
                    self.assertRaisesRegex(AssertionError, 'received 1'):
                qa.check_case({'kind': 'table', 'source': root / 'book.ods'}, 'dif',
                    [root / 'book.dif', root / 'book.aio-encoding.json'], root, root / 'soffice', 30)


if __name__ == '__main__':
    unittest.main()
