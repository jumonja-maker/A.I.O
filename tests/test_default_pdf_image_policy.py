"""Ordinary PDF exports must not silently add JPEG loss to source pixels."""
import ast
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from app_backend import libreoffice_engine as engine
from app_backend.converter_guard.export_recipe import native_export_recipe
from app_backend.converter_guard.uno_session import WORKER_SOURCE

PDF_DATA = {'UseLosslessCompression': True, 'ReduceImageResolution': False}
WRITER = 'com.sun.star.text.TextDocument'


class DefaultPdfImagePolicy(unittest.TestCase):
    def recipe(self, target='pdf'):
        name = 'writer_pdf_Export' if target == 'pdf' else 'writer8'
        native = SimpleNamespace(valid=True, fingerprint='native-registry',
            runtime_export_filter=lambda *_: None,
            observed_decision=lambda *_: 'registered',
            export_services=lambda *_: [WRITER],
            export_filter=lambda *_: name,
            filters={name: (WRITER, {target}, {'EXPORT'})},
            uncertain_extensions=set(), primary_extensions={name: target})
        return native_export_recipe(native, target, {'registryFingerprint': native.fingerprint,
                                                     'documentServices': [WRITER]})

    def test_ordinary_pdf_recipe_carries_lossless_images_and_no_downsampling(self):
        recipe = self.recipe()
        self.assertEqual(dict(recipe.filter_data), PDF_DATA)
        self.assertIsNone(recipe.filter_options)
        import json
        extension, name, data = recipe.cli_spec('pdf').split(':', 2)
        self.assertEqual((extension, name), ('pdf', 'writer_pdf_Export'))
        self.assertEqual(json.loads(data), {key: {'type': 'boolean', 'value': value}
                                         for key, value in PDF_DATA.items()})

    def test_other_formats_keep_their_existing_filter_data(self):
        self.assertFalse(self.recipe('odt').filter_data)

    def run_cli(self, target, initial_profile=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root/'source.png'; source.write_bytes(b'original source')
        binary = root/'soffice'; binary.touch()
        workspace = root/'native'
        if initial_profile is not None:
            profile_file = workspace/'profile/user/registrymodifications.xcu'
            profile_file.parent.mkdir(parents=True)
            profile_file.write_text(initial_profile)
        observations = {}

        class Tree:
            def __init__(tree, command, **kwargs):
                profile = Path(unquote(urlsplit(next(v.split('=', 1)[1] for v in command
                    if v.startswith('-env:UserInstallation='))).path))
                # file:///D:/... must become D:/... on Windows.
                import os
                if os.name == 'nt' and profile.as_posix().startswith('/'):
                    profile = Path(profile.as_posix()[1:])
                config = profile/'user/registrymodifications.xcu'
                observations['config'] = config.read_text() if config.exists() else None
                (workspace/'output'/('source.'+target)).write_bytes(
                    b'%PDF-1.7\nfixture\n' if target == 'pdf' else b'native output')
                tree.process = SimpleNamespace(poll=lambda: 0, returncode=0)
            def __enter__(tree): return tree
            def __exit__(tree, *_): return False

        with patch.object(engine, 'ProcessTree', Tree):
            outputs = engine.convert_path(source, target, workspace, executable=binary,
                                           cancel=threading.Event())
        self.assertTrue(outputs[0].exists())
        self.assertEqual(source.read_bytes(), b'original source')
        return observations['config']

    def test_fresh_direct_cli_pdf_profile_uses_the_same_quality_defaults(self):
        text = self.run_cli('pdf')
        self.assertIsNotNone(text)
        root = ET.fromstring(text)
        ns = '{http://openoffice.org/2001/registry}'
        items = [item for item in root if item.get(ns+'path') ==
                 '/org.openoffice.Office.Common/Filter/PDF/Export']
        self.assertEqual(len(items), 1)
        values = {item.get(ns+'name'): item.findtext('value') for item in items[0]}
        self.assertEqual(values, {'UseLosslessCompression': 'true',
                                  'ReduceImageResolution': 'false'})

    def test_direct_cli_does_not_replace_a_prepared_private_profile(self):
        original = ('<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                    '<item oor:path="/private/security"><prop oor:name="keep">'
                    '<value>retained</value></prop></item></oor:items>')
        self.assertEqual(self.run_cli('pdf', original), original)

    def test_non_pdf_cli_does_not_install_pdf_profile_settings(self):
        self.assertIsNone(self.run_cli('custom'))

    def worker_helper(self):
        node = next(item for item in ast.parse(WORKER_SOURCE).body
                    if isinstance(item, ast.FunctionDef) and item.name == 'automatic_export_properties')
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                     '<actual-worker-pdf-helper>', 'exec'), namespace)
        return namespace['automatic_export_properties']

    def test_native_worker_accepts_only_the_vetted_pdf_defaults_without_retry(self):
        helper = self.worker_helper()
        uno = SimpleNamespace(Any=lambda name, values: (name, values))
        properties = helper({'format': 'pdf', 'exportFilter': 'writer_pdf_Export',
                             'exportFilterData': dict(PDF_DATA)},
                            lambda name, value: (name, value), uno)
        self.assertEqual(properties, [('FilterData', ('[]com.sun.star.beans.PropertyValue',
            tuple(PDF_DATA.items())))])

    def test_native_worker_rejects_arbitrary_unrequested_filter_properties(self):
        helper = self.worker_helper()
        for change in ({'exportFilterData': {'Quality': 1}}, {'format': 'png'},
                       {'exportFilter': 'arbitrary_filter'},
                       {'exportFilterData': {'UseLosslessCompression': 1,
                                             'ReduceImageResolution': False}}):
            request = {'format': 'pdf', 'exportFilter': 'writer_pdf_Export',
                       'exportFilterData': dict(PDF_DATA), **change}
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                helper(request, lambda *a: a, SimpleNamespace(Any=lambda *a: a))

    def test_retry_and_non_pdf_worker_paths_remain_compatible(self):
        helper = self.worker_helper()
        args = (lambda *a: a, SimpleNamespace(Any=lambda *a: a))
        self.assertTrue(helper({'format': 'pdf', 'exportFilter': 'writer_pdf_Export',
            'exportFilterData': dict(PDF_DATA), 'automaticRecipe': 'pdf_preserve_images'}, *args))
        self.assertEqual(helper({'format': 'odt', 'exportFilter': 'writer8'}, *args), [])


if __name__ == '__main__': unittest.main()
