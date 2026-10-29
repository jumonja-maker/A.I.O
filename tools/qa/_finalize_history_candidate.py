"""Disposable candidate corrections, applied before all acceptance tests."""
from pathlib import Path

def replace(text,before,after,count=1):
 assert text.count(before)==count,(before[:90],text.count(before),count)
 return text.replace(before,after)

p=Path('app_backend/converter_guard/conversion_history.py');s=p.read_text(encoding='utf-8')
s=replace(s,'class ConversionHistory:', '''def _link_if_present(path):
    # A new history and absent SQLite sidecars are normal. Other stat errors
    # still fail closed; a dangling symlink remains a link, not an empty DB.
    try:
        return path_is_link(path)
    except FileNotFoundError:
        return False


class ConversionHistory:''')
s=replace(s,'if path_is_link(self.root) or path_is_link(self.path):',
 'if _link_if_present(self.root) or _link_if_present(self.path):')
s=replace(s,"if path_is_link(Path(str(self.path) + suffix)):",
 "if _link_if_present(Path(str(self.path) + suffix)):")
s=replace(s,"'revision INTEGER NOT NULL, recorded_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, '",
 "'session TEXT NOT NULL, revision INTEGER NOT NULL, recorded_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, '")
s=replace(s,'    def record(self, job_id, revision, report):\n        identity = _identity(job_id)',
 "    def record(self, job_id, revision, report, *, session=None):\n        identity = _identity(job_id)\n        session = '0'*32 if session is None else _identity(session)")
s=replace(s,"'scope': 'terminal_conversion_report', 'containsResultFiles': False,",
 "'scope': 'terminal_conversion_report', 'containsResultFiles': False, 'sessionId': session,")
s=replace(s,"'(job_id,revision,recorded_ms,updated_ms,summary,report) VALUES(?,?,?,?,?,?) '",
 "'(job_id,session,revision,recorded_ms,updated_ms,summary,report) VALUES(?,?,?,?,?,?,?) '")
s=replace(s,"'WHERE history.deleted=0 AND excluded.revision>history.revision',\n                (identity, revision, now, now, encoded_summary, encoded))",
 "'WHERE history.deleted=0 AND history.session=excluded.session AND excluded.revision>history.revision',\n                (identity, session, revision, now, now, encoded_summary, encoded))")
adopt='''    def adopt(self, job_id, session):
        """Bind a recovered job before publication, without inventing a report.

        Recovery owns the result journal lock. Its in-memory revision restarts;
        a fresh generation permits updates and rejects delayed old writers.
        A tombstone always wins, including across application restarts.
        """
        identity, session = _identity(job_id), _identity(session)
        now = time.time_ns() // 1000000
        with self._connection(write=True) as connection:
            connection.execute('INSERT INTO history '
                '(job_id,session,revision,recorded_ms,updated_ms,summary,report) '
                'VALUES(?,?,-1,?,?,NULL,NULL) ON CONFLICT(job_id) DO UPDATE SET '
                'session=excluded.session,revision=-1 '
                'WHERE history.deleted=0 AND history.session!=excluded.session',
                (identity, session, now, now))

'''
s=replace(s,'    def list(self, *, before=None, limit=25):',adopt+'    def list(self, *, before=None, limit=25):')
s=replace(s,"'WHERE deleted=0 AND seq<? ORDER BY seq DESC LIMIT ?',",
 "'WHERE deleted=0 AND report IS NOT NULL AND seq<? ORDER BY seq DESC LIMIT ?',")
s=replace(s,"'SELECT report FROM history WHERE job_id=? AND deleted=0', (identity,)).fetchone()",
 "'SELECT report FROM history WHERE job_id=? AND deleted=0 AND report IS NOT NULL', (identity,)).fetchone()")
s=replace(s,"'(job_id,revision,recorded_ms,updated_ms,summary,report,deleted) '\n                'VALUES(?,0,?,?,NULL,NULL,1) ON CONFLICT(job_id) DO UPDATE SET '",
 "'(job_id,session,revision,recorded_ms,updated_ms,summary,report,deleted) '\n                'VALUES(?,?,0,?,?,NULL,NULL,1) ON CONFLICT(job_id) DO UPDATE SET '")
s=replace(s,'                (identity, now, now))','                (identity, \'0\'*32, now, now))')
p.write_text(s,encoding='utf-8',newline='\n')

p=Path('app_backend/converter_guard/core.py');s=p.read_text(encoding='utf-8')
old="""        finally:
            with self._lock:
                job.saving = False
                job.io -= 1
                self._record(job, 'save_idle')
                self._io_drained.notify_all()
            self._remember_history(job)
            self._cleanup(job)"""
new="""        finally:
            # Keep the existing save I/O lease until its metadata snapshot is
            # persisted; close() must not race this final history write.
            self._remember_history(job)
            with self._lock:
                job.saving = False
                job.io -= 1
                self._record(job, 'save_idle')
                self._io_drained.notify_all()
            self._cleanup(job)"""
s=replace(s,old,new,2)
s=replace(s,'    recovered: bool = False\n',
 '    recovered: bool = False\n    history_session: str = field(default_factory=lambda: secrets.token_hex(16))\n')
