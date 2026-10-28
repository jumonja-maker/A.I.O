import csv
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import unquote, urlparse

from app_backend.converter_guard import GuardError
from app_backend.converter_guard.sheet_outputs import (
    SHEET_MANIFEST, SheetOutputMismatch, read_sheet_manifest, sheet_file_evidence,
    sheet_output_name, verify_csv_sheet, verify_fods_csv, write_sheet_manifest)
from app_backend.converter_guard.evidence_status import task_evidence
from test_guard_legacy_native_v17 import Model, Sheet, functions, prop
from test_guard_legacy_sheets_v17 import native_flat, sheet


class AllSheetsContracts(unittest.TestCase):
    def test_manifest_is_a_required_companion_not_fifth_user_document(self):
        from app_backend.converter_guard.core import _Artifact
        sheet=_Artifact('a'*32,Path('book-sheet-1.csv'),'book-sheet-1.csv',
                        12,'sheet-ticket',format='csv',
                        source_sheet={'index':0,'name':'Sheet1','indexBase':0})
        manifest=_Artifact('a'*32,Path('AIO-sheets.json'),'AIO-sheets.json',
                           32,'manifest-ticket',format='csv')
        self.assertFalse(sheet.companion)
        self.assertTrue(manifest.companion)
        self.assertFalse(sheet.metadata()['supportingFile'])
        self.assertTrue(manifest.metadata()['supportingFile'])
        # The sidecar is still a real, unsaved owned artifact until copied and
        # independently read back. It must not be silently marked saved.
        self.assertFalse(manifest.saved)

    def test_safe_names_do_not_depend_on_untrusted_sheet_names(self):
        for stem in ('../CON', 'NUL', 'COM1.ext', 'AUX', 'LPT9', 'a\\b', '.', 'x'*1000, 'Книга'):
            names = [sheet_output_name(stem, i, 'csv') for i in range(4)]
            self.assertEqual(len({n.casefold() for n in names}), 4)
            for name in names:
                self.assertLess(len(name.encode('utf-8')), 255)
                self.assertEqual(Path(name).name, name)
                self.assertNotIn('\\', name)
                self.assertNotIn('/', name)
                self.assertFalse(name.startswith('.'))

    def test_long_native_snapshot_stem_is_short_and_keeps_original_untouched(self):
        from app_backend.converter_guard.legacy_sheets import prepare_calc_sheet_snapshots
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            native=base/'small.fods'
            native.write_bytes(native_flat(sheet('Data','42.5')))
            before=hashlib.sha256(native.read_bytes()).hexdigest()
            outputs,proof=prepare_calc_sheet_snapshots(
                native,base/'snapshots',stem='Книга'*49,cancel=threading.Event())
            self.assertEqual(len(outputs),1)
            self.assertTrue(outputs[0].is_file())
            self.assertTrue(outputs[0].name.startswith('aio-snapshot-'))
            self.assertLess(len(outputs[0].name.encode('utf-16-le'))//2,255)
            self.assertEqual(hashlib.sha256(native.read_bytes()).hexdigest(),before)

    def test_complete_csv_including_literal_id_multiline_and_tail(self):
        rows = [['ID','Text','Value'], ['001','Первая\nстрока','42.5'], ['002','last','-7']]
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'sheet.csv'
            with p.open('w',encoding='utf-8-sig',newline='') as f:
                csv.writer(f).writerows(rows)
            check=verify_csv_sheet(p,'csv',3,3,lambda r,c:rows[r][c])
            self.assertEqual(check['status'],'checked_subset')
            self.assertEqual(check['checkedCells'],9)
            original=p.read_bytes()
            for damaged in ('ID,Text,Value\n001,first,42.5\n', 'ID,Text,Value\n1,first,42.5\n002,last,-7\n', '"unterminated'):
                p.write_text(damaged,encoding='utf-8')
                with self.assertRaises(SheetOutputMismatch):
                    verify_csv_sheet(p,'csv',3,3,lambda r,c:rows[r][c])
            p.write_bytes(original)

    def test_budget_is_unknown_and_cancel_is_never_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'sheet.csv';p.write_text('001\n')
            check=verify_csv_sheet(p,'csv',1000001,1,lambda r,c:'001')
            self.assertEqual(check['status'],'not_checked')
            event=threading.Event();event.set()
            with self.assertRaises(InterruptedError):
                verify_csv_sheet(p,'csv',1,1,lambda r,c:'001',cancel=event)

    def test_manifest_requires_complete_unchanged_unique_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);paths=[];entries=[]
            for i,name in enumerate(('CON','con','../escape','a/b')):
                p=root/sheet_output_name('book',i,'csv');p.write_text('001\n');paths.append(p)
                entries.append(sheet_file_evidence(p,i,name,{'status':'checked_subset'}))
            manifest=write_sheet_manifest(root,entries);paths.append(manifest)
            self.assertEqual(read_sheet_manifest(paths)['sheetCount'],4)
        self.assertEqual(SHEET_MANIFEST,'AIO-sheets.json')
            with self.assertRaises(GuardError):read_sheet_manifest(paths[:-2]+[manifest])
            paths[0].write_text('1\n')
            with self.assertRaises(GuardError):read_sheet_manifest(paths)

    def test_native_csv_exports_each_sheet_and_metadata_without_using_names_as_paths(self):
        source=Model(*(Sheet(n,[['ID','Value'],['001',str(i)],['999','END']])
                       for i,n in enumerate(('CON','con','../escape','name/elsewhere'))))
        copies=[]
        class Writer(Model):
            def storeToURL(self,url,options):
                p=Path(unquote(urlparse(url).path))
                if __import__('os').name=='nt' and p.as_posix().startswith('/'):
                    p=Path(str(p)[1:])
                with p.open('x',encoding='utf-8',newline='') as f:
                    csv.writer(f).writerows(self.sheets.sheets[0].values)
        def load(*args):
            copy=Writer(Sheet('default',[['']]));copies.append(copy);return copy
        with tempfile.TemporaryDirectory() as tmp:
            paths=functions()['export_table_sheets'](source,SimpleNamespace(loadComponentFromURL=load),
                prop,Path(tmp)/'book.csv',[prop('FilterName','Text - txt - csv (StarCalc)'),
                prop('FilterOptions','44,34,76,1,'),prop('InteractionHandler',object())],csv_format='csv')
            manifest=read_sheet_manifest(paths)
            self.assertEqual(len([p for p in paths if p.suffix=='.csv']),4)
            self.assertEqual([e['sourceSheetName'] for e in manifest['sheets']],
                             ['CON','con','../escape','name/elsewhere'])
            self.assertTrue(all(e['contentCheck']['status']=='checked_subset' for e in manifest['sheets']))
            self.assertTrue(all(m.closed for m in copies))
            self.assertEqual(source.exports,[])

    def test_cli_snapshot_display_and_native_formula_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fods=root/'one.fods';out=root/'one.csv'
            fods.write_bytes(native_flat(sheet('Data','42.5')));out.write_text('42.5\n')
            self.assertEqual(verify_fods_csv(fods,out,'csv')['status'],'checked_subset')
            out.write_text('7\n')
            with self.assertRaises(SheetOutputMismatch):verify_fods_csv(fods,out,'csv')

    def test_native_empty_repeated_tail_does_not_exhaust_value_budget(self):
        body=('<t:table t:name="Data"><t:table-row><t:table-cell o:value-type="string">'
            '<x:p>001</x:p></t:table-cell><t:table-cell t:number-columns-repeated="16383"/>'
            '</t:table-row><t:table-row t:number-rows-repeated="1048575">'
            '<t:table-cell t:number-columns-repeated="16384"/></t:table-row></t:table>')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fods=root/'native.fods';csv_file=root/'native.csv'
            fods.write_bytes(native_flat(body));csv_file.write_text('001\n')
            check=verify_fods_csv(fods,csv_file,'csv')
            self.assertEqual(check['status'],'checked_subset')
            csv_file.write_text('1\n')
            with self.assertRaises(SheetOutputMismatch):verify_fods_csv(fods,csv_file,'csv')

    def test_ready_save_readback_and_quality_remain_independent(self):
        artifacts=[SimpleNamespace(saved=True,readback_verified=True),SimpleNamespace(saved=False,readback_verified=False)]
        task=SimpleNamespace(artifacts=artifacts,quality=None,state='ready',format='csv',code=None,
            result_checks=[{'check':'native_calc_all_sheets','status':'checked_subset',
                'checkedFeatures':['sheet_count'],'unknownReasons':['csv_comparison_budget']}])
        observed=task_evidence(task)
        self.assertEqual(observed['conversionStatus'],'ready')
        self.assertEqual(observed['saveStatus'],'partial')
        self.assertFalse(observed['qualityCompared'])
        self.assertFalse(observed['readbackVerified'])
        self.assertEqual(observed['checkedFeatures'],['sheet_count'])
        self.assertIn('csv_comparison_budget',observed['unknownReasons'])
        self.assertFalse(observed['confirmedLoss'])
        artifacts[1].saved=True;artifacts[1].readback_verified=True;task.state='saved'
        self.assertTrue(task_evidence(task)['readbackVerified'])
        self.assertFalse(task_evidence(task)['qualityCompared'])


if __name__=='__main__':unittest.main()
