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
s=replace(s,old,new,2);p.write_text(s,encoding='utf-8',newline='\n')

p=Path('tests/test_conversion_history.py');s=p.read_text(encoding='utf-8')
s=replace(s,'import subprocess\n','import subprocess\nimport sys\n')
extra='''    def test_save_history_write_retains_existing_io_lease(self):
        identity,state=self.ready();observed=[];original=self.guard._history.record
        def record(*args):
            owned=self.guard._jobs[identity]
            observed.append((owned.io,owned.saving))
            return original(*args)
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


'''
s=replace(s,'class HistoryHTTP(unittest.TestCase):',extra+'class HistoryHTTP(unittest.TestCase):')
p.write_text(s,encoding='utf-8',newline='\n')
print('HISTORY_CANDIDATE_CORRECTIONS=missing_path_and_save_lease;TESTS_NOT_DISABLED')
