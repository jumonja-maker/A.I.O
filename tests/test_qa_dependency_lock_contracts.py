"""A09/A10: QA workflow and dependency manifest cannot silently regress."""
from pathlib import Path
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'tools' / 'build'


class LockedQaBuild(unittest.TestCase):
    def test_runtime_and_build_require_exact_versions(self):
        helper = runpy.run_path(str(BUILD / 'verify_qa_dependency_lock.py'))
        parse = helper['parse_lock']
        runtime = parse(BUILD / 'requirements-qa-locked.txt')
        build = parse(BUILD / 'requirements-qa-build-locked.txt')
        self.assertTrue({'openpyxl', 'pyside6', 'pypdf', 'pillow'} <= set(runtime))
        self.assertTrue({'pyinstaller', 'pyinstaller-hooks-contrib'} <= set(build))
        self.assertTrue(all(version for name, version in runtime.values()))
        self.assertTrue(all(version for name, version in build.values()))

    def test_workflow_checks_exact_dependencies_and_covers_future_main(self):
        workflow = (ROOT / '.github/workflows/aio-hosted-qa.yml').read_text(encoding='utf-8')
        self.assertIn('push:\n    branches: [main]', workflow)
        self.assertIn('qa_wheels.py --phase runtime', workflow)
        self.assertIn('qa_wheels.py --phase build', workflow)
        self.assertIn('--wheel-receipt build/aio-qa-wheels-runtime.json', workflow)
        self.assertIn('--wheel-receipt build/aio-qa-wheels-build.json', workflow)
        self.assertIn('verify_qa_dependency_lock.py --phase build', workflow)
        self.assertIn('AIO_BUNDLED_DEPENDENCY_MANIFEST=PASS', workflow)
        self.assertNotIn('uses: actions/upload-artifact', workflow)

    def test_frozen_qa_spec_requires_source_matched_manifest(self):
        spec = (ROOT / 'AllInOne.spec').read_text(encoding='utf-8')
        self.assertIn('aio-qa-dependencies.json', spec)
        self.assertIn('Missing source-bound QA dependency manifest', spec)
        self.assertIn('qa_info.get', spec)
