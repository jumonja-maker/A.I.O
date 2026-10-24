"""Negotiated bounded status payloads and durable recovery evidence."""
import io
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard, GuardError
from app_backend.converter_guard.core import _Artifact, _LocalHtmlResources, _html_resource_warning
from app_backend.converter_guard.result_store import ResultStore
from test_converter_guard_v02 import FakeAdapter, wait_finished


class ResultDeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.guard = ConverterGuard(adapter=FakeAdapter(), spool_parent=self.temp.name)

    def tearDown(self):
        self.guard.close()
        self.temp.cleanup()

    def converted(self):
        rows = [{'id':str(i), 'name':str(i)+'.docx', 'size':1} for i in range(3)]
        identity = self.guard.create(rows, ['pdf'])['jobId']
        for row in rows:
            self.guard.upload(identity, row['id'], io.BytesIO(b'x'), 1)
        self.guard.start(identity, archive_mode='on_demand')
        wait_finished(self.guard, identity)
        return identity, self.guard._jobs[identity]

    def test_opt_in_delta_retains_identity_saved_updates_and_all_legacy_outputs(self):
        identity, job = self.converted()
        before = self.guard.status(identity)
        item = job.files[1]
        item.targets[0].artifacts[0].saved = True
        item.targets[0].state = 'saved'
        self.guard._record(job, 'native_stage', file_id=item.id, target='pdf')
        change = self.guard.status(identity, delta=True, result_delta=True,
                                   since_revision=before['revision'])
        self.assertTrue(change['delta'])
        self.assertTrue(change['resultOutputsDelta'])
        self.assertEqual(len(change['result']['outputs']), 1)
        updated = change['result']['outputs'][0]
        self.assertTrue(updated['saved'])
        self.assertEqual(updated['ticket'], before['result']['outputs'][1]['ticket'])
        merged = {a['ticket']:a for a in before['result']['outputs']}
        merged.update({a['ticket']:a for a in change['result']['outputs']})
        self.assertEqual(list(merged.values()), self.guard.status(identity)['result']['outputs'])
        legacy = self.guard.status(identity, delta=True, since_revision=before['revision'])
        self.assertNotIn('resultOutputsDelta', legacy)
        self.assertEqual(len(legacy['result']['outputs']), 3)

    def test_packaging_only_delta_does_not_walk_retained_inventory(self):
        identity, job = self.converted()
        before = self.guard.status(identity)
        self.guard._record(job, 'packaging_started')
        with patch.object(_Artifact, 'metadata', autospec=True, wraps=_Artifact.metadata) as metadata:
            # No file changed: only partialResult needs its single anchor.
            self.guard.status(identity, delta=True, result_delta=True,
                              since_revision=before['revision'])
            self.assertEqual(metadata.call_count, 1)
        delta = self.guard.status(identity, delta=True, result_delta=True,
                                  since_revision=before['revision'])
        self.assertEqual(delta['result']['outputs'], [])
        self.assertTrue(delta['resultOutputsDelta'])

    def test_no_result_stays_null_and_full_resync_has_the_complete_inventory(self):
        identity = self.guard.create([{'id':'f', 'name':'one.docx', 'size':0}], ['pdf'])['jobId']
        job = self.guard._jobs[identity]
        revision = job.revision
        self.guard._record(job, 'native_stage', file_id='f', target='pdf')
        delta = self.guard.status(identity, delta=True, result_delta=True, since_revision=revision)
        self.assertIsNone(delta['result'])
        self.assertNotIn('resultOutputsDelta', delta)
        identity, job = self.converted()
        full = self.guard.status(identity, result_delta=True)
        self.assertNotIn('resultOutputsDelta', full)
        self.assertEqual(len(full['result']['outputs']), 3)
        self.guard._record(job, 'save_confirmed')  # wildcard must force full inventory
        resync = self.guard.status(identity, delta=True, result_delta=True,
                                   since_revision=full['revision'])
        self.assertNotIn('resultOutputsDelta', resync)
        self.assertEqual(len(resync['result']['outputs']), 3)

    def test_changed_file_returns_every_companion_and_unknown_mode_rejected(self):
        identity, job = self.converted()
        before = self.guard.status(identity)
        item = job.files[0]
        companion = _Artifact(identity, job.root/'fixture.png', 'fixture.png', 7,
                              'stable-companion', item.id, 'pdf', 'images/fixture.png')
        item.targets[0].artifacts.append(companion)
        job.artifacts.append(companion)
        self.guard._record(job, 'result_ready', file_id=item.id, target='pdf')
        delta = self.guard.status(identity, delta=True, result_delta=True,
                                  since_revision=before['revision'])
        self.assertEqual({a['ticket'] for a in delta['result']['outputs']},
                         {a.ticket for a in item.targets[0].artifacts})
        self.assertEqual(delta['partialResult']['outputsCount'], 4)
        with self.assertRaises(GuardError):
            self.guard.status(identity, result_delta=1)

    def test_first_result_creation_supplies_inventory_when_prior_result_was_null(self):
        identity, job = self.converted()
        job.result = None
        before = self.guard.status(identity)
        self.assertIsNone(before['result'])
        self.guard._make_result(job)
        first = self.guard.status(identity, delta=True, result_delta=True,
                                  since_revision=before['revision'])
        self.assertNotIn('resultOutputsDelta', first)
        self.assertEqual(len(first['result']['outputs']), 3)

    def test_save_publication_race_does_not_overwrite_the_new_destination(self):
        identity, job = self.converted()
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        destination = Path(external.name)
        original = os.rename if os.name == 'nt' else os.link
        collided = []

        def create_competing_file(source, target, *args, **kwargs):
            Path(target).write_bytes(b'external file created after exists check')
            collided.append(Path(target))
            return original(source, target, *args, **kwargs)

        primitive = 'app_backend.converter_guard.core.os.' + ('rename' if os.name == 'nt' else 'link')
        with patch(primitive, side_effect=create_competing_file), self.assertRaises(GuardError) as error:
            self.guard.save_folder(job.artifacts[0].ticket, destination)
        self.assertEqual(error.exception.code, 'SAVE_CHANGED')
        self.assertEqual(len(collided), 1)
        self.assertEqual(collided[0].read_bytes(), b'external file created after exists check')
        self.assertTrue(all(not a.saved and a.path.is_file() for a in job.artifacts))
        self.assertFalse(list(destination.rglob('*.aio-part-*')))

    @unittest.skipIf(os.name == 'nt', 'POSIX link-unsupported fallback')
    def test_unsupported_hardlinks_use_exclusive_copy_and_independent_readback(self):
        identity, job = self.converted()
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        with patch('app_backend.converter_guard.core.os.link',
                   side_effect=OSError(errno.EOPNOTSUPP, 'fixture unsupported links')):
            outcome = self.guard.save_folder(job.artifacts[0].ticket, external.name)
        self.assertTrue(outcome['readbackVerified'])
        self.assertTrue(outcome['savedAll'])
        folder = Path(external.name)/outcome['name']
        for artifact in job.artifacts:
            self.assertEqual((folder/artifact.archive_name).read_bytes(), artifact.path.read_bytes())

    @unittest.skipIf(os.name == 'nt', 'POSIX link-unsupported fallback')
    def test_unsupported_link_fallback_still_rejects_a_competing_target(self):
        identity, job = self.converted()
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        collided = []

        def unsupported_with_competitor(source, target, **kwargs):
            Path(target).write_bytes(b'external file before exclusive fallback')
            collided.append(Path(target))
            raise OSError(errno.EOPNOTSUPP, 'fixture unsupported links')

        with patch('app_backend.converter_guard.core.os.link', side_effect=unsupported_with_competitor), \
                self.assertRaises(GuardError) as error:
            self.guard.save_folder(job.artifacts[0].ticket, external.name)
        self.assertEqual(error.exception.code, 'SAVE_CHANGED')
        self.assertEqual(collided[0].read_bytes(), b'external file before exclusive fallback')
        self.assertTrue(all(not a.saved and a.path.is_file() for a in job.artifacts))


