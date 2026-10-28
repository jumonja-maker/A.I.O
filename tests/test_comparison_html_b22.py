from pathlib import Path
import tempfile
import unittest

from app_backend.converter_guard.comparison_analysis import analyze_pair,collect_source_summary,preview_text


class HtmlComparisonEvidence(unittest.TestCase):
    def test_visible_markup_text_entities_tables_and_script_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.html';result=Path(tmp)/'output.txt'
            source.write_text('<!DOCTYPE html><html><head><title>Title excluded</title></head><body><h1>Привет &amp; мир</h1><table><tr><td>001</td><td>A<br>B</td></tr></table><script>secret()</script></body></html>')
            summary=collect_source_summary(source)
            self.assertTrue(summary['complete']);self.assertEqual(summary['counts']['tables'],1)
            self.assertEqual(summary['_grids'],[],'dynamic HTML cannot prove visible table values')
            self.assertNotIn('secret',summary['_text']);self.assertNotIn('Title',summary['_text'])
            self.assertFalse(summary['_text_complete'])
            source.write_text(source.read_text().replace('<script>secret()</script>',''))
            simple=collect_source_summary(source)
            self.assertEqual(simple['_grids'][0]['rows'][0][0]['value'],'001')
            self.assertEqual(simple['_grids'][0]['rows'][0][1]['value'],'A B')
            self.assertEqual(preview_text(source)['kind'],'text')

    def test_basic_html_and_writer_style_text_are_proven_without_claiming_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.html';result=Path(tmp)/'output.txt'
            source.write_text('<html><body><p>Контроль 001</p><p>Конец</p></body></html>')
            result.write_text('Контроль 001\nКонец')
            report=analyze_pair(source,[result],'txt')
            self.assertIn('normalized_text',report['checkedFeatures'])
            self.assertEqual(report['status'],'checked')
            self.assertIn('html_css_layout',report['unknown'])
            result.write_text('')
            lost=analyze_pair(source,[result],'txt')
            self.assertEqual(lost['status'],'confirmed_loss')

    def test_missing_resource_or_complex_tables_never_become_proven_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.html'
            source.write_text('<html><body><p>Body</p><img src="missing.png"></body></html>')
            self.assertFalse(collect_source_summary(source)['complete'])
            source.write_text('<html><body><table><tr><td colspan="2">A</td></tr></table></body></html>')
            summary=collect_source_summary(source)
            self.assertTrue(summary['complete']);self.assertEqual(summary['_grids'],[])
            self.assertIn('html_complex_table_mapping',summary['unknown'])
            source.write_text('<html><body><p hidden>Hidden</p><p>Body</p></body></html>')
            self.assertFalse(collect_source_summary(source)['_text_complete'])
            empty=Path(tmp)/'empty.txt';empty.write_text('')
            self.assertEqual(analyze_pair(source,[empty],'txt')['status'],'uncertain')
