"""A09: a green aggregate requires both platforms and every mandatory stage."""
from pathlib import Path
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / 'tools/qa/required_qa_gate.py'))


class RequiredQaGate(unittest.TestCase):
    def steps(self):
        return {name: {'outcome':'success', 'conclusion':'success'} for name in
                ('deps', 'units', 'windows_engine', 'linux_engine', 'fidelity', 'vector_pages', 'package')}

    def test_both_platform_policies_accept_their_complete_successes(self):
        for platform in ('Windows', 'Linux'):
            self.assertIn('units', GATE['validate_steps'](platform, self.steps()))
        linux = self.steps()
        for name in ('windows_engine', 'fidelity', 'vector_pages', 'package'):
            linux[name] = {'outcome':'skipped', 'conclusion':'skipped'}
        GATE['validate_steps']('Linux', linux)

    def test_missing_skipped_failed_cancelled_and_masked_steps_fail(self):
        for platform in ('Windows', 'Linux'):
            required = GATE['validate_steps'](platform, self.steps())
            for name in required:
                for result in (None, {}, {'outcome':'failure', 'conclusion':'success'},
                               *({'outcome':s, 'conclusion':s} for s in ('failure', 'cancelled', 'skipped'))):
                    with self.subTest(platform=platform, name=name, result=result):
                        steps = self.steps()
                        steps[name] = result
                        with self.assertRaises(ValueError):
                            GATE['validate_steps'](platform, steps)

    def test_only_successful_matrix_is_accepted(self):
        GATE['validate_matrix']('success')
        for result in (None, '', 'failure', 'cancelled', 'skipped', 'neutral', True):
            with self.subTest(result=result), self.assertRaises(ValueError):
                GATE['validate_matrix'](result)

    def test_unknown_platform_or_malformed_steps_is_rejected(self):
        for platform, steps in (('macOS', self.steps()), ('Linux', []), ('Windows', None)):
            with self.assertRaises(ValueError):
                GATE['validate_steps'](platform, steps)

    def test_workflow_has_unconditional_gate_and_preinstall_lock_checks(self):
        workflow = (ROOT / '.github/workflows/aio-hosted-qa.yml').read_text(encoding='utf-8')
        self.assertIn('merge_group:', workflow)
        self.assertIn('name: AIO QA gate', workflow)
        self.assertIn('needs: [qa]', workflow)
        self.assertIn("AIO_QA_MATRIX_RESULT: ${{ needs.qa.result }}", workflow)
        self.assertIn('AIO_QA_STEPS: ${{ toJSON(steps) }}', workflow)
        self.assertIn('required_qa_gate.py --matrix', workflow)
        self.assertNotIn('paths:', workflow)
        self.assertNotIn('paths-ignore:', workflow)
        for phase, filename in (('runtime', 'requirements-qa-locked.txt'), ('build', 'requirements-qa-build-locked.txt')):
            check = 'verify_qa_dependency_lock.py --phase ' + phase + ' --check-locks'
            install = 'python -m pip install -r tools/build/' + filename
            self.assertLess(workflow.index(check), workflow.index(install))
        final_job = workflow.split('  required-qa:', 1)[1]
        self.assertIn('if: ${{ always() }}', final_job)
        self.assertNotIn('continue-on-error', workflow)