class RecoveryTransactionTests(unittest.TestCase):
    def test_new_transaction_does_not_erase_an_earlier_missing_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(Path(directory))
            header = {'schema':1, 'jobId':'a'*32, 'files':[]}
            header['checksum'] = store._digest(header)
            (Path(directory)/'job.json').write_text(json.dumps(header), encoding='utf-8')
            store.append_many([
                {'kind':'task_begin', 'transaction':'lost', 'index':0, 'state':'ready'},
                {'kind':'task_begin', 'transaction':'complete', 'index':1, 'state':'ready'},
                {'kind':'task_commit', 'transaction':'complete'},
            ])
            _, records, _, integrity = store.read()
            self.assertEqual(integrity, 'incomplete_transaction')
            self.assertNotIn(0, records)
            self.assertEqual(records[1]['state'], 'ready')


class HtmlResourceNamesTests(unittest.TestCase):
    def test_existing_and_missing_html_attribute_names_keep_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.html'
            (root/'logo file.png').write_bytes(b'fixture')
            for attr in ('src', 'href', 'background'):
                source.write_text('<img '+attr+'="logo file.png">', encoding='utf-8')
                self.assertIsNone(_html_resource_warning(source))
                source.write_text('<img '+attr+'="missing file.png">', encoding='utf-8')
                missing = []
                self.assertEqual(_html_resource_warning(source, missing=missing),
                                 'HTML_LOCAL_RESOURCES_NOT_UPLOADED')
                self.assertEqual(missing, ['missing file.png'])

    def test_quoted_css_urls_and_imports_keep_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.html'
            (root/'logo file.png').write_bytes(b'fixture')
            (root/'extra style.css').write_text('body { color: black; }', encoding='utf-8')
            for css in ('body {background: url("logo file.png")}',
                        "body {background: url('logo file.png')}",
                        r'body {background: url(logo\ file.png)}',
                        r'body {background: url(logo\20 file.png)}',
                        '@import "extra style.css";'):
                source.write_text('<style>'+css+'</style>', encoding='utf-8')
                self.assertIsNone(_html_resource_warning(source))

    def test_split_css_token_is_not_reported_as_a_truncated_missing_url(self):
        seen = []
        parser = _LocalHtmlResources(lambda value: seen.append(value) or True)
        parser.feed('<style>body{background:url("logo ')
        self.assertEqual(seen, [])
        parser.feed('file.png")}</style>')
        self.assertEqual(seen, ['logo file.png'])


if __name__ == '__main__':
    unittest.main()
