"""Finite automatic export recipes, applied through real CLI/UNO boundaries."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard.adapter import LibreOfficeAdapter
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.export_recipe import (
    automatic_recipe, automatic_recipe_catalogue, validate_retry_recipe,
    native_export_recipe, CALC_TEXT)
from app_backend.converter_guard.routes import Registry
from app_backend.converter_guard.uno_session import WORKER_SOURCE
from test_converter_guard_v04 import component, node, C, W
import test_guard_uno_session as worker_contracts


class AutomaticRecipes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root/'soffice'; self.exe.touch()
        path = self.root/'registry.xcd'
        types = node('text', Extensions='csv tsv tab') + node('pdf', Extensions='pdf')
        filters = (node(CALC_TEXT, Type='text', DocumentService=C, Flags='IMPORT EXPORT') +
                   node('calc_pdf_Export', Type='pdf', DocumentService=C, Flags='EXPORT') +
                   node('writer_pdf_Export', Type='pdf', DocumentService=W, Flags='EXPORT'))
        path.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">'+
            component('Types', 'Types', types)+component('Filter', 'Filters', filters)+'</oor:data>')
        self.native = Registry(((str(path), 0, path.stat().st_size),), self.exe)
        self.proof = {'state':'detected', 'basis':'native_preflight_document_kind',
            'registryFingerprint':self.native.fingerprint, 'documentServices':[C]}
        self.adapter = LibreOfficeAdapter(self.exe)
        self.registry_patch = patch.object(self.adapter, '_registry', return_value=self.native)
        self.registry_patch.start(); self.addCleanup(self.registry_patch.stop)
        self.source = self.root/'source.ods'; self.source.write_bytes(b'fixture')

    def test_catalogue_is_finite_and_caller_cannot_mutate_internal_recipe(self):
        first = automatic_recipe_catalogue()
        self.assertEqual({r['id'] for r in first}, {'pdf_preserve_images', 'csv_preserve_values'})
        first[0]['parameters']['UseLosslessCompression'] = False
        self.assertTrue(validate_retry_recipe('pdf_preserve_images', 'pdf')['parameters']['UseLosslessCompression'])
        for identity, target in [('unknown', 'pdf'), ('pdf_preserve_images', 'csv'),
                                 ({'UseLosslessCompression':True}, 'pdf')]:
            with self.subTest(identity=identity, target=target), self.assertRaises(GuardError):
                validate_retry_recipe(identity, target)

    def test_pdf_uses_typed_cli_properties_and_does_not_change_native_default(self):
        recipe = automatic_recipe('pdf_preserve_images', 'pdf', self.native, self.proof)
        target, name, encoded = recipe.cli_spec('pdf').split(':', 2)
        self.assertEqual((target, name), ('pdf', 'calc_pdf_Export'))
        self.assertEqual(json.loads(encoded), {
            'UseLosslessCompression': {'type':'boolean', 'value':True},
            'ReduceImageResolution': {'type':'boolean', 'value':False}})
        self.assertEqual(native_export_recipe(self.native, 'pdf', self.proof).cli_spec('pdf'),
                         'pdf:calc_pdf_Export')

    def test_csv_preserves_raw_numeric_precision_utf8_and_all_sheets(self):
        for target, separator in [('csv', '44'), ('tsv', '9'), ('tab', '9')]:
            recipe = automatic_recipe('csv_preserve_values', target, self.native, self.proof)
            tokens = recipe.filter_options.split(',')
            self.assertEqual(tokens[0:3], [separator, '34', '76'])
            self.assertEqual(tokens[8:10], ['false', 'false'])
            self.assertEqual(tokens[11], '-1')
            self.assertEqual(tokens[13], 'true')
            self.assertIn('AUTOMATIC_CSV_RAW_VALUES', recipe.warnings)

    def test_stale_unregistered_and_wrong_model_never_get_silent_fallback(self):
        for proof in ({}, {**self.proof, 'registryFingerprint':'stale'},
                      {**self.proof, 'documentServices':[W]}):
            with self.subTest(proof=proof), self.assertRaises(GuardError):
                automatic_recipe('csv_preserve_values', 'csv', self.native, proof)
        self.assertEqual(self.adapter.validate_retry_recipe('pdf_preserve_images', 'pdf', self.proof)['filter'],
                         'calc_pdf_Export')

    def test_cli_task_scope_clears_recipe_and_export_provenance(self):
        self.adapter.prepare_source(self.proof)
        specs = []
        with patch('app_backend.converter_guard.adapter.convert_path',
                   side_effect=lambda *a, **k: specs.append(k['convert_spec']) or []):
            self.adapter.prepare_retry_recipe('pdf_preserve_images')
            self.adapter._convert_native(self.source, 'pdf', self.root/'retry', cancel=threading.Event(),
                                         timeout=None, on_started=lambda:None)
            self.assertEqual(self.adapter.export_info()['automaticRecipe'], 'pdf_preserve_images')
            self.adapter.prepare_retry_recipe(None)
            self.adapter._convert_native(self.source, 'pdf', self.root/'normal', cancel=threading.Event(),
                                         timeout=None, on_started=lambda:None)
            self.assertNotIn('automaticRecipe', self.adapter.export_info())
        self.assertIn('UseLosslessCompression', specs[0])
        self.assertEqual(specs[1], 'pdf:calc_pdf_Export')

    def test_uno_request_and_cli_have_the_same_internal_options(self):
        requests = []
        def request(value, **kwargs):
            requests.append(value)
            output = Path(value['output'])/(self.source.stem+'.pdf')
            output.write_bytes(b'%PDF-1.7\n% synthetic contract')
            return {'ok':True}
        office = SimpleNamespace(disabled=False, ensure_started=lambda *a:None, request=request)
        self.adapter._local.batch = self.native, office
        self.adapter.prepare_source(self.proof)
        self.adapter.prepare_retry_recipe('pdf_preserve_images')
        self.adapter._convert_native(self.source, 'pdf', self.root/'uno', cancel=threading.Event(),
                                     timeout=None, on_started=lambda:None)
        self.assertEqual(requests[0]['exportFilterData'],
                         {'UseLosslessCompression':True, 'ReduceImageResolution':False})
        self.assertIsNone(requests[0]['exportOptions'])
        self.assertEqual(requests[0]['automaticRecipe'], 'pdf_preserve_images')

    def test_embedded_worker_applies_properties_only_to_selected_fresh_model(self):
        models, imports, exports, replies = worker_contracts.UnoWorkerContracts().execute([
            {'automaticRecipe':'pdf_preserve_images', 'exportFilterData':
             {'UseLosslessCompression':True, 'ReduceImageResolution':False}}, {}])
        self.assertTrue(all(row['ok'] for row in replies))
        self.assertEqual({p.Name:p.Value for p in exports[0]['FilterData']},
                         {'UseLosslessCompression':True, 'ReduceImageResolution':False})
        self.assertNotIn('FilterData', exports[1])
        self.assertTrue(all('FilterData' not in row and row['MacroExecutionMode']==0 for row in imports))
        self.assertEqual(len(models), 2)
        self.assertTrue(all(model.closed for model in models))

    def test_worker_rejects_arbitrary_properties_and_next_task_still_runs(self):
        for data in ({'UseLosslessCompression':True, 'ReduceImageResolution':False, 'EncryptFile':True},
                     {'UseLosslessCompression':1, 'ReduceImageResolution':False}):
            _, _, exports, replies = worker_contracts.UnoWorkerContracts().execute([
                {'automaticRecipe':'pdf_preserve_images', 'exportFilterData':data}, {}])
            results = [r for r in replies if r['id']!='ready']
            self.assertFalse(results[0]['ok'])
            self.assertIn('AUTOMATIC_FILTER_DATA_INVALID', results[0]['diagnostic'])
            self.assertTrue(results[1]['ok'])
            self.assertEqual(len(exports), 1)

    def test_csv_worker_validates_the_exact_catalogue_string(self):
        namespace = {'__name__':'recipe_test'}
        exec(compile(WORKER_SOURCE, 'worker-recipe', 'exec'), namespace)
        recipe = automatic_recipe('csv_preserve_values', 'csv', self.native, self.proof)
        request = {'automaticRecipe':'csv_preserve_values', 'format':'csv',
                   'exportFilter':CALC_TEXT, 'exportOptions':recipe.filter_options}
        self.assertEqual(namespace['automatic_export_properties'](request, lambda n,v:(n,v)), [])
        for changed in ({'format':'pdf'}, {'exportFilter':'writer_pdf_Export'},
                        {'exportOptions':recipe.filter_options+',arbitrary'}):
            with self.assertRaises(RuntimeError):
                namespace['automatic_export_properties']({**request, **changed}, lambda n,v:(n,v))

    def test_csv_worker_discovers_every_sheet_and_rejects_missing_or_empty_inventory(self):
        namespace = {'__name__':'recipe_test'}
        exec(compile(WORKER_SOURCE, 'worker-recipe', 'exec'), namespace)
        output = self.root/'csv-files'; output.mkdir()
        model = SimpleNamespace(getSheets=lambda:SimpleNamespace(getCount=lambda:2))
        request = {'output':str(output), 'format':'csv'}
        with self.assertRaisesRegex(RuntimeError, 'AUTOMATIC_CSV_SHEETS_MISSING'):
            namespace['automatic_csv_outputs'](request, model)
        first = output/'source-Лист1.csv'; first.write_text('00123,1.23456789')
        with self.assertRaisesRegex(RuntimeError, 'AUTOMATIC_CSV_SHEETS_MISSING'):
            namespace['automatic_csv_outputs'](request, model)
        second = output/'source-Лист2.csv'; second.write_text('other,values')
        self.assertEqual(set(namespace['automatic_csv_outputs'](request, model)), {first, second})

    def test_cancel_does_not_dispatch_native_retry(self):
        self.adapter.prepare_source(self.proof)
        self.adapter.prepare_retry_recipe('pdf_preserve_images')
        cancel = threading.Event(); cancel.set()
        with patch('app_backend.converter_guard.adapter.convert_path') as dispatch:
            with self.assertRaises(GuardError) as error:
                self.adapter._convert_native(self.source, 'pdf', self.root/'cancel', cancel=cancel,
                                             timeout=None, on_started=lambda:None)
            self.assertEqual(error.exception.code, 'CANCELLED')
            dispatch.assert_not_called()
