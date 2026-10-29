"""Disposable candidate finalization; deleted before product publication."""
from pathlib import Path

def edit(name,before,after,count=1):
    path=Path(name);text=path.read_text(encoding='utf-8')
    assert text.count(before)==count,(name,before[:80],text.count(before))
    path.write_text(text.replace(before,after),encoding='utf-8',newline='\n')

edit('app_backend/converter_guard/core.py','    retain_sources: bool = False\n',
     '    retain_sources: bool = False\n    retain_results: bool = False  # This process/UI lifetime, never a source lease.\n')
edit('app_backend/converter_guard/core.py',
     'def create(self, files, formats=None, *, plan=None, prepare_only=False, retain_sources=False):',
     'def create(self, files, formats=None, *, plan=None, prepare_only=False, retain_sources=False, retain_results=False):')
edit('app_backend/converter_guard/core.py','        if type(retain_sources) is not bool:\n',
     "        if type(retain_results) is not bool:\n            raise GuardError('RETENTION_POLICY', 'Некорректный режим хранения результатов.')\n        if type(retain_sources) is not bool:\n")
edit('app_backend/converter_guard/core.py','                       retain_sources=retain_sources)',
     '                       retain_sources=retain_sources, retain_results=retain_results)')
edit('app_backend/converter_guard/core.py','reap_saved and self._saved_all(j) and not j.retain_sources',
     'reap_saved and self._saved_all(j) and not (j.retain_sources or j.retain_results)')
edit('app_backend/converter_guard/http_api.py',"retain_sources=body.get('retainSources', False)))",
     "retain_sources=body.get('retainSources', False),\n                    retain_results=body.get('retainResults', False)))",2)
edit('src/converter-bridge.js',"api('/prepare', {files:metadata(inputs)})",
     "api('/prepare', {files:metadata(inputs),retainResults:true})")
edit('src/converter-bridge.js','Keep the final UI snapshot until explicit Close; saved spools may be reaped.',
     'Keep the visible result session until Close, without retaining originals.')

extra='''
class ResultLifetime(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.guard=ConverterGuard(adapter=FakeAdapter(),recovery_parent=self.root/'jobs',history_parent=self.root/'history')
    def tearDown(self):self.guard.close();self.temp.cleanup()
    def ready(self,retain=True):
        identity=self.guard.create([{'id':'f','name':'input.docx','size':4}],['pdf'],retain_results=retain)['jobId']
        self.guard.upload(identity,'f',io.BytesIO(b'data'),4);self.guard.start(identity,archive_mode='on_demand')
        state=wait_finished(self.guard,identity);self.guard._jobs[identity].future.result(timeout=10)
        return identity,state
    def test_saved_ui_results_remain_until_close_without_originals(self):
        identity,state=self.ready();job=self.guard._jobs[identity]
        self.assertFalse(job.files[0].source.exists())
        self.assertTrue(self.guard.save_folder(state['result']['ticket'],self.root)['saved'])
        self.guard.recoverable(scan=False)
        self.assertEqual(self.guard.status(identity)['succeeded'],1)
        self.assertTrue(job.root.exists())
        with self.guard.open_result(state['result']['ticket']) as (stream,_,__):self.assertEqual(stream.read(),b'data')
        outcome=self.guard.close_conversion(identity)
        self.assertTrue(outcome['closed']);self.assertFalse(outcome['confirmationRequired']);self.assertFalse(job.root.exists())
        self.assertEqual(self.guard.history_list()['items'][0]['savedOutputs'],1)
    def test_ui_result_lifetime_does_not_keep_saved_spool_after_shutdown(self):
        identity,state=self.ready();root=self.guard._jobs[identity].root
        self.guard.save_folder(state['result']['ticket'],self.root)
        self.guard.close();self.assertFalse(root.exists())
    def test_non_ui_saved_jobs_still_reap(self):
        identity,state=self.ready(retain=False);root=self.guard._jobs[identity].root
        self.guard.save_folder(state['result']['ticket'],self.root)
        self.guard.recoverable()
        self.assertNotIn(identity,self.guard._jobs);self.assertFalse(root.exists())
    def test_result_retention_requires_actual_bool(self):
        for value in (1,'true',None):
            with self.subTest(value=value),self.assertRaises(GuardError) as caught:
                self.guard.create([{'id':'f','name':'input.txt','size':0}],['pdf'],retain_results=value)
            self.assertEqual(caught.exception.code,'RETENTION_POLICY')


'''
edit('tests/test_module_retirement.py',"if __name__=='__main__':unittest.main()",extra+"if __name__=='__main__':unittest.main()")
edit('docs/review/AIO_MODULE_RETIREMENT_M16_M17_2026-10-30_RU.md',
     'Служебный исходник нового обычного задания освобождается после обработки всех его целей;',
     'Отдельная опция retainResults удерживает только результаты видимой сессии до «Закрыть»,\nне сохраняя исходник ради старого анализа. После штатного завершения приложения сохранённые\nслужебные результаты освобождаются, несохранённые остаются для Recovery. Эта UI-lease не\nпереносится в журнал следующего запуска; обычная очистка не-UI заданий не отключена.\nСлужебный исходник нового обычного задания освобождается после обработки всех его целей;')
print('RETIREMENT_RESULT_LIFETIME=independent_from_source_retention;NO_TEST_DISABLED')
