"""Actual native recipes and ownership regressions; not Windows acceptance."""
import io
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import test_guard_uno_session as worker_contracts

from app_backend.converter_guard import ConverterGuard
from app_backend.converter_guard.export_recipe import native_export_recipe, DRAW
from app_backend.converter_guard.routes import Registry
from test_converter_guard_v04 import component, node, W
from test_converter_guard_v02 import FakeAdapter, wait_finished
from app_backend.converter_guard.preflight_native import _mathml_root


class DrawRecipes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'registry.xcd'
        types = (node('html', Extensions='html htm') + node('xhtml', Extensions='xhtml'))
        filters = (node('draw_html_Export', Type='html', DocumentService=DRAW, Flags='EXPORT') +
                   node('XHTML Draw File', Type='xhtml', DocumentService=DRAW, Flags='EXPORT') +
                   node('Draw import', Type='html', DocumentService=DRAW, Flags='IMPORT') +
                   node('HTML (StarWriter)', Type='html', DocumentService=W, Flags='IMPORT EXPORT'))
        path.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">' +
                        component('Types', 'Types', types) + component('Filter', 'Filters', filters) + '</oor:data>')
        self.native = Registry(((str(path), 0, path.stat().st_size),), path)
        self.proof = {'state':'detected','basis':'native_preflight_uno_document',
                      'modelLoaded':True,'documentServices':[DRAW], 'drawPages':1,
                      'registryFingerprint':self.native.fingerprint}

    def test_one_native_draw_page_uses_installed_content_preserving_filter(self):
        for target in ('html','htm'):
            with self.subTest(target=target):
                recipe = native_export_recipe(self.native,target,self.proof)
                self.assertEqual(recipe.filter_name,'XHTML Draw File')
                self.assertEqual(recipe.required_draw_pages,1)
                self.assertEqual(recipe.cli_spec(target),target+':XHTML Draw File')

    def test_unknown_or_multiple_pages_keep_native_default_with_explicit_risk(self):
        for pages in (None,0,2,-1,True,'1'):
            with self.subTest(pages=pages):
                recipe=native_export_recipe(self.native,'html',{**self.proof,'drawPages':pages})
                self.assertEqual(recipe.filter_name,'draw_html_Export')
                self.assertEqual(recipe.warnings,('NATIVE_DRAW_HTML_CONTENT_RISK',))
                self.assertIsNone(recipe.required_draw_pages)

    def test_missing_ambiguous_or_overlay_xhtml_filter_is_not_forced(self):
        candidates=self.native.export_candidates['xhtml',DRAW]
        candidates.add('ambiguous')
        self.assertEqual(native_export_recipe(self.native,'html',self.proof).filter_name,'draw_html_Export')
        candidates.clear()
        self.assertEqual(native_export_recipe(self.native,'html',self.proof).filter_name,'draw_html_Export')
        candidates.add('XHTML Draw File');self.native.uncertain_extensions.add('xhtml')
        self.assertEqual(native_export_recipe(self.native,'html',self.proof).filter_name,'draw_html_Export')

    def test_writer_and_stale_native_proofs_do_not_use_draw_recipe(self):
        recipe=native_export_recipe(self.native,'html',{**self.proof,'documentServices':[W]})
        self.assertEqual(recipe.filter_name,'HTML (StarWriter)')
        self.assertEqual(recipe.warnings,())
        self.assertIsNone(native_export_recipe(self.native,'html',{**self.proof,'registryFingerprint':'stale'}))



class WriterSvgRecipe(unittest.TestCase):
    def test_selection_only_writer_svg_is_unavailable_for_whole_document(self):
        # LibreOffice's filterWriterOrCalc explicitly requires SelectionOnly.
        # Selecting a graphic would silently drop the rest of this document.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'registry.xcd'
            types = node('svg', Extensions='svg')
            filters = (node('writer_svg_Export', Type='svg', DocumentService=W, Flags='EXPORT') +
                       node('writer_test_import', Type='svg', DocumentService=W, Flags='IMPORT'))
            path.write_text(
                '<oor:data xmlns:oor="http://openoffice.org/2001/registry">' +
                component('Types', 'Types', types) +
                component('Filter', 'Filters', filters) + '</oor:data>')
            native = Registry(((str(path), 0, path.stat().st_size),), path)
            proof = {'state': 'detected', 'basis': 'native_preflight_uno_document',
                     'modelLoaded': True, 'documentServices': [W],
                     'registryFingerprint': native.fingerprint}
            recipe = native_export_recipe(native, 'svg', proof)
            self.assertIsNone(recipe)
            self.assertEqual(native.observed_decision('svg', proof), 'unavailable')
            self.assertEqual(native.route_reason('svg', proof), 'NATIVE_SVG_SELECTION_ONLY')
            self.assertIsNone(native_export_recipe(
                native, 'svg', {**proof, 'registryFingerprint': 'wrong'}))
            # Draw/Impress SVG is covered separately and remains available.




