"""Ensure the shipped Qt entry page loads the separate two-pane module."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class NativeViewerRegistration(unittest.TestCase):
    def test_entry_page_loads_viewer_before_comparison_with_no_duplicates(self):
        html=(ROOT/'index.html').read_text(encoding='utf-8')
        viewer='<script src="src/guard-comparison-viewer.js" defer></script>'
        analysis='<script src="src/guard-comparison.js" defer></script>'
        self.assertEqual(html.count(viewer),1)
        self.assertEqual(html.count(analysis),1)
        self.assertLess(html.index(viewer),html.index(analysis))
        self.assertTrue((ROOT/'src'/'guard-comparison-viewer.js').is_file())

if __name__=='__main__':unittest.main()
