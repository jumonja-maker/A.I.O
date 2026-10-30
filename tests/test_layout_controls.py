"""M01/M13/M15: keep real generated controller and browser layout in regression."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from app_backend.converter_assets import read_designer_asset
from app_backend.converter_layout_ui_patch import LAYOUT_CSS, TRASH_ANCHOR, apply_layout_controls

ROOT = Path(__file__).resolve().parents[1]


class LayoutControlsTests(unittest.TestCase):
    def test_svg_preserves_accessible_label_and_does_not_add_handlers(self):
        original = ('prefix <button ' + TRASH_ANCHOR + ' suffix').encode('utf-8')
        patched = apply_layout_controls(original).decode('utf-8')
        self.assertIn('aria-label="Очистить список исходников"', patched)
        self.assertIn('aria-hidden="true" focusable="false"', patched)
        self.assertIn('aio-cv__trash-icon', patched)
        self.assertNotIn('🗑', patched)
        self.assertNotIn('onclick', patched)

    def test_missing_duplicate_or_reapplied_anchor_fails_closed(self):
        for data in (b'', (TRASH_ANCHOR*2).encode('utf-8'), apply_layout_controls(TRASH_ANCHOR.encode('utf-8'))):
            with self.subTest(data=data[:50]), self.assertRaises(ValueError):
                apply_layout_controls(data)

    def test_materialized_product_has_svg_and_preserves_existing_actions(self):
        code = read_designer_asset(str(ROOT), 'file-converter.js').decode('utf-8')
        self.assertIn('aio-cv__trash-icon', code)
        self.assertEqual(code.count('data-cv-clear-inputs title='), 1)
        self.assertIn('local(runBtn,\'click\',runConversion)', code)
        self.assertIn('local(saveBtn,\'click\',saveOutput)', code)
        self.assertIn('Закрыть</button>', code)
        self.assertIn('Сохранить результаты</button>', code)

    def test_materialized_css_contains_bounded_layout_and_theme_states(self):
        css = read_designer_asset(str(ROOT), 'file-converter.css').decode('utf-8')
        self.assertTrue(css.endswith(LAYOUT_CSS))
        self.assertIn('visibility: hidden', css)
        self.assertIn('color: var(--danger)', LAYOUT_CSS)
        self.assertIn('color: var(--muted)', LAYOUT_CSS)
        self.assertNotIn('setInterval', LAYOUT_CSS)

    def test_real_webengine_viewports_and_save_states(self):
        # Reuse the existing live host, synthetic native adapter and teardown;
        # do not collect inherited HTTP tests for a second time.
        import test_converter_native_dom as dom
        fixture = dom.NativeUIDOMAcceptance('test_ui_contract')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT/'tools/qa/layout_controls_webengine.py'),
                '--url',fixture.base,'--output',directory],env=os.environ.copy(),
                capture_output=True,text=True,encoding='utf-8',timeout=160)
            evidence_path=Path(directory)/'evidence.json'
            evidence=json.loads(evidence_path.read_text(encoding='utf-8')) if evidence_path.exists() else {}
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
            self.assertEqual(evidence.get('status'),'PASS',evidence)
            self.assertEqual(len(evidence['cases']),10)
            self.assertEqual(evidence['installedGUI'],'NOT_RUN')
            print(result.stdout.strip())
