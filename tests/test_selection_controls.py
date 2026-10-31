"""M05/M06 selection tests execute the materialized product handlers."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from app_backend.converter_assets import read_designer_asset

ROOT = Path(__file__).resolve().parents[1]


class SelectionControls(unittest.TestCase):
    def check_case(self, case):
        module = os.environ.get('AIO_DOM_MODULE')
        if not module or not shutil.which('node'):
            self.skipTest('DOM acceptance requires Node and AIO_DOM_MODULE')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'controller.js'
            source.write_bytes(read_designer_asset(str(ROOT), 'file-converter.js'))
            result = subprocess.run(['node', str(ROOT/'tests/selection_controls_dom.mjs'),
                                     str(source), case], capture_output=True, text=True,
                                    encoding='utf-8', timeout=30, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
        self.assertIn('AIO_SELECTION_CONTROLS=', result.stdout)

    def test_single_card_keeps_nodes_and_scroll(self): self.check_case('single')
    def test_bulk_second_click_clears(self): self.check_case('toggle')
    def test_bulk_keeps_card_nodes_and_scroll(self): self.check_case('bulk_identity')
    def test_only_verified_targets_for_selected_inputs(self): self.check_case('eligibility')
    def test_cross_category_selection_and_partial_selection(self): self.check_case('categories')
    def test_processing_checking_and_saving_are_immutable(self): self.check_case('busy')
    def test_empty_input_selection_does_nothing(self): self.check_case('empty')


if __name__ == '__main__': unittest.main()