class DrawWorker(unittest.TestCase):
    def test_page_count_is_read_from_loaded_model_and_mismatch_never_exports(self):
        with patch.object(worker_contracts,'W',DRAW):
            models,loads,exports,replies=worker_contracts.UnoWorkerContracts().execute([
                {'operation':'inspect'}, {'requiredDrawPages':1}, {}],draw_pages=2)
        self.assertEqual(replies[1]['drawPages'],2)
        self.assertEqual(replies[2]['code'],'NATIVE_MODEL_CHANGED')
        self.assertTrue(replies[3]['ok'])
        self.assertEqual(len(exports),1)
        self.assertTrue(all(model.closed for model in models))


class FormulaContainers(unittest.TestCase):
    def test_formula_identity_uses_package_mimetype_and_mathml_root(self):
        with tempfile.TemporaryDirectory() as directory:
            for number,(mime,content,expected) in enumerate([
                (b'application/vnd.oasis.opendocument.formula',b'<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>',True),
                (b'application/vnd.oasis.opendocument.chart',b'<math xmlns="http://www.w3.org/1998/Math/MathML"/>',False),
                (b'application/vnd.oasis.opendocument.formula',b'<document><math/></document>',False),
                (b'x'*1024,b'<math xmlns="http://www.w3.org/1998/Math/MathML"/>',False)]):
                path=Path(directory)/(str(number)+'.unlisted')
                with zipfile.ZipFile(path,'w') as archive:
                    archive.writestr('mimetype',mime);archive.writestr('content.xml',content)
                self.assertEqual(_mathml_root(path),expected)


class SaveAndRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.guards=[]
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda:[g.close() for g in self.guards])

    def guard(self,adapter=None):
        g=ConverterGuard(adapter=adapter or FakeAdapter(),recovery_parent=self.root/'recovery')
        self.guards.append(g);return g

    def prepared(self,g):
        identity=g.create([{'id':'f','name':'renamed.unknown','size':1}],['html'])['jobId']
        g.upload(identity,'f',io.BytesIO(b'x'),1);g.start(identity,archive_mode='on_demand')
        return identity,wait_finished(g,identity)

    def test_detached_progress_observer_does_not_interrupt_copy_or_acknowledgement(self):
        g=self.guard();identity,state=self.prepared(g)
        destination=self.root/'save';destination.mkdir()
        def gone(*_):raise RuntimeError('GUI observer was detached')
        saved=g.save_folder(state['result']['ticket'],destination,on_progress=gone)
        self.assertTrue(saved['savedAll']);self.assertEqual(saved['outputs'],2)
        self.assertEqual(len(list(destination.rglob('*.png'))),1)
        self.assertEqual(g.status(identity)['files'][0]['targets'][0]['state'],'saved')

    def test_native_export_filter_and_risk_survive_a_real_journal_restart(self):
        class RiskAdapter(FakeAdapter):
            def export_info(self):
                return {'filter':'draw_html_Export','warnings':('NATIVE_DRAW_HTML_CONTENT_RISK',)}
        g=self.guard(RiskAdapter());identity,state=self.prepared(g)
        target=state['files'][0]['targets'][0]
        self.assertEqual(target['exportFilter'],'draw_html_Export')
        self.assertEqual(target['warnings'][0]['code'],'NATIVE_DRAW_HTML_CONTENT_RISK')
        g.close();restored=self.guard().status(identity)['files'][0]['targets'][0]
        self.assertEqual(restored['exportFilter'],target['exportFilter'])
        self.assertEqual(restored['warnings'],target['warnings'])
        self.assertEqual(len(restored['outputs']),2)

    def test_malformed_optional_export_diagnostics_cannot_lose_good_outputs(self):
        for info in (None,{'filter':123,'warnings':None},{'filter':'x'*1000,'warnings':[['invalid']]},
                     {'sheetOutputs':None}, {'sheetOutputs':[None, {'file':'x'}]}):
            with self.subTest(info=info):
                class DiagnosticAdapter(FakeAdapter):
                    def export_info(self):return info
                guard=self.guard(DiagnosticAdapter());identity,state=self.prepared(guard)
                self.assertEqual((state['succeeded'],state['failed']),(1,0))
                target=state['files'][0]['targets'][0]
                self.assertEqual(len(target['outputs']),2)
                self.assertIsNone(target['exportFilter']);self.assertEqual(target['warnings'],[])


if __name__=='__main__':unittest.main()
