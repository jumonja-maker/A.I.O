import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app_backend.converter_guard.comparison_analysis import analyze_pair


class MappedSheetComparison(unittest.TestCase):
    def test_all_sheets_mapping_and_loss_location_are_independently_checked(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.xlsx';book=Workbook();book.remove(book.active);entries=[];paths=[]
            for index,name in enumerate(('Коды','Data')):
                sheet=book.create_sheet(name);sheet.append(['ID','Label']);sheet.append(['001','A'+str(index)])
                path=root/f'sheet-{index}.csv'
                with path.open('w',newline='',encoding='utf-8') as stream:csv.writer(stream).writerows(sheet.values)
                paths.append(path);entries.append({'sourceSheetIndex':index,'sourceSheetName':name,'file':path.name,'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            book.save(source);manifest=root/'AIO-sheets.json'
            def write():manifest.write_text(json.dumps({'schemaVersion':1,'scope':'whole_workbook','sheetIndexBase':0,'sheetCount':2,'sheets':entries}))
            write();report=analyze_pair(source,[*paths,manifest],'csv')
            self.assertIn('all_sheet_outputs_mapping',report['checkedFeatures'])
            self.assertIn('all_sheet_values_by_index_name',report['checkedFeatures'])
            self.assertNotIn('supplemental_outputs_not_inspected',report['unknown'])
            self.assertEqual(report['outputScope']['inspectedSupplementalFiles'],1)
            paths[1].write_text('ID,Label\n001,LOST\n')
            changed=analyze_pair(source,[*paths,manifest],'csv')
            self.assertEqual(changed['status'],'not_checked');self.assertFalse(changed['qualityCompared'])
            entries[1].update(size=paths[1].stat().st_size,sha256=hashlib.sha256(paths[1].read_bytes()).hexdigest());write()
            lost=analyze_pair(source,[*paths,manifest],'csv')
            self.assertEqual(lost['status'],'confirmed_loss')
            location=next(f for f in lost['findings'] if f['code']=='CELL_VALUES_CHANGED')['locations'][0]
            self.assertEqual((location['sheetName'],location['row'],location['column']),('Data',2,2))
            entries[1]['sourceSheetName']='Wrong';write()
            self.assertEqual(analyze_pair(source,[*paths,manifest],'csv')['status'],'not_checked')