s=replace(s,'            self._history.record(job.id, revision, report)',
 '            self._history.record(job.id, revision, report, session=job.history_session)')
s=replace(s,'    def _publish_recovered(self, job):\n', '''    def _publish_recovered(self, job):
        if self._history is not None:
            try:
                # Only explicit recovery reaches here, with ownership of the
                # result journal. Never open SQLite under the Guard state lock.
                self._history.adopt(job.id, job.history_session)
            except Exception:
                self._history_write_error = 'HISTORY_WRITE_FAILED'
''')
p.write_text(s,encoding='utf-8',newline='\n')

p=Path('tests/test_conversion_history.py');s=p.read_text(encoding='utf-8')
s=replace(s,'import subprocess\n','import subprocess\nimport sys\n')
s=replace(s,'        def blocked(*args):entered.set();release.wait(3)',
 '        def blocked(*args,**kwargs):entered.set();release.wait(3)')
extra='''    def test_save_history_write_retains_existing_io_lease(self):
        identity,state=self.ready();observed=[];original=self.guard._history.record
        def record(*args,**kwargs):
            owned=self.guard._jobs[identity]
            observed.append((owned.io,owned.saving))
            return original(*args,**kwargs)
        with patch.object(self.guard._history,'record',side_effect=record):
            self.guard.save_folder(state['result']['ticket'],self.root)
        self.assertEqual(len(observed),1)
        self.assertGreater(observed[0][0],0)
        self.assertTrue(observed[0][1])
    def test_history_survives_actual_process_restart(self):
        identity,_=self.ready()
        self.guard.release(identity,discard=True);self.guard.close()
        code="from app_backend.converter_guard.conversion_history import ConversionHistory; import sys,json; print(json.dumps(ConversionHistory(sys.argv[1]).report(sys.argv[2])))"
        raw=subprocess.check_output([sys.executable,'-c',code,str(self.root/'history'),identity],
            text=True,encoding='utf-8',timeout=15)
        data=json.loads(raw)
        self.assertEqual(data['historySnapshot']['jobId'],identity)
        self.assertEqual(data['failed'],1)
    def test_real_recovery_save_updates_history_after_revision_reset(self):
        parent=self.root/'recoverable';history=self.root/'recovered-history'
        first=ConverterGuard(adapter=FakeAdapter(),recovery_parent=parent,history_parent=history)
        self.addCleanup(first.close)
        identity=first.create([{'id':'f','name':'restored.docx','size':4}],['pdf'])['jobId']
        first.upload(identity,'f',io.BytesIO(b'keep'),4)
        first.start(identity,archive_mode='on_demand')
        wait_finished(first,identity);first._jobs[identity].future.result(timeout=10)
        old=first.history_report(identity)['historySnapshot'];first.close()
        second=ConverterGuard(adapter=FakeAdapter(),recovery_parent=parent,history_parent=history)
        self.addCleanup(second.close)
        recovered=second.status(identity)
        self.assertTrue(second._jobs[identity].recovered)
        outcome=second.save_folder(recovered['result']['ticket'],self.root)
        self.assertTrue(outcome['saved'])
        current=second.history_report(identity)
        self.assertNotEqual(current['historySnapshot']['sessionId'],old['sessionId'])
        self.assertEqual(second.history_list()['items'][0]['savedOutputs'],1)
        self.assertTrue(current['files'][0]['targets'][0]['outputs'][0]['readbackVerified'])


'''
s=replace(s,'class HistoryHTTP(unittest.TestCase):',extra+'class HistoryHTTP(unittest.TestCase):')
store_tests='''    def test_recovered_generation_accepts_low_revision_and_rejects_old_writer(self):
        old,new='c'*32,'d'*32
        self.history.record('a'*32,90,report(),session=old)
        self.history.adopt('a'*32,new)
        self.history.record('a'*32,2,report(saved=True),session=new)
        self.history.record('a'*32,999,report(saved=False),session=old)
        self.assertEqual(self.history.report('a'*32)['historySnapshot']['revision'],2)
        self.assertEqual(self.history.list()['items'][0]['savedOutputs'],1)
    def test_adoption_does_not_resurrect_a_deleted_report(self):
        self.history.record('a'*32,1,report())
        self.history.delete('a'*32);self.history.adopt('a'*32,'c'*32)
        self.history.record('a'*32,3,report(),session='c'*32)
        self.assertEqual(self.history.list()['items'],[])
    def test_adoption_without_report_is_not_a_completed_conversion(self):
        self.history.adopt('a'*32,'c'*32)
        self.assertEqual(self.history.list()['items'],[])
        with self.assertRaises(GuardError):self.history.report('a'*32)
        self.history.record('a'*32,99,report(),session='d'*32)
        self.assertEqual(self.history.list()['items'],[])
        self.history.record('a'*32,1,report(),session='c'*32)
        self.assertEqual(len(self.history.list()['items']),1)


'''
s=replace(s,'class HistoryGuardIntegration(unittest.TestCase):',store_tests+'class HistoryGuardIntegration(unittest.TestCase):')
p.write_text(s,encoding='utf-8',newline='\n')
print('HISTORY_CANDIDATE_CORRECTIONS=missing_path_save_lease_and_recovery_generation;TESTS_NOT_DISABLED')
