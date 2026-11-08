"""Upload staging never deletes or publishes another writer's file."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_backend.converter_guard import ConverterGuard
from app_backend.converter_guard.errors import GuardError


class NoConversion:
    def convert(self, *args, **kwargs):
        raise AssertionError('Upload fixture must not run an export')


class UploadStageOwnership(unittest.TestCase):
    def fixture(self, resource):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        guard = ConverterGuard(adapter=NoConversion(), spool_parent=temporary.name)
        self.addCleanup(guard.close)
        identity = guard.create([{'id': 'f', 'name': 'document.html' if resource else 'document.docx',
                                  'size': 4}], prepare_only=resource)['jobId']
        item = guard._jobs[identity].files[0]
        item.source.parent.mkdir(parents=True, exist_ok=True)
        if resource:
            guard.configure_resources(identity, 'f', [{'path': 'photo.png', 'size': 4}])
            stage = item.source.parent / ('.aio-resource-' + 'a' * 24)
            target = item.source.parent / 'photo.png'
        else:
            stage = item.source.parent.parent / (item.source.parent.name + '.part')
            target = item.source

        def upload(stream=None):
            stream = stream or io.BytesIO(b'data')
            with patch('app_backend.converter_guard.core.secrets.token_hex', return_value='a' * 24):
                if resource:
                    return guard.upload_resource(identity, 'f', 'photo.png', stream, 4)
                return guard.upload(identity, 'f', stream, 4)
        return guard, item, stage, target, upload

    def test_collision_preserves_preexisting_source_and_resource_stage(self):
        for resource in (False, True):
            with self.subTest(resource=resource):
                _guard, item, stage, target, upload = self.fixture(resource)
                stage.write_bytes(b'OTHER_WRITER')
                with self.assertRaises(GuardError) as caught:
                    upload()
                self.assertEqual(caught.exception.code, 'INPUT_STORAGE_FAILED')
                self.assertTrue(stage.exists(), 'Exclusive-create failure removed the existing stage')
                self.assertEqual(stage.read_bytes(), b'OTHER_WRITER')
                self.assertFalse(target.exists())
                self.assertIsNone(item.content_digest)
                if resource:
                    self.assertFalse(item.resources['photo.png']['ready'])

    def test_replacement_before_publication_is_not_published_or_deleted(self):
        original_open = Path.open
        for resource in (False, True):
            with self.subTest(resource=resource):
                _guard, _item, stage, target, upload = self.fixture(resource)
                replaced = []
                class ReplaceAfterClose:
                    def __init__(self, stream):
                        self.stream = stream
                    def __enter__(self):
                        return self.stream.__enter__()
                    def __exit__(self, *args):
                        result = self.stream.__exit__(*args)
                        if not replaced:
                            stage.replace(stage.with_name(stage.name + '.displaced'))
                            stage.write_bytes(b'OTHER_WRITER')
                            replaced.append(True)
                        return result
                def opened(path, mode='r', *args, **kwargs):
                    stream = original_open(path, mode, *args, **kwargs)
                    return ReplaceAfterClose(stream) if path == stage and mode == 'xb' else stream
                with patch.object(Path, 'open', opened):
                    with self.assertRaises(GuardError) as caught:
                        upload()
                self.assertEqual(caught.exception.code, 'INPUT_STORAGE_FAILED')
                self.assertEqual(stage.read_bytes(), b'OTHER_WRITER')
                self.assertFalse(target.exists())

    def test_new_writer_stage_after_successful_rename_is_not_cleaned_up(self):
        original_replace = Path.replace
        for resource in (False, True):
            with self.subTest(resource=resource):
                _guard, _item, stage, target, upload = self.fixture(resource)
                def published(path, destination):
                    result = original_replace(path, destination)
                    if path == stage:
                        stage.write_bytes(b'OTHER_WRITER_AFTER_RENAME')
                    return result
                with patch.object(Path, 'replace', published):
                    upload()
                self.assertEqual(target.read_bytes(), b'data')
                self.assertTrue(stage.exists())
                self.assertEqual(stage.read_bytes(), b'OTHER_WRITER_AFTER_RENAME')

    def test_cancel_cleans_own_stage_and_keeps_input_unpublished(self):
        for resource in (False, True):
            with self.subTest(resource=resource):
                _guard, item, stage, target, upload = self.fixture(resource)
                class CancelOnRead(io.BytesIO):
                    def read(self, size=-1):
                        result = super().read(size)
                        item.cancel.set()
                        return result
                with self.assertRaises(GuardError) as caught:
                    upload(CancelOnRead(b'data'))
                self.assertEqual(caught.exception.code, 'CANCELLED')
                self.assertFalse(stage.exists())
                self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
