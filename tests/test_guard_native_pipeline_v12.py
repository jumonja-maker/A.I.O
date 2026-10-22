"""Ordinary conversion: defer redundant ZIP I/O, retain exact result ownership."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard import ConverterGuard, GuardError
from test_converter_guard_v02 import FakeAdapter, wait_finished


class NativePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.adapter = FakeAdapter()
        self.guard = ConverterGuard(adapter=self.adapter, spool_parent=self.root/'spool',
                                    recovery_parent=self.root/'recovery')

    def tearDown(self):
        self.adapter.resume.set()
        self.guard.close()
        self.temp.cleanup()

    def run_job(self, *, mode='on_demand', names=('first.csv', 'second.csv')):
        files = [{'id':str(i), 'name':name, 'size':1} for i, name in enumerate(names)]
        identity = self.guard.create(files, ['pdf'])['jobId']
        for file in files:
            self.guard.upload(identity, file['id'], io.BytesIO(b'x'), 1)
        self.guard.start(identity, archive_mode=mode)
        return identity, wait_finished(self.guard, identity)

    def test_on_demand_completion_never_opens_or_reserves_a_zip(self):
        with patch('app_backend.converter_guard.core.zipfile.ZipFile',
                   side_effect=AssertionError('ZIP must require an explicit request')):
            identity, state = self.run_job()
        self.assertEqual((state['succeeded'], state['state']), (2, 'ready'))
        self.assertTrue(state['bundleDeferred'])
        self.assertTrue(state['result']['folderOnly'])
        self.assertIsNone(state['packagingError'])
        self.assertFalse(list(self.guard._jobs[identity].root.glob('*.zip')))
        self.assertEqual(len(state['result']['outputs']), 2)

    def test_folder_save_contains_every_output_without_a_zip(self):
        identity, state = self.run_job()
        destination = self.root/'saved'; destination.mkdir()
        saved = self.guard.save_folder(state['result']['ticket'], destination)
        self.assertEqual((saved['savedAll'], saved['outputs']), (True, 2))
        paths = list((destination/saved['name']).glob('**/*.pdf'))
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.read_bytes() == b'data' for path in paths))
        self.assertFalse(list(self.guard._jobs[identity].root.glob('*.zip')))

    def test_explicit_zip_contains_all_outputs_and_keeps_success_state(self):
        identity, _ = self.run_job()
        accepted = self.guard.rebuild_bundle(identity)
        self.assertFalse(accepted['finished'])
        state = wait_finished(self.guard, identity)
        self.assertEqual(state['state'], 'ready')
        self.assertFalse(state['bundleDeferred'])
        self.assertFalse(state['result'].get('folderOnly', False))
        self.assertTrue(state['result']['name'].endswith('.zip'))
        bundle = self.guard._jobs[identity].bundle
        with zipfile.ZipFile(bundle.path) as archive:
            files = [n for n in archive.namelist() if n.endswith('.pdf')]
            self.assertEqual(len(files), 2)
            self.assertTrue(all(archive.read(name) == b'data' for name in files))
        ticket = state['result']['ticket']
        repeated = self.guard.rebuild_bundle(identity)
        self.assertEqual(repeated['result']['ticket'], ticket)

    def test_failed_explicit_zip_keeps_all_original_outputs_saveable(self):
        identity, state = self.run_job()
        with patch('app_backend.converter_guard.core.zipfile.ZipFile',
                   side_effect=OSError(28, 'Disk full')):
            self.guard.rebuild_bundle(identity)
            state = wait_finished(self.guard, identity)
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(state['packagingError'], 'BUNDLE_STORAGE_FAILED')
        self.assertEqual(len(state['result']['outputs']), 2)
        self.assertTrue(all(a.path.is_file() and not a.saved for a in self.guard._jobs[identity].artifacts))
        destination = self.root/'saved'; destination.mkdir()
        self.assertTrue(self.guard.save_folder(state['result']['ticket'], destination)['savedAll'])

    def test_default_auto_mode_retains_existing_download_contract(self):
        identity, state = self.run_job(mode='auto')
        self.assertFalse(state['bundleDeferred'])
        self.assertTrue(state['result']['name'].endswith('.zip'))
        self.assertIsNotNone(self.guard._jobs[identity].bundle)

    def test_single_output_does_not_need_a_zip_in_either_mode(self):
        for mode in ('auto', 'on_demand'):
            with self.subTest(mode=mode):
                _, state = self.run_job(mode=mode, names=('single.csv',))
                self.assertFalse(state['bundleDeferred'])
                self.assertFalse(state['result'].get('folderOnly', False))
                self.assertTrue(state['result']['name'].endswith('.pdf'))

    def test_invalid_archive_mode_is_rejected_before_start_mutates_job(self):
        identity = self.guard.create([{'id':'a', 'name':'one.csv', 'size':1}], ['pdf'])['jobId']
        self.guard.upload(identity, 'a', io.BytesIO(b'x'), 1)
        for value in (None, [], True, 'unknown'):
            with self.subTest(value=value):
                with self.assertRaises(GuardError) as error:
                    self.guard.start(identity, archive_mode=value)
                self.assertEqual(error.exception.code, 'ARCHIVE_POLICY')
                self.assertEqual(self.guard.status(identity)['state'], 'uploading')

    def test_cancelled_conversion_state_survives_explicit_zip(self):
        files = [{'id':str(i), 'name':name, 'size':1}
                 for i, name in enumerate(('first.csv', 'hold.csv'))]
        identity = self.guard.create(files, ['pdf'])['jobId']
        for file in files:
            self.guard.upload(identity, file['id'], io.BytesIO(b'x'), 1)
        self.guard.start(identity, archive_mode='on_demand')
        self.assertTrue(self.adapter.waiting.wait(2))
        self.guard.cancel(identity)
        state = wait_finished(self.guard, identity)
        self.assertEqual((state['state'], state['succeeded']), ('cancelled', 1))
        self.guard.rebuild_bundle(identity)
        rebuilt = wait_finished(self.guard, identity)
        self.assertEqual(rebuilt['state'], 'cancelled')
        self.assertEqual((rebuilt['succeeded'], rebuilt['cancelled']), (1, 1))

    def test_import_interaction_is_not_repeated_for_every_target_or_other_file(self):
        original = self.adapter.convert
        calls = []
        def convert(source, target, workspace, **kwargs):
            calls.append((source.name,target))
            if source.name == 'locked.csv':
                kwargs['on_started']()
                raise GuardError('PREFLIGHT_INTERACTION_REQUIRED', 'Import needs input.', stage='import')
            return original(source,target,workspace,**kwargs)
        self.adapter.convert = convert
        files = [{'id':'locked','name':'locked.csv','size':1},
                 {'id':'good','name':'good.csv','size':1}]
        identity = self.guard.create(files, ['pdf','csv','tsv'])['jobId']
        for file in files:
            self.guard.upload(identity,file['id'],io.BytesIO(b'x'),1)
        self.guard.start(identity,archive_mode='on_demand')
        state = wait_finished(self.guard,identity)
        self.assertEqual((state['succeeded'],state['failed'],state['attempted']),(3,3,4))
        self.assertEqual([name for name,_ in calls].count('locked.csv'),1)
        self.assertEqual([name for name,_ in calls].count('good.csv'),3)
        self.assertEqual(state['files'][1]['state'],'ready')

    def test_deferred_outputs_survive_restart_and_can_build_an_explicit_archive(self):
        identity, _ = self.run_job()
        self.guard.close()
        self.guard = ConverterGuard(adapter=self.adapter,spool_parent=self.root/'spool',
                                    recovery_parent=self.root/'recovery')
        restored = self.guard.status(identity)
        self.assertTrue(restored['recovered'])
        self.assertEqual(len(restored['result']['outputs']),2)
        self.guard.rebuild_bundle(identity)
        state = wait_finished(self.guard,identity)
        self.assertTrue(state['result']['name'].endswith('.zip'))
        with zipfile.ZipFile(self.guard._jobs[identity].bundle.path) as archive:
            self.assertEqual(len([name for name in archive.namelist() if name.endswith('.pdf')]),2)


if __name__ == '__main__':
    unittest.main()
