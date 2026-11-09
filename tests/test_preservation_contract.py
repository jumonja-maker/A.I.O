"""Native preservation policy tests; no LibreOffice, Qt or source I/O required."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('aio_policy_test',
    ROOT / 'app_backend/converter_guard/preservation_contract.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
CALC = ('com.sun.star.sheet.SpreadsheetDocument',)
WRITER = ('com.sun.star.text.TextDocument',)
IMPRESS = ('com.sun.star.presentation.PresentationDocument',)
DRAW = ('com.sun.star.drawing.DrawingDocument',)


class PreservationPolicyTests(unittest.TestCase):
    def test_unrelated_routes_have_no_contract(self):
        for services, target in ((WRITER, 'pdf'), (CALC, 'xlsx'), ((), 'png')):
            self.assertIsNone(P.aio_plan_preservation(services, target, None))

    def test_every_single_sheet_format_requires_all_sheets(self):
        for target in ('csv', 'tab', 'tsv', 'dbf', 'dif', 'slk', 'sylk'):
            with self.subTest(target=target):
                plan = P.aio_plan_preservation(CALC, target, 4)
                self.assertEqual((plan.unit, plan.expected), ('sheets', 4))

    def test_draw_and_impress_require_all_pages(self):
        for services in (DRAW, IMPRESS):
            for target in ('png', 'svg', 'jpg', 'tiff', 'webp'):
                plan = P.aio_plan_preservation(services, target, 3)
                self.assertEqual((plan.unit, plan.expected), ('pages', 3))

    def test_writer_jpeg_aliases_share_page_contract(self):
        for target in ('jpg', 'jpeg', 'jfif', 'jif', 'jpe', 'png', 'webp'):
            self.assertEqual(P.aio_plan_preservation(WRITER, target, 9).expected, 9)

    def test_unknown_count_is_never_assumed_to_be_one(self):
        for count in (None, True, False, 0, -1, 1.0, '4'):
            with self.subTest(count=count):
                with self.assertRaises(P.AioPreservationIncomplete):
                    P.aio_plan_preservation(CALC, 'slk', count)

    def test_plans_are_immutable_and_task_local(self):
        first = P.aio_plan_preservation(CALC, 'slk', 4)
        second = P.aio_plan_preservation(WRITER, 'png', 2)
        with self.assertRaises(AttributeError):
            first.expected = 1
        self.assertEqual((first.expected, second.expected), (4, 2))

    def test_sidecars_cannot_mask_missing_sheets(self):
        plan = P.aio_plan_preservation(CALC, 'dbf', 4)
        with self.assertRaises(P.AioPreservationLoss):
            P.aio_verify_preservation(plan, ['source.dbf', 'source.cpg',
                                           'AIO-sheets.json', 'note.txt'])

    def test_duplicate_paths_cannot_mask_missing_pages(self):
        plan = P.aio_plan_preservation(IMPRESS, 'png', 3)
        with self.assertRaises(P.AioPreservationLoss):
            P.aio_verify_preservation(plan, ['a.png', 'b.png', 'A.PNG'])

    def test_missing_and_extra_units_fail(self):
        plan = P.aio_plan_preservation(WRITER, 'png', 3)
        for outputs in (['1.png'], ['1.png', '2.png', '3.png', '4.png']):
            with self.assertRaises(P.AioPreservationLoss):
                P.aio_verify_preservation(plan, outputs)

    def test_paths_are_names_not_untrusted_locations(self):
        plan = P.aio_plan_preservation(WRITER, 'png', 1)
        for name in ('../a.png', 'nested/a.png', 'nested\\a.png'):
            with self.assertRaises(P.AioPreservationIncomplete):
                P.aio_verify_preservation(plan, [name])

    def test_checked_scope_does_not_claim_full_quality(self):
        plan = P.aio_plan_preservation(CALC, 'sylk', 4)
        report = P.aio_verify_preservation(plan,
            ['a.sylk', 'a__sheet_002.sylk', 'a__sheet_003.sylk', 'a__sheet_004.sylk'])
        self.assertEqual(report['expectedUnits'], report['observedOutputs'])
        self.assertEqual(report['status'], 'checked_subset')
        self.assertFalse(report['qualityCompared'])
        self.assertIn('original_import_equivalence', report['notChecked'])

    def test_same_pixel_content_is_not_automatically_a_lost_page(self):
        report = P.aio_verify_preservation(P.aio_plan_preservation(DRAW, 'png', 2),
                                           ['page1.png', 'page2.png'])
        self.assertEqual(report['observedOutputs'], 2)

    def test_no_file_io_or_subprocess_for_policy_decisions(self):
        with patch('builtins.open', side_effect=AssertionError('extra I/O')), \
                patch('subprocess.run', side_effect=AssertionError('extra process')):
            for _ in range(1000):
                plan = P.aio_plan_preservation(CALC, 'slk', 4)
                P.aio_verify_preservation(plan, ['a.slk', 'b.slk', 'c.slk', 'd.slk'])

    def test_embedded_worker_uses_identical_policy(self):
        embedded = {}
        exec(compile(P.PRESERVATION_POLICY_SOURCE, '<worker policy>', 'exec'), embedded)
        plan = embedded['aio_plan_preservation'](CALC, 'slk', 2)
        report = embedded['aio_verify_preservation'](plan, ['a.slk', 'b.slk'])
        self.assertEqual(report, P.aio_verify_preservation(
            P.aio_plan_preservation(CALC, 'slk', 2), ['a.slk', 'b.slk']))

    def test_unimplemented_calc_graphics_are_not_falsely_marked_covered(self):
        self.assertIsNone(P.aio_preservation_scope(CALC, 'png'))


if __name__ == '__main__':
    unittest.main()
