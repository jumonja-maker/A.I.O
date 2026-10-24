"""Native legacy sheet/graphic boundary contracts; real API tests live in QA."""
from pathlib import Path
import json
from types import SimpleNamespace
import tempfile
import unittest

from app_backend.converter_guard.uno_session import WORKER_SOURCE


def functions():
    namespace = {'__name__': 'legacy_export_boundary'}
    exec(compile(WORKER_SOURCE, 'legacy_export_boundary', 'exec'), namespace)
    return namespace


def prop(name, value):
    return SimpleNamespace(Name=name, Value=value)


class Sheet:
    def __init__(self, name, values):
        self.name, self.values = name, values
        self.restores = []
        self.style = 'number/date formats from importSheet'
    def getName(self):
        return self.name
    def setName(self, name):
        self.name = name
    def getCellByPosition(self, column, row):
        def set_string(value):
            self.values[row][column] = value
        return SimpleNamespace(getString=lambda: str(self.values[row][column]), setString=set_string)
    def getRows(self):
        def insert(index, count):
            for _ in range(count):
                self.values.insert(index, [''] * len(self.values[0]))
        return SimpleNamespace(insertByIndex=insert)
    def createCursor(self):
        return SimpleNamespace(gotoEndOfUsedArea=lambda _: None,
            getRangeAddress=lambda: SimpleNamespace(EndRow=len(self.values)-1,
                                                    EndColumn=len(self.values[0])-1))
    def getCellRangeByPosition(self, c, r, last_c, last_r):
        def put(values):
            self.restores.append((c, r, values))
            for offset, row in enumerate(values):
                self.values[r+offset][c:last_c+1] = row
        return SimpleNamespace(getDataArray=lambda: tuple(tuple(row[c:last_c+1])
            for row in self.values[r:last_r+1]), setDataArray=put)


class Sheets:
    def __init__(self, *sheets):
        self.sheets = list(sheets)
    def getCount(self):
        return len(self.sheets)
    def getByIndex(self, index):
        return self.sheets[index]
    def getElementNames(self):
        return tuple(s.name for s in self.sheets)
    def removeByName(self, name):
        self.sheets = [s for s in self.sheets if s.name != name]
    def importSheet(self, model, name, index):
        source = next(s for s in model.sheets.sheets if s.name == name)
        # Simulate a formula cached value changing during native sheet copy.
        copy = Sheet(name, [['RECALCULATED'] * len(source.values[0]) for _ in source.values])
        self.sheets.insert(index, copy)
        return index


class Model:
    def __init__(self, *sheets):
        self.sheets = Sheets(*sheets)
        self.closed = False
        self.automatic = True
        self.exports = []
    def getSheets(self):
        return self.sheets
    def enableAutomaticCalculation(self, value):
        self.automatic = value
    def close(self, _):
        self.closed = True
    def storeToURL(self, url, options):
        self.exports.append((url, options, self.sheets.sheets[0].values))


