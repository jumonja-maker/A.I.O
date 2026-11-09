"""LOK document handles release owned source locks before child process exit."""
import ctypes as C
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard import preflight_native as worker


class LokDocumentCleanup(unittest.TestCase):
    def run_worker(self, *, kind=0, type_failure=False, loaded=True, close_stage_failure=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        program, profile, work = (root/name for name in ('program', 'profile', 'work'))
        for path in (program, profile, work):
            path.mkdir()
        (program/'libsofficeapp.so').write_bytes(bytes(1024))
        source = root/'source.unknown'
        source.write_bytes(b'not a chart, master, or math package')
        closed, loads = [], []
        def load(_kit, url, options):
            loads.append((url, options))
            return 2 if loaded else None
        load_fn = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_char_p, C.c_char_p)(load)
        kind_fn = C.CFUNCTYPE(C.c_int, C.c_void_p)(lambda _doc: kind)
        parts_fn = C.CFUNCTYPE(C.c_int, C.c_void_p)(lambda _doc: 2)
        close_fn = C.CFUNCTYPE(None, C.c_void_p)(lambda doc: closed.append(doc))
        address = lambda fn: C.cast(fn, C.c_void_p).value
        kit = SimpleNamespace(load_options=address(load_fn))
        doc = SimpleNamespace(document_type=address(kind_fn), parts=address(parts_fn), destroy=address(close_fn))
        def table(pointer, _structure, member):
            if pointer == 2 and member == 'document_type' and type_failure:
                raise ValueError('document type ABI unavailable')
            return doc if pointer == 2 else kit
        class Hook:
            def __call__(self, *_args):
                return 1
        native = SimpleNamespace(libreofficekit_hook_2=Hook())
        original_stage = worker._stage
        def stage(folder, name):
            if name == 'document_close' and close_stage_failure:
                raise OSError('close stage disk full')
            original_stage(folder, name)
        with patch.object(worker, 'os', SimpleNamespace(name='posix', fsync=os.fsync)), \
                patch.object(worker.C, 'CDLL', return_value=native), patch.object(worker, '_table', side_effect=table), \
                patch.object(worker, '_stage', side_effect=stage):
            if close_stage_failure:
                with self.assertRaisesRegex(OSError, 'close stage disk full'):
                    worker.detect(program, profile, source, work)
                result = None
            elif type_failure:
                with self.assertRaisesRegex(ValueError, 'document type ABI'):
                    worker.detect(program, profile, source, work)
                result = None
            else:
                result = worker.detect(program, profile, source, work)
        return result, closed, loads, (work/'stage.txt').read_text()

    def test_success_and_draw_parts_release_exactly_the_loaded_document(self):
        for kind in (0, 3):
            with self.subTest(kind=kind):
                result, closed, loads, stage = self.run_worker(kind=kind)
                self.assertEqual(result['kind'], kind)
                if kind == 3:
                    self.assertEqual(result['drawPages'], 2)
                self.assertEqual(closed, [2])
                self.assertEqual(loads[0][1], b'Batch=true')
                self.assertEqual(stage, 'document_closed')

    def test_unclassified_loaded_model_still_releases_its_document(self):
        result, closed, _loads, stage = self.run_worker(kind=4)
        self.assertEqual(result['code'], 'PREFLIGHT_KIND_UNDETERMINED')
        self.assertEqual(closed, [2])
        self.assertEqual(stage, 'document_closed')

    def test_type_inspection_failure_does_not_leak_the_loaded_document(self):
        _result, closed, _loads, stage = self.run_worker(type_failure=True)
        self.assertEqual(closed, [2])
        self.assertEqual(stage, 'document_closed')

    def test_failed_load_does_not_destroy_an_unowned_handle(self):
        result, closed, _loads, stage = self.run_worker(loaded=False)
        self.assertEqual(result['code'], 'PREFLIGHT_IMPORT_UNDETERMINED')
        self.assertEqual(closed, [])
        self.assertEqual(stage, 'document_loaded')

    def test_close_stage_disk_error_still_destroys_the_owned_document(self):
        _result, closed, _loads, stage = self.run_worker(close_stage_failure=True)
        self.assertEqual(closed, [2])
        self.assertEqual(stage, 'document_typed')


if __name__ == '__main__':
    unittest.main()
