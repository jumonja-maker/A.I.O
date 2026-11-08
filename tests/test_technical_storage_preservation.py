"""Exact spreadsheet values and ownership of native-save staging files."""
import io
from datetime import date, datetime, time, timedelta
from math import copysign
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.excel import MergeError, merge_xlsx
from test_converter_guard_v02 import FakeAdapter, wait_finished


def workbook_bytes(rows=(), change=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['Code', 'Value'])
    for row in rows:
        sheet.append(row)
    if change is not None:
        change(sheet)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def rewrite_part(payload, part, transform):
    output = io.BytesIO()
    with ZipFile(io.BytesIO(payload)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for name in source.namelist():
            content = source.read(name)
            target.writestr(name, transform(content) if name == part else content)
    return output.getvalue()


class ExcelExactValues(unittest.TestCase):
    def test_exact_integers_stay_numeric_and_booleans_and_floats_keep_their_types(self):
        integers = [0, -1, 9007199254740991, 10000000000000002,
                    -10000000000000002, 12345678901234567]
        source = workbook_bytes([(str(index), 42) for index in range(len(integers))] +
                                [('bool', True), ('float', 64.01)])

        def exact_numbers(content):
            # Build exact source numbers without passing them through the same
            # openpyxl serializer whose precision is under test.
            tree = ElementTree.fromstring(content)
            cells = {cell.get('r'): cell for cell in tree.findall('.//{*}c')}
            for index, value in enumerate(integers, 2):
                cells[f'B{index}'].find('{*}v').text = str(value)
            return ElementTree.tostring(tree)

        source = rewrite_part(source, 'xl/worksheets/sheet1.xml', exact_numbers)
        output = merge_xlsx([('exact.xlsx', source), ('other.xlsx', workbook_bytes())])
        workbook = load_workbook(io.BytesIO(output), read_only=True)
        try:
            rows = list(workbook.active.iter_rows())[1:]
            for row, value in zip(rows, integers):
                self.assertEqual(row[1].value, value)
                self.assertIs(type(row[1].value), int)
                self.assertEqual(row[1].data_type, 'n')
            self.assertIs(rows[-2][1].value, True)
            self.assertEqual(rows[-2][1].data_type, 'b')
            self.assertEqual(rows[-1][1].value, 64.01)
            self.assertIs(type(rows[-1][1].value), float)
        finally:
            workbook.close()

    def test_readback_accepts_empty_cells_dates_and_numeric_types_with_trailing_blanks(self):
        values = [None, '', date(2026, 11, 8), datetime(2026, 11, 8, 12, 34, 56, 123000),
                  time(12, 34, 56, 123000), timedelta(days=2, seconds=30),
                  True, False, 1, 0, 64.01, 1.0, -0.0]
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['Kind', 'Value', 'Trailing'])
        for index, value in enumerate(values):
            sheet.append([str(index), value, None])
        source = io.BytesIO()
        workbook.save(source)
        workbook.close()

        def explicit_float_tokens(content):
            tree = ElementTree.fromstring(content)
            cells = {cell.get('r'): cell for cell in tree.findall('.//{*}c')}
            for coordinate, token in (('B13', '1.0'), ('B14', '-0.0')):
                cells[coordinate].find('{*}v').text = token
            return ElementTree.tostring(tree)

        source = rewrite_part(source.getvalue(), 'xl/worksheets/sheet1.xml', explicit_float_tokens)
        output = merge_xlsx([('one.xlsx', source), ('two.xlsx', source)])
        before = load_workbook(io.BytesIO(source), read_only=True)
        after = load_workbook(io.BytesIO(output), read_only=True)
        try:
            expected = list(before.active.values)[1:]
            actual = list(after.active.values)[1:]
            self.assertEqual(actual, expected * 2)
            self.assertEqual([[type(value) for value in row] for row in actual],
                             [[type(value) for value in row] for row in expected] * 2)
            self.assertIs(type(actual[11][1]), float)
            self.assertIs(type(actual[12][1]), float)
            self.assertEqual(copysign(1, actual[12][1]), -1)
            self.assertTrue(all(len(row) == 3 and row[2] is None for row in actual))
        finally:
            before.close()
            after.close()

    def test_output_readback_rejects_changed_value_with_same_header_and_row_count(self):
        from openpyxl.workbook.workbook import Workbook as OpenpyxlWorkbook
        save = OpenpyxlWorkbook.save

        def corrupt_output(workbook, stream):
            save(workbook, stream)
            if workbook.write_only:
                changed = rewrite_part(stream.getvalue(), 'xl/worksheets/sheet1.xml',
                                       lambda xml: xml.replace(b'<v>123</v>', b'<v>124</v>'))
                stream.seek(0)
                stream.truncate()
                stream.write(changed)

        first = workbook_bytes([('001', 123)])
        second = workbook_bytes([('002', 456)])
        with patch.object(OpenpyxlWorkbook, 'save', corrupt_output):
            with self.assertRaisesRegex(RuntimeError, 'проверки созданного файла'):
                merge_xlsx([('one.xlsx', first), ('two.xlsx', second)])

    def test_iso_date_only_cells_keep_their_type_and_explicit_general_format(self):
        workbook = Workbook(iso_dates=True)
        sheet = workbook.active
        sheet.append(['Kind', 'Value'])
        values = [date(2026, 11, 8), date(1899, 12, 29),
                  datetime(2026, 11, 8, 12, 34, 56, 123000), time(12, 34, 56, 123000)]
        for index, value in enumerate(values):
            sheet.append([str(index), value])
            if index == 1:
                sheet.cell(index + 2, 2).number_format = 'General'
        source = io.BytesIO()
        workbook.save(source)
        workbook.close()
        output = merge_xlsx([('one.xlsx', source.getvalue()), ('two.xlsx', source.getvalue())])
        result = load_workbook(io.BytesIO(output), read_only=True)
        try:
            rows = list(result.active.iter_rows())[1:]
            actual = [row[1].value for row in rows]
            self.assertEqual(actual, values * 2)
            self.assertEqual([type(value) for value in actual], [type(value) for value in values] * 2)
            self.assertEqual(rows[1][1].number_format, 'General')
        finally:
            result.close()

    def test_inherited_row_and_column_styles_are_rejected_before_output(self):
        for dimension in ('row', 'column'):
            for style in ('font', 'number_format'):
                with self.subTest(dimension=dimension, style=style):
                    def change(sheet):
                        target = (sheet.row_dimensions[2] if dimension == 'row' else
                                  sheet.column_dimensions['B'])
                        if style == 'font':
                            target.font = Font(bold=True, color='FFFF0000')
                        else:
                            target.number_format = '000000'

                    source = workbook_bytes([('001', 123)], change)
                    original = bytes(source)
                    phases = []
                    with self.assertRaisesRegex(MergeError, 'целых строк или столбцов'):
                        merge_xlsx([('styled.xlsx', source),
                                    ('plain.xlsx', workbook_bytes([('002', 456)]))],
                                   progress=lambda phase, *_: phases.append(phase))
                    self.assertEqual(source, original)
                    self.assertNotIn('write', phases)
                    self.assertNotIn('save', phases)


