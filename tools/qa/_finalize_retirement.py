"""Disposable candidate finalization; deleted before product publication."""
import ast
from pathlib import Path
import subprocess

BASE='ad3492dd701082cf1d961a68d0d99de6a08457dd'
def edit(name,before,after,count=1):
    path=Path(name);text=path.read_text(encoding='utf-8')
    assert text.count(before)==count,(name,before[:80],text.count(before))
    path.write_text(text.replace(before,after),encoding='utf-8',newline='\n')
def remove_methods(path,names):
    p=Path(path);text=p.read_text(encoding='utf-8');lines=text.splitlines(keepends=True)
    found=[n for n in ast.walk(ast.parse(text)) if isinstance(n,ast.FunctionDef) and n.name in names]
    assert {n.name for n in found}==set(names)
    p.write_text(''.join(line for i,line in enumerate(lines,1) if not any(n.lineno<=i<=n.end_lineno for n in found)),encoding='utf-8',newline='\n')

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

# Metadata bodies use the same unbuffered loopback socket as file uploads.
# Socket shutdown alone did not wake the old blocking read on Windows.
edit('app_backend/converter_guard/http_api.py',
'''            raw = bytearray()
            while len(raw) < length:
                block = handler.rfile.read(min(CHUNK, length - len(raw)))
                if not block:
                    break
                raw.extend(block)''',
'''            raw = bytearray()
            deadline = time.monotonic() + self.guard.limits.upload_idle_timeout
            while len(raw) < length:
                with self._lock:
                    if self._closed:
                        raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GuardError('REQUEST_TIMEOUT', 'Передача запроса остановилась.', status=408)
                ready, _, _ = select.select([handler.connection], [], [], min(0.1, remaining))
                if not ready:
                    continue
                block = handler.rfile.read(min(CHUNK, length - len(raw)))
                if not block:
                    break
                raw.extend(block)
                deadline = time.monotonic() + self.guard.limits.upload_idle_timeout''')
edit('app_backend/converter_guard/http_api.py',
'''        with self._lock:
            self._connections.add(handler.connection)''',
'''        with self._lock:
            if self._closed:
                raise GuardError('GUARD_CLOSED', 'Guard завершает работу.', status=503)
            self._connections.add(handler.connection)''')

# These two complete files exclusively test the removed preview/observer.
for name in ('tests/test_frozen_preview_budget_contract.py','tests/test_guard_qt_observation_v13.py'):
    Path(name).unlink()
# Retain all six common lifecycle/save tests in this mixed test module.
edit('tests/test_session_lifecycle_b16_b27.py',
     'from app_backend.converter_guard.diagnostic_export import diagnostic_package\n','')
remove_methods('tests/test_session_lifecycle_b16_b27.py',{
    'test_selected_diagnostic_never_saves_or_adds_required_outputs',
    'test_failed_without_output_exports_report_only_unless_source_explicitly_chosen',
    'test_diagnostic_rejects_other_task_and_changed_or_symlinked_output'})

# Restore the mixed stress driver; preserve native controls, evidence validation
# and input identity. Only comparative/parser-preview stages move to archive.
name='tools/qa/guard_remaining_bag_stress3.py'
p=Path(name);p.write_bytes(subprocess.check_output(['git','show',BASE+':'+name]))
s=p.read_text(encoding='utf-8')
s=''.join(line for line in s.splitlines(keepends=True) if not any(token in line for token in
    ("('session','guard_session_native_acceptance.py'","('viewer-100'","('viewer-150'")))
