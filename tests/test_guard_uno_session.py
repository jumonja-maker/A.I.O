"""Native API contract and stdio ownership; not a Windows native acceptance."""
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.uno_session import UnoSession, WORKER_SOURCE

W='com.sun.star.text.TextDocument'
C='com.sun.star.sheet.SpreadsheetDocument'


class UnoWorkerContracts(unittest.TestCase):
    def execute(self, requests, *, fail_first=False):
        namespace={'__name__':'worker_contract'}
        exec(compile(WORKER_SOURCE,'worker_contract','exec'),namespace)
        models=[];loads=[];exports=[]
        class Model:
            closed=False
            def supportsService(self,service):return service==W
            def getArgs(self):return (SimpleNamespace(Name='FilterName',Value='Actual native import'),)
            def storeToURL(self,url,options):
                exports.append({p.Name:p.Value for p in options})
                from urllib.parse import urlsplit
                from urllib.request import url2pathname
                # file:///D:/... must resolve to D:\\... on Windows.
                Path(url2pathname(urlsplit(url).path)).write_text('native fixture')
            def close(self,negotiate):self.closed=True
        def load(url,frame,flags,options):
            loads.append({p.Name:p.Value for p in options})
            if fail_first and len(loads)==1:return None
            model=Model();models.append(model);return model
        descriptor=(SimpleNamespace(Name='FilterName',Value='Writer import'),)
        desktop=SimpleNamespace(loadComponentFromURL=load)
        detector=SimpleNamespace(queryTypeByDescriptor=lambda descriptor,deep:('writer',globals_descriptor))
        globals_descriptor=descriptor
        resolver=SimpleNamespace(resolve=lambda url:remote)
        manager=SimpleNamespace(createInstanceWithContext=lambda name,context:
            resolver if name.endswith('UnoUrlResolver') else desktop if name.endswith('Desktop') else detector)
        remote=SimpleNamespace(ServiceManager=manager)
        uno=ModuleType('uno');uno.getComponentContext=lambda:remote
        uno.createUnoStruct=lambda name:SimpleNamespace();uno.getTypeByName=lambda name:name
        helper=ModuleType('unohelper');helper.Base=type('Base',(),{})
        task=ModuleType('com.sun.star.task');task.XInteractionHandler=type('Handler',(),{})
        stdout=io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);start=root/'start';start.touch();source=root/'input.docx';source.write_bytes(b'original')
            boot=root/'boot.json';boot.write_text(json.dumps({'program':str(root),'profile':str(root),
                'start':str(start),'pipe':'aio_test'}))
            commands=[]
            for i,options in enumerate(requests):
                output=root/str(i);output.mkdir()
                commands.append({'id':str(i),'operation':'export','source':str(source),'output':str(output),
                    'services':[W,C],'expectedServices':[W],'importFilters':['Writer import'],
                    'format':'pdf','exportFilter':'writer_pdf_Export',**options})
            with patch.dict(sys.modules,{'uno':uno,'unohelper':helper,'com.sun.star.task':task}), \
                    patch('subprocess.Popen'),patch('sys.stdin',io.StringIO('\n'.join(json.dumps(r) for r in commands))), \
                    patch('sys.stdout',stdout):
                namespace['main'](str(boot))
            self.assertEqual(source.read_bytes(),b'original')
        replies=[json.loads(line.removeprefix('AIO_GUARD_RPC ')) for line in stdout.getvalue().splitlines()]
        return models,loads,exports,[r for r in replies if 'ok' in r]

    def test_inspection_loads_and_closes_a_model_without_exporting(self):
        models,loads,exports,replies=self.execute([{'operation':'inspect'}])
        self.assertEqual(exports,[])
        self.assertEqual(replies[-1]['services'],[W])
        self.assertTrue(models[0].closed)
        self.assertEqual(loads[0]['MacroExecutionMode'],0)
        self.assertTrue(loads[0]['ReadOnly']);self.assertTrue(loads[0]['Silent'])
        self.assertNotIn('FilterName',loads[0],'inspection must not force a guessed text importer')
        self.assertEqual(replies[-1]['filterName'],'Actual native import')

    def test_each_export_uses_an_explicit_native_filter_and_a_fresh_model(self):
        models,loads,exports,replies=self.execute([{}, {'format':'html','exportFilter':'HTML (StarWriter)'}])
        self.assertEqual([e['FilterName'] for e in exports],['writer_pdf_Export','HTML (StarWriter)'])
        self.assertTrue(all(e['Overwrite'] is False for e in exports))
        self.assertEqual(len(models),2);self.assertTrue(all(m.closed for m in models))
        self.assertTrue(all(r['ok'] for r in replies))

    def test_changed_model_does_not_export_under_an_old_route_proof(self):
        _,_,exports,replies=self.execute([{'expectedServices':[C]}])
        self.assertEqual(exports,[])
        self.assertEqual(replies[-1]['code'],'NATIVE_MODEL_CHANGED')

    def test_failed_import_does_not_poison_the_next_source(self):
        models,loads,exports,replies=self.execute([{},{}],fail_first=True)
        self.assertEqual(replies[1]['code'],'SOURCE_IMPORT_FAILED')
        self.assertTrue(replies[2]['ok']);self.assertEqual(len(exports),1)


class UnoResponseContracts(unittest.TestCase):
    def session(self):
        session=UnoSession('/unused/soffice.exe','/unused/work',lambda path:None)
        session.tree=SimpleNamespace(process=SimpleNamespace(poll=lambda:None))
        session.reader=SimpleNamespace(is_alive=lambda:True)
        return session

    def test_another_request_cannot_complete_current_task(self):
        session=self.session();session.responses.put({'id':'other','ok':True})
        with self.assertRaises(GuardError) as error:
            session._receive('current',threading.Event(),1)
        self.assertEqual(error.exception.code,'UNO_PROTOCOL')

    def test_stage_messages_are_bound_to_the_same_request(self):
        session=self.session();stages=[]
        for value in ({'id':'a','stage':'import'},{'id':'a','stage':'export'},{'id':'a','ok':True}):
            session.responses.put(value)
        self.assertTrue(session._receive('a',threading.Event(),1,stages.append)['ok'])
        self.assertEqual(stages,['import','export'])

    def test_timeout_and_cancel_do_not_turn_into_a_route_denial(self):
        session=self.session()
        with self.assertRaises(GuardError) as error:
            session._receive('a',threading.Event(),.01)
        self.assertEqual(error.exception.code,'ENGINE_TIMEOUT')
        cancelled=threading.Event();cancelled.set()
        with self.assertRaises(GuardError) as error:session._receive('a',cancelled,1)
        self.assertEqual(error.exception.code,'CANCELLED')

    def test_dead_response_reader_fails_without_waiting_thirty_minutes(self):
        session=self.session();session.reader.is_alive=lambda:False
        with self.assertRaises(GuardError) as error:session._receive('a',threading.Event(),1)
        self.assertEqual(error.exception.code,'UNO_PROTOCOL')