class LegacyNativeContracts(unittest.TestCase):
    def test_all_sheets_export_separately_with_original_evaluated_values(self):
        source = Model(Sheet('one', [['ID', 'Value'], ['A', '00123']]),
                       Sheet('two', [['ID', 'Value'], ['B', 42.5]]),
                       Sheet('three', [['ID', 'Value'], ['C', '=literal']]))
        copies, calls = [], []
        def load(url, frame, flags, options):
            calls.append((url, {p.Name:p.Value for p in options}))
            copy = Model(Sheet('default', [['']]))
            copies.append(copy)
            return copy
        with tempfile.TemporaryDirectory() as tmp:
            paths = functions()['export_table_sheets'](source,
                SimpleNamespace(loadComponentFromURL=load), prop,
                Path(tmp)/'book.dif', [prop('FilterName', 'DIF'),
                    prop('InteractionHandler', object())])
        self.assertEqual([p.name for p in paths], ['book.dif', 'book__sheet_002.dif', 'book__sheet_003.dif'])
        self.assertEqual([m.exports[0][2] for m in copies], [s.values for s in source.sheets.sheets])
        self.assertTrue(all(len(m.sheets.sheets) == 1 and m.closed and not m.automatic for m in copies))
        self.assertTrue(all(c[1]['MacroExecutionMode'] == 0 and c[1]['UpdateDocMode'] == 0 for c in calls))
        self.assertEqual(source.exports, [])
        self.assertFalse(source.closed)

    def test_native_formula_error_is_preserved_as_its_displayed_error_text(self):
        first = Sheet('one', [['Value'], [None]])
        first.getCellByPosition = lambda column, row: SimpleNamespace(getString=lambda: '#DIV/0!')
        source = Model(first, Sheet('two', [['Value'], ['ok']]))
        copies = []
        def load(*args):
            copy = Model(Sheet('default', [['']]))
            copies.append(copy)
            return copy
        with tempfile.TemporaryDirectory() as tmp:
            functions()['export_table_sheets'](source, SimpleNamespace(loadComponentFromURL=load),
                prop, Path(tmp)/'book.dif', [prop('InteractionHandler', object())])
        self.assertEqual(copies[0].exports[0][2][1][0], '#DIV/0!')

    def test_dbf_mixed_column_uses_native_memo_without_changing_numeric_column(self):
        source = Model(Sheet('Sheet1', [['Mixed', 'Amount'], [12, 7.5], ['Привет', 8.5]]))
        copies = []
        def load(*args):
            copy = Model(Sheet('Sheet1', [['']]))
            copies.append(copy)
            return copy
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'mixed.dbf'
            functions()['export_table_sheets'](source, SimpleNamespace(loadComponentFromURL=load),
                prop, target, [prop('FilterName', 'dBase'), prop('InteractionHandler', object())])
            mapping = [json.loads(line) for line in target.with_name(target.name + '.aio-columns.jsonl').read_text().splitlines()]
        self.assertEqual(copies[0].exports[0][2], [['Mixed,M', 'Amount'], ['12', 7.5], ['Привет', 8.5]])
        self.assertEqual(mapping[0]['originalHeaders'], ['Mixed', 'Amount'])
        self.assertEqual(mapping[0]['memoColumns'], [0])
        self.assertEqual(mapping[1], {'row': 1, 'column': 0, 'sourceType': 'number', 'sourceValue': 12, 'exportedText': '12'})
        self.assertEqual(mapping[2]['exportedText'], 'Привет')
        self.assertEqual(source.sheets.sheets[0].values, [['Mixed', 'Amount'], [12, 7.5], ['Привет', 8.5]])
        self.assertEqual(copies[0].sheets.getCount(), 1)
        self.assertTrue(copies[0].closed)

    def test_headerless_dbf_mixed_column_keeps_first_source_record(self):
        source = Model(Sheet('data', [[12, 7.5], ['Привет', 8.5]]))
        copy = Model(Sheet('default', [['']]))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'no-header.dbf'
            functions()['export_table_sheets'](source, SimpleNamespace(loadComponentFromURL=lambda *args: copy),
                prop, target, [prop('FilterName', 'dBase'), prop('InteractionHandler', object())])
            metadata = json.loads(target.with_name(target.name + '.aio-columns.jsonl').read_text().splitlines()[0])
        self.assertFalse(metadata['sourceHasHeader'])
        self.assertEqual(copy.exports[0][2], [['FIELD1,M', 'FIELD2'], ['12', 7.5], ['Привет', 8.5]])

    def test_single_sheet_remains_a_direct_native_export(self):
        source = Model(Sheet('one', [['ID'], ['A']]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'book.dbf'
            paths = functions()['export_table_sheets'](source,
                SimpleNamespace(loadComponentFromURL=lambda *args: self.fail('Unexpected copy')),
                prop, path, [prop('FilterName', 'dBase')])
        self.assertEqual(paths, [path])
        self.assertEqual(len(source.exports), 1)

    def test_snapshot_is_closed_even_when_native_export_rejects_data(self):
        source = Model(Sheet('one', [['A']]), Sheet('two', [['B']]))
        copy = Model(Sheet('default', [['']]))
        def reject(*args):
            raise RuntimeError('Native field width rejected')
        copy.storeToURL = reject
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, 'field width'):
            functions()['export_table_sheets'](source,
                SimpleNamespace(loadComponentFromURL=lambda *args: copy), prop,
                Path(tmp)/'book.dbf', [prop('InteractionHandler', object())])
        self.assertTrue(copy.closed)

    def test_svm_uses_actual_graphic_capability_and_every_page(self):
        calls, selected = [], []
        def save(options):
            from urllib.parse import urlsplit
            from urllib.request import url2pathname
            settings = {p.Name:p.Value for p in options}
            calls.append(settings)
            Path(url2pathname(urlsplit(settings['URL']).path)).write_bytes(b'VCLMTF' + b'\0' * 64)
            return True
        encoder = SimpleNamespace(supportsMimeType=lambda value: value == 'image/x-svm',
            setSourceDocument=selected.append, filter=save)
        remote = SimpleNamespace(ServiceManager=SimpleNamespace(
            createInstanceWithContext=lambda name, context: encoder))
        pages = SimpleNamespace(getCount=lambda: 3, getByIndex=lambda index: 'native-page-'+str(index))
        with tempfile.TemporaryDirectory() as tmp:
            paths = functions()['export_draw_svm'](
                SimpleNamespace(getDrawPages=lambda: pages), remote, prop, Path(tmp)/'draw.svm')
        self.assertEqual(selected, ['native-page-0', 'native-page-1', 'native-page-2'])
        self.assertEqual([p.name for p in paths], ['draw.svm', 'draw__page_002.svm', 'draw__page_003.svm'])
        self.assertTrue(all(c['MediaType'] == 'image/x-svm' and not c['Overwrite'] for c in calls))

    def test_absent_svm_encoder_is_not_replaced_with_another_format(self):
        encoder = SimpleNamespace(supportsMimeType=lambda value: False)
        remote = SimpleNamespace(ServiceManager=SimpleNamespace(
            createInstanceWithContext=lambda name, context: encoder))
        pages = SimpleNamespace(getCount=lambda: 1)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, 'SVM_UNAVAILABLE'):
            functions()['export_draw_svm'](SimpleNamespace(getDrawPages=lambda: pages), remote,
                                           prop, Path(tmp)/'draw.svm')


if __name__ == '__main__':
    unittest.main()
