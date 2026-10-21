"""Run the embedded UNO worker and adapter against controlled native boundaries."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_backend.converter_guard import GuardError
from app_backend.converter_guard.preflight_uno import UNO_WORKER_SOURCE
from app_backend.libreoffice_engine import convert_path
import test_guard_uno_session as worker_contracts
import test_guard_export_recipes_v10 as recipes

W = worker_contracts.W


class UnoPipelineTests(unittest.TestCase):
    def test_worker_does_not_need_a_separate_type_detection_service(self):
        services = []
        def namespace(**kwargs):
            factory = kwargs.get('createInstanceWithContext')
            if factory:
                def create(name, context):
                    services.append(name)
                    if name.endswith('TypeDetection'):
                        raise RuntimeError('No separate detection API')
                    return factory(name, context)
                kwargs['createInstanceWithContext'] = create
            return SimpleNamespace(**kwargs)
        with patch('test_guard_uno_session.SimpleNamespace', side_effect=namespace):
            models, _, exports, replies = worker_contracts.UnoWorkerContracts().execute([{'operation':'inspect'}, {}])
        self.assertTrue(all(reply['ok'] for reply in replies))
        self.assertEqual(len(models), 2)
        self.assertEqual(len(exports), 1)
        inspected = next(reply for reply in replies if reply.get('id') == '0')
        self.assertEqual(inspected['filterName'], 'Actual native import')
        self.assertEqual(inspected['typeName'], 'Actual native type')
        self.assertFalse(any(name.endswith('TypeDetection') for name in services))

    def test_export_gets_its_own_silent_abort_handler_and_next_export_still_runs(self):
        aborted = []
        def export(options):
            self.assertTrue(options['Silent'])
            if not aborted:
                abort = SimpleNamespace(select=lambda: aborted.append(True))
                continuation = SimpleNamespace(queryInterface=lambda _kind: abort)
                options['InteractionHandler'].handle(SimpleNamespace(getContinuations=lambda: [continuation]))
        models, _, exports, replies = worker_contracts.UnoWorkerContracts().execute([{}, {}], on_export=export)
        results = [reply for reply in replies if reply.get('id') != 'ready']
        self.assertEqual(results[0]['code'], 'EXPORT_INTERACTION_REQUIRED')
        self.assertFalse(results[0]['ok'], 'A partial output is not successful after abort')
        self.assertTrue(results[1]['ok'])
        self.assertEqual(aborted, [True])
        self.assertTrue(all(model.closed for model in models))
        self.assertIsNot(exports[0]['InteractionHandler'], exports[1]['InteractionHandler'])

    def test_export_prompt_followed_by_native_exception_has_an_explicit_code(self):
        def export(options):
            options['InteractionHandler'].blocked = True
            raise RuntimeError('Native export refused the interaction')
        models, _, _, replies = worker_contracts.UnoWorkerContracts().execute([{}], on_export=export)
        self.assertEqual(replies[-1]['code'], 'EXPORT_INTERACTION_REQUIRED')
        self.assertEqual(replies[-1]['stage'], 'export')
        self.assertTrue(models[0].closed)

    def test_missing_source_error_does_not_crash_worker_or_poison_next_request(self):
        _, _, exports, replies = worker_contracts.UnoWorkerContracts().execute([
            {'source':str(Path(tempfile.gettempdir())/'aio-v12-nonexistent'/'input.docx')}, {}])
        self.assertEqual(replies[1]['code'], 'SOURCE_IMPORT_FAILED')
        self.assertTrue(replies[2]['ok'])
        self.assertEqual(len(exports), 1)

    def test_one_shot_preflight_loads_once_and_uses_live_services_without_extra_scans(self):
        namespace = {'__name__':'v12_one_shot'}
        exec(compile(UNO_WORKER_SOURCE, 'v12_one_shot', 'exec'), namespace)
        loads, created, closed = [], [], []
        model = SimpleNamespace(supportsService=lambda service:service == W,
            getArgs=lambda:(SimpleNamespace(Name='FilterName',Value='Actual import'),
                            SimpleNamespace(Name='TypeName',Value='Actual type')),
            close=lambda _negotiate:closed.append(True))
        desktop = SimpleNamespace(loadComponentFromURL=lambda *args:loads.append(args) or model)
        resolver = SimpleNamespace(resolve=lambda _url:remote)
        def create(name, _context):
            created.append(name)
            if name.endswith('UnoUrlResolver'):return resolver
            if name.endswith('Desktop'):return desktop
            raise AssertionError('Redundant native service '+name)
        remote = SimpleNamespace(ServiceManager=SimpleNamespace(createInstanceWithContext=create))
        uno=ModuleType('uno');uno.getComponentContext=lambda:remote
        uno.createUnoStruct=lambda _name:SimpleNamespace();uno.getTypeByName=lambda name:name
        helper=ModuleType('unohelper');helper.Base=type('Base',(),{})
        task=ModuleType('com.sun.star.task');task.XInteractionHandler=type('Handler',(),{})
        class Finished(Exception):pass
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);program=root/'program';program.mkdir();(program/'soffice.exe').touch()
            profile=root/'profile';profile.mkdir();source=root/'input.unknown';source.write_bytes(b'original')
            (root/'start.token').touch();manifest=root/'request.json'
            manifest.write_text(json.dumps({'program':str(program),'source':str(source),
                'profile':str(profile),'documentServices':[W,'com.sun.star.sheet.SpreadsheetDocument']}))
            with patch.dict(sys.modules,{'uno':uno,'unohelper':helper,'com.sun.star.task':task}), \
                 patch('subprocess.Popen'),patch('os._exit',side_effect=Finished):
                with self.assertRaises(Finished):namespace['main'](str(manifest))
            result=json.loads((root/'result.json').read_text())
            self.assertEqual(source.read_bytes(),b'original')
        self.assertEqual(len(loads),1)
        self.assertEqual(closed,[True])
        self.assertEqual(result['documentServices'],[W])
        self.assertEqual(result['filterName'],'Actual import')
        self.assertEqual(result['typeName'],'Actual type')
        self.assertTrue(result['modelLoaded'])
        self.assertFalse(any(name.endswith(('TypeDetection','GraphicProvider')) for name in created))

    def test_cli_waits_for_process_exit_without_sleeping_a_full_poll_interval(self):
        waits=[]
        class Process:
            returncode=None
            def poll(self):return self.returncode
            def wait(self,timeout):
                waits.append(timeout);self.returncode=0
        class Tree:
            def __init__(self,command,**kwargs):
                self.process=Process()
                output=Path(command[command.index('--outdir')+1])
                (output/'result.pdf').write_bytes(b'fixture')
                kwargs['on_started']()
            def __enter__(self):return self
            def __exit__(self,*_):pass
        cancel=SimpleNamespace(is_set=lambda:False,wait=Mock(side_effect=AssertionError('Unnecessary full sleep')))
        with tempfile.TemporaryDirectory() as directory, patch('app_backend.libreoffice_engine.ProcessTree',Tree):
            root=Path(directory);source=root/'input.docx';source.write_bytes(b'original')
            result=convert_path(source,'pdf',root/'work',executable=Path(__file__),cancel=cancel)
            self.assertEqual(result[0].read_bytes(),b'fixture')
        self.assertEqual(waits,[.1])


class HealthyWorkerTests(unittest.TestCase):
    setUp = recipes.ExportRecipesQA.setUp
    tearDown = recipes.ExportRecipesQA.tearDown
    def test_completed_native_export_error_keeps_the_healthy_worker(self):
        calls=[]
        def request(value, **_):
            calls.append(value)
            if len(calls)==1:
                return {'ok':False,'code':'ENGINE_FAILED','stage':'export','diagnostic':'fixture'}
            (Path(value['output'])/'result.pdf').write_bytes(b'next-result')
            return {'ok':True}
        office=SimpleNamespace(disabled=False,ensure_started=lambda *args:None,request=request,recycle=Mock())
        self.adapter._local.batch=(self.native,office)
        self.adapter.prepare_source(self.proof)
        with self.assertRaises(GuardError) as error:
            self.adapter.convert(self.source,'pdf',self.root/'first',cancel=threading.Event(),
                                 timeout=3,on_started=lambda:None)
        self.assertEqual(error.exception.code,'ENGINE_FAILED')
        output=self.adapter.convert(self.source,'pdf',self.root/'next',cancel=threading.Event(),
                                    timeout=3,on_started=lambda:None)
        self.assertEqual(output[0].read_bytes(),b'next-result')
        office.recycle.assert_not_called()
        self.assertEqual(len(calls),2)

    def test_transport_failure_recycles_and_cannot_silently_export_again_via_cli(self):
        office=SimpleNamespace(disabled=False,ensure_started=lambda *args:None,
            request=Mock(side_effect=GuardError('UNO_SESSION_UNAVAILABLE','Lost after dispatch',stage='launch')),
            recycle=Mock())
        self.adapter._local.batch=(self.native,office)
        self.adapter.prepare_source(self.proof)
        with patch('app_backend.converter_guard.adapter.convert_path') as cli:
            with self.assertRaises(GuardError) as error:
                self.adapter.convert(self.source,'pdf',self.root/'first',cancel=threading.Event(),
                                     timeout=3,on_started=lambda:None)
        self.assertEqual(error.exception.code,'UNO_SESSION_UNAVAILABLE')
        cli.assert_not_called();office.recycle.assert_called_once()


if __name__ == '__main__':
    unittest.main()
