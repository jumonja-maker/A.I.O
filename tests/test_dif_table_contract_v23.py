"""B28: silent DIF truncation, not performance or optional full-fidelity QA."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape, quoteattr
import zipfile

from app_backend.converter_guard.dif_table_contract import check_dif_table
from app_backend.converter_guard.errors import GuardError
from b28_dif_fixtures import original_dif, SHA256

O = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
T = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
S = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
ROWS = [['ID', 'Text', 'Value'], ['001', 'Строка ё', 1.43], ['002', 'Последняя ячейка', -0.04]]


def dif_bytes(rows):
    lines = ['TABLE', '0,1', '"TEST"', 'VECTORS', '0,' + str(len(rows[0])), '""',
             'TUPLES', '0,' + str(len(rows)), '""', 'DATA', '0,0', '""']
    for row in rows:
        lines += ['-1,0', 'BOT']
        for value in row:
            lines += (['0,' + str(value), 'V'] if isinstance(value, (int, float)) else
                      ['1,0', '"' + value.replace('"', '""') + '"'])
    return ('\r\n'.join(lines + ['-1,0', 'EOD']) + '\r\n').encode()


def fods(rows):
    parts = [f'<o:document xmlns:o="{O}" xmlns:t="{T}" xmlns:s="{S}">',
             '<o:body><o:spreadsheet><t:table>']
    for row in rows:
        parts.append('<t:table-row>')
        for value in row:
            if isinstance(value, (int, float)):
                parts.append(f'<t:table-cell o:value-type="float" o:value="{value}"/>')
            else:
                parts.append('<t:table-cell o:value-type="string"><s:p>' + escape(value) + '</s:p></t:table-cell>')
        parts.append('</t:table-row>')
    return ''.join(parts) + '</t:table></o:spreadsheet></o:body></o:document>'


def html(rows):
    parts = ['<!DOCTYPE html><html><table>']
    for row in rows:
        parts.append('<tr>')
        for value in row:
            attrs = ' sdval=' + quoteattr(str(value)) if isinstance(value, (int, float)) else ''
            parts.append('<td' + attrs + '>' + escape(str(value)) + '</td>')
        parts.append('</tr>')
    return ''.join(parts) + '</table></html>'


def xlsx(path, rows):
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    parts = [f'<worksheet xmlns="{ns}"><dimension ref="A1"/><sheetData>']
    for ri, row in enumerate(rows, 1):
        parts.append(f'<row r="{ri}">')
        for ci, value in enumerate(row):
            ref = f'{chr(65+ci)}{ri}'
            if isinstance(value, (int, float)):
                parts.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                parts.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets><sheet name="Data" sheetId="1" r:id="r1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="{rel}/worksheet" Target="worksheets/data.xml"/></Relationships>')
        z.writestr('xl/worksheets/data.xml', ''.join(parts))


class DifTableContractV23(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'original.dif'; self.source.write_bytes(dif_bytes(ROWS))
        self.cancel = threading.Event()

    def check(self, rows=ROWS, target='html'):
        p = self.root/('output.' + target)
        if target in ('html','xhtml'): p.write_text(html(rows), encoding='utf-8')
        elif target == 'fods': p.write_text(fods(rows), encoding='utf-8')
        else: xlsx(p, rows)
        return check_dif_table([p], target, self.source, cancel=self.cancel)

    def test_correct_html_and_xhtml(self):
        for ext in ('html','xhtml'):
            self.assertEqual(self.check(target=ext)['matchedCells'], 9)

    def test_correct_xlsx_xlsm_xltx_xltm_with_stale_dimension(self):
        for ext in ('xlsx','xlsm','xltx','xltm'):
            value = self.check(target=ext)
            self.assertEqual(value['status'], 'checked_subset'); self.assertTrue(value['typesCompared'])

    def test_correct_fods(self):
        self.assertEqual(self.check(target='fods')['matchedCells'], 9)

    def test_known_russian_decimal_comma_html_matches_numeric_cells(self):
        output = self.root / 'result.html'
        rendered = (html(ROWS)
            .replace('sdval="1.43"', 'sdval="1,43" sdnum="1049;"')
            .replace('sdval="-0.04"', 'sdval="-0,04" sdnum="1049;"'))
        output.write_text(rendered, encoding='utf-8')
        proof = check_dif_table([output], 'html', self.source)
        self.assertEqual(proof['status'], 'checked_subset')
        self.assertEqual(proof['matchedCells'], 9)

    def test_russian_decimal_comma_changes_still_veto(self):
        output = self.root / 'result.html'
        rendered = (html(ROWS)
            .replace('sdval="1.43"', 'sdval="1,44" sdnum="1049;"')
            .replace('sdval="-0.04"', 'sdval="-0,04" sdnum="1049;"'))
        output.write_text(rendered, encoding='utf-8')
        with self.assertRaises(GuardError) as ctx:
            check_dif_table([output], 'html', self.source)
        self.assertEqual(ctx.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_comma_without_verified_russian_locale_is_not_checked(self):
        output = self.root / 'result.html'
        rendered = (html(ROWS)
            .replace('sdval="1.43"', 'sdval="1,43" sdnum="1033;"'))
        output.write_text(rendered, encoding='utf-8')
        self.assertEqual(check_dif_table([output], 'html', self.source)['status'], 'not_checked')

    def test_missing_tail_rejected(self):
        with self.assertRaises(GuardError) as ctx: self.check(rows=ROWS[:2])
        self.assertEqual(ctx.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')
        self.assertEqual(json.loads(ctx.exception.diagnostic)['differentCells'], 3)

    def test_missing_last_text_rejected(self):
        rows = [r[:] for r in ROWS]; rows[-1][1] = ''
        with self.assertRaises(GuardError): self.check(rows=rows)

    def test_same_count_wrong_text_rejected(self):
        rows = [r[:] for r in ROWS]; rows[-1][1] = 'different'
        with self.assertRaises(GuardError): self.check(rows=rows)

    def test_same_count_wrong_number_rejected(self):
        rows = [r[:] for r in ROWS]; rows[-1][2] = 42
        with self.assertRaises(GuardError): self.check(rows=rows)

    def test_string_id_must_not_turn_into_number(self):
        rows = [r[:] for r in ROWS]; rows[1][0] = 1
        with self.assertRaises(GuardError): self.check(rows=rows, target='xlsm')

    def test_coordinate_swap_rejected(self):
        rows = [r[:] for r in ROWS]; rows[1],rows[2] = rows[2],rows[1]
        with self.assertRaises(GuardError): self.check(rows=rows)

    def test_extra_nonempty_cell_rejected(self):
        with self.assertRaises(GuardError): self.check(rows=ROWS + [['extra']])

    def test_unsupported_target_is_not_certified(self):
        self.assertIsNone(check_dif_table([], 'pdf', self.source))

    def test_non_dif_is_not_constrained(self):
        self.source.write_text('ordinary text')
        self.assertIsNone(self.check())

    def test_unrecognized_dif_does_not_claim_loss(self):
        self.source.write_bytes(self.source.read_bytes().replace(b'0,1.43',b'0,1,43'))
        self.assertEqual(self.check()['status'], 'not_checked')

    def test_truncated_source_does_not_certify_prefix(self):
        self.source.write_bytes(self.source.read_bytes().replace(b'-1,0\r\nEOD\r\n', b''))
        self.assertEqual(self.check()['status'], 'not_checked')

    def test_encoding_uncertainty_has_no_source_text(self):
        self.source.write_bytes(self.source.read_bytes().replace('Строка ё'.encode(), b'\xffSECRET'))
        value = self.check(); self.assertEqual(value['status'], 'not_checked')
        self.assertNotIn('SECRET', json.dumps(value))

    def test_cancel_propagates(self):
        self.cancel.set()
        with self.assertRaises(GuardError) as ctx: self.check()
        self.assertEqual(ctx.exception.code,'CANCELLED')

    def test_budget_is_unknown_not_damage(self):
        out = self.root/'out.html'; out.write_text(html(ROWS), encoding='utf-8')
        self.assertEqual(check_dif_table([out],'html',self.source,seconds=0)['status'],'not_checked')

    def test_dtd_entity_not_expanded(self):
        out = self.root/'out.fods'
        out.write_text('<!DOCTYPE o:document [<!ENTITY e "SECRET">]>'+fods(ROWS),encoding='utf-8')
        value=check_dif_table([out],'fods',self.source)
        self.assertEqual(value['status'],'not_checked');self.assertIn('xml_declarations',value['reason'])

    def test_html_no_typed_numeric_evidence_remains_unknown(self):
        out=self.root/'out.html'; data=html(ROWS).replace(' sdval="1.43"','').replace(' sdval="-0.04"','')
        out.write_text(data,encoding='utf-8');self.assertEqual(check_dif_table([out],'html',self.source)['status'],'not_checked')

    def test_no_false_full_fidelity_claim_and_source_unchanged(self):
        old=self.source.read_bytes(); value=self.check()
        self.assertFalse(value['qualityCompared']);self.assertFalse(value['fullDocumentFidelity'])
        self.assertEqual(old,self.source.read_bytes())

    def test_all_eight_fixtures_match_preserved_sha(self):
        for level in range(1,9): self.assertEqual(hashlib.sha256(original_dif(level)).hexdigest(),SHA256[level-1])

    def test_real_stress_dimensions_with_synthetic_seven_cell_output(self):
        out=self.root/'broken.html'
        out.write_text(html([['ID','Label','Amount','Rate','Date','Memo'],['001']]),encoding='utf-8')
        for level in range(1,9):
            self.source.write_bytes(original_dif(level))
            with self.assertRaises(GuardError) as ctx:check_dif_table([out],'html',self.source)
            diag=json.loads(ctx.exception.diagnostic)
            self.assertEqual(diag['resultCells'],7);self.assertEqual(diag['differentCells'],(7+9*level)*6-7)


class DifCliBoundaryV23(unittest.TestCase):
    def test_live_uno_unresolved_filter_gets_snapshot_at_actual_cli_boundary(self):
        from app_backend.converter_guard.adapter import LibreOfficeAdapter
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);exe=root/'soffice.exe';exe.touch()
            src=root/'source.dif';src.write_bytes(dif_bytes(ROWS));original=src.read_bytes()
            adapter=LibreOfficeAdapter(exe)
            native=SimpleNamespace(valid=True)
            office=SimpleNamespace(disabled=False,request=lambda *a,**k: self.fail('ambiguous filter was sent to UNO'))
            adapter._registry=lambda:native;adapter._local.batch=(native,office)
            adapter._local.import_filter='DIF';adapter._local.import_options='76'
            def snapshot(source,workspace,**kw):
                self.assertEqual(source,src);workspace.mkdir(parents=True)
                out=workspace/'source.fods';out.write_text(fods(ROWS),encoding='utf-8')
                return out,{'kind':'native_dif_numeric_snapshot','numericCheck':{'status':'checked_subset'},'warnings':[]}
            calls=[]
            def convert(source,target,workspace,**kw):
                calls.append((source,target,kw.get('import_filter')))
                out=workspace/'output'/'source.html';out.parent.mkdir(parents=True)
                out.write_text(html(ROWS),encoding='utf-8');return [out]
            with patch('app_backend.converter_guard.adapter.native_export_recipe',return_value=None), \
                    patch.object(adapter,'_prepare_dif_snapshot',side_effect=snapshot), \
                    patch('app_backend.converter_guard.adapter.convert_path',side_effect=convert):
                adapter._convert_native(src,'html',root/'task',cancel=threading.Event(),timeout=None,on_started=lambda:None)
            self.assertEqual(calls[0][0].suffix,'.fods')
            self.assertEqual(calls[0][2],'OpenDocument Spreadsheet Flat XML')
            self.assertEqual(adapter.execution_mode(),'native_cli')
            self.assertEqual(adapter._local.import_filter,'DIF')
            self.assertEqual(adapter._local.import_options,'76');self.assertEqual(src.read_bytes(),original)
            self.assertEqual(adapter.export_info()['sourcePreparation']['tableCheck']['matchedCells'],9)

    def test_final_contract_runs_even_without_optional_quality_analysis(self):
        from app_backend.converter_guard.adapter import LibreOfficeAdapter
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);exe=root/'soffice.exe';exe.touch()
            source=root/'source.dif';source.write_bytes(dif_bytes(ROWS));original=source.read_bytes()
            prepared=root/'source.fods';prepared.write_text(fods(ROWS),encoding='utf-8')
            bad=root/'broken.html';bad.write_text(html(ROWS[:1]),encoding='utf-8')
            adapter=LibreOfficeAdapter(exe)
            adapter._registry=lambda:SimpleNamespace(fingerprint='unit')
            adapter._local.import_filter='DIF';adapter._local.import_options='76'
            summary={'kind':'native_dif_numeric_snapshot','numericCheck':{'status':'checked_subset'},'warnings':[]}
            with patch.object(adapter,'_prepare_dif_snapshot',return_value=(prepared,summary)), \
                    patch.object(adapter,'_convert_native',return_value=[bad]), \
                    patch('app_backend.converter_guard.native_metafile.native_metafile_target',return_value=None), \
                    patch('app_backend.converter_guard.adapter.check_native_output',return_value=None):
                with self.assertRaises(GuardError) as ctx:
                    adapter.convert(source,'html',root/'work',cancel=threading.Event(),timeout=None,on_started=lambda:None)
            self.assertEqual(ctx.exception.code,'ENGINE_OUTPUT_CONTENT_LOSS')
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual(adapter._local.import_filter,'DIF')