class SaveStageOwnership(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.guard = ConverterGuard(adapter=FakeAdapter(), recovery_parent=self.root/'owned')
        self.addCleanup(self.guard.close)
        identity = self.guard.create([{'id':'f', 'name':'source.docx', 'size':4}], ['pdf'])['jobId']
        self.guard.upload(identity, 'f', io.BytesIO(b'keep'), 4)
        self.guard.start(identity, archive_mode='on_demand')
        wait_finished(self.guard, identity)
        self.job = self.guard._jobs[identity]
        self.artifact = self.job.artifacts[0]

    def test_exclusive_stage_collision_preserves_other_writer_file(self):
        original_open = Path.open
        collided = []

        def existing_stage(path, mode='r', *args, **kwargs):
            if mode == 'xb' and '.aio-part-' in path.name:
                with original_open(path, 'xb') as stream:
                    stream.write(b'OTHER_WRITER')
                collided.append(path)
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, 'open', existing_stage):
            with self.assertRaises(GuardError):
                self.guard.save_folder(self.artifact.ticket, self.root)
        self.assertEqual(len(collided), 1)
        self.assertEqual(collided[0].read_bytes(), b'OTHER_WRITER')
        self.assertFalse(self.artifact.saved)
        self.assertTrue(self.artifact.path.exists())

    def test_replaced_stage_survives_failed_publication(self):
        replacements = []

        def replace_then_fail(part, target, cancel):
            part.replace(part.with_name('displaced-owned-stage'))
            part.write_bytes(b'OTHER_WRITER')
            replacements.append(part)
            raise OSError('Publication interrupted')

        with patch('app_backend.converter_guard.core._publish_saved_copy', replace_then_fail):
            with self.assertRaises(GuardError):
                self.guard.save_folder(self.artifact.ticket, self.root)
        self.assertEqual(replacements[0].read_bytes(), b'OTHER_WRITER')
        self.assertFalse(self.artifact.saved)
        self.assertTrue(self.artifact.path.exists())

    def test_stage_replacement_is_rejected_before_publication(self):
        from app_backend.file_identity import file_identity
        replacements = []

        def replace_before_identity(path):
            if '.aio-part-' in Path(path).name and not replacements:
                path.replace(path.with_name('displaced-owned-stage'))
                path.write_bytes(b'OTHER_WRITER')
                replacements.append(path)
            return file_identity(path)

        with patch('app_backend.file_identity.file_identity', replace_before_identity):
            with patch('app_backend.converter_guard.core._publish_saved_copy') as publish:
                with self.assertRaises(GuardError) as error:
                    self.guard.save_folder(self.artifact.ticket, self.root)
                publish.assert_not_called()
        self.assertEqual(error.exception.code, 'SAVE_CHANGED')
        self.assertEqual(replacements[0].read_bytes(), b'OTHER_WRITER')
        self.assertFalse(self.artifact.saved)

    def test_cancellation_removes_its_own_stage_and_retains_output(self):
        cancel = threading.Event()
        original_open = Path.open
        stages = []

        def cancel_after_stage_open(path, mode='r', *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            if mode == 'xb' and '.aio-part-' in path.name:
                stages.append(path)
                cancel.set()
            return stream

        with patch.object(Path, 'open', cancel_after_stage_open):
            with self.assertRaises(GuardError) as error:
                self.guard.save_folder(self.artifact.ticket, self.root, cancel=cancel)
        self.assertEqual(error.exception.code, 'SAVE_CANCELLED')
        self.assertEqual(len(stages), 1)
        self.assertFalse(stages[0].exists())
        self.assertFalse(self.artifact.saved)
        self.assertTrue(self.artifact.path.exists())

    def test_successful_save_removes_its_stage(self):
        result = self.guard.save_folder(self.artifact.ticket, self.root)
        self.assertTrue(result['readbackVerified'])
        self.assertFalse(list((self.root/result['name']).rglob('*.aio-part-*')))
        self.assertTrue(self.artifact.saved)


if __name__ == '__main__':
    unittest.main()