start=s.index('SAFETY_DECISIONS={');end=s.index('\n\n\ndef digest',start)
s=s[:start]+"SAFETY_DECISIONS={'conflicting-mime':'invalid','renamed-writer':'recognized','inspection-budget':'not_checked'}"+s[end:]
start=s.index('def safety_negative_corpus(');end=s.index('\ndef native_positive_corpus(',start)
s=s[:start]+'''def safety_negative_corpus(folder):
    """Retained bounded source declarations; comparative controls are archived."""
    from app_backend.converter_guard.source_contract import inspect_source_contract
    folder.mkdir();records=[]
    office='urn:oasis:names:tc:opendocument:xmlns:office:1.0'
    text='urn:oasis:names:tc:opendocument:xmlns:text:1.0'
    writer=(f'<office:document xmlns:office="{office}" xmlns:text="{text}" '
        'office:mimetype="application/vnd.oasis.opendocument.text"><office:body>'
        '<office:text><text:p>ALPHA 001 OMEGA</text:p></office:text></office:body></office:document>')
    controls={
        'conflicting-mime':('conflicting.fods',writer.replace('opendocument.text','opendocument.spreadsheet').encode()),
        'renamed-writer':('writer-renamed.xlsx',writer.encode()),
        'inspection-budget':('bounded.fodt',writer.encode()),
    }
    for name,(filename,raw) in controls.items():
        source=folder/filename;source.write_bytes(raw);before=digest(source)
        outcome=inspect_source_contract(source,**({'budget':8} if name=='inspection-budget' else {}))
        actual=outcome['state']
        if actual!=SAFETY_DECISIONS[name]:raise ValueError(name+': unexpected decision '+actual)
        if digest(source)!=before:raise ValueError(name+': parser changed original')
        records.append({'name':name,'file':filename,'sha256':before,'status':'PASS',
            'expectedDecision':SAFETY_DECISIONS[name],'actualDecision':actual,
            'scope':'bounded_source_declaration','evidence':outcome})
    write(folder/'manifest.json',{'status':'PASS','controls':records})
    return records

''' + s[end:]
s=s.replace("env['QT_SCALE_FACTOR']='1.5' if name=='viewer-150' else '1'","env['QT_SCALE_FACTOR']='1'")
s=s.replace("'lifecycle_or_viewer_invariants'","'native_lifecycle_invariants'")
s=s.replace('fixed bounded parser or preview identity control','fixed bounded source declaration control')
s=s.replace('This driver requires the reviewed feature PRs together.','Comparative UI stages are archived separately; native safeguards remain.')
p.write_text(s,encoding='utf-8',newline='\n')
edit('tests/test_remaining_bag_stress3.py',
     "        self.assertEqual(stages['viewer-150']['checks'],5)",
     "        self.assertNotIn('viewer-150',stages)\n        self.assertNotIn('viewer-100',stages)\n        self.assertNotIn('session',stages)")
edit('tests/test_remaining_bag_stress3.py','self.assertEqual(len(rows),12)','self.assertEqual(len(rows),3)')
edit('tests/test_remaining_bag_stress3.py',
     "next(r for r in rows if r['name']=='empty-result')['actualDecision'],'confirmed_loss'",
     "next(r for r in rows if r['name']=='conflicting-mime')['actualDecision'],'invalid'")
edit('tests/test_remaining_bag_stress3.py',
     "next(r for r in rows if r['name']=='dtd-entity')['actualDecision'],'not_checked'",
     "next(r for r in rows if r['name']=='inspection-budget')['actualDecision'],'not_checked'")

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
p=Path('docs/review/AIO_MODULE_RETIREMENT_M16_M17_2026-10-30_RU.md')
with p.open('a',encoding='utf-8') as f:f.write('''

## Доработка по первому полному прогону

Первый isolated run 40973524852 не принят. Linux показал раннюю очистку сохранённой
UI-сессии; вместо ослабления DOM-теста добавлена независимая retainResults lease.
Windows выявил зависающее чтение тела JSON при закрытии: добавлена проверка готовности
сокета и остановки между короткими чтениями, с прежним idle-timeout и ограничением размера.
В transfer-admission повторно проверяется закрытие под тем же lock.

Пропущенные тесты именно удалённого preview/Qt-observer перенесены в архив, а смешанные
наборы не удалены: шесть lifecycle-тестов сохранены; stress3 сохраняет нативные стадии,
проверку JSON/CSV-доказательств и хешей исходников. Три bounded source-contract контроля
продолжаются, девять сравнительных/preview-контролей остались в архивной версии.
Эти изменения покрытия явно учтены и не означают исправление исторических отклонений.
''')
print('RETIREMENT_RESULT_LIFETIME=independent_from_source_retention;SHARED_TESTS_PRESERVED')
print('RETIREMENT_ADDITIONAL_ARCHIVED_TEST_METHODS=9')
