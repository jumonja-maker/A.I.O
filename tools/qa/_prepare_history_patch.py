"""Disposable exact-base preparation; never shipped in the product tree."""
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen

BASE = '25712d9bc2a0cc25df8fdb52f2670593f451ac1a'
BASES = {
 'app_backend/converter_guard/core.py':'7226c93c30067066e31f96630c21d78e5886801d',
 'app_backend/converter_guard/http_api.py':'6ecbafc00241e5707272ae1b1d4f4cdbebb0d2fc',
 'app_backend/server.py':'7d0cb33c0c2456efc4dc43e8913f2a61a50f35e5',
 'index.html':'304c8ed1ce812153fa8d3b4b38c606a54263f1fe',
}
PRODUCT = [*BASES, 'src/guard-queue.js', 'src/conversion-history.js',
 'app_backend/converter_guard/conversion_history.py', 'tests/test_conversion_history.py',
 'tests/conversion_history_dom.mjs']

def blob(raw):
 return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()

def replace(text, before, after, count=1):
 assert text.count(before)==count,(before[:90],text.count(before),count)
 return text.replace(before,after)

METHODS = '''    def _remember_history(self, job):
        """Persist metadata on worker/save boundaries, outside the state lock.

        No outputs or sources are opened. An unavailable history must never
        change conversion success, save acknowledgements or result ownership.
        """
        if self._history is None:
            return
        try:
            with self._lock:
                if job.preparation or job.running or job.state not in TERMINAL:
                    return
                revision = job.revision
                report = copy.deepcopy({**self._report(job), 'jobId':job.id,
                    'state':job.state, 'officeStarts':job.office_starts,
                    'packagingError':job.packaging_error})
            self._history.record(job.id, revision, report)
        except Exception:
            # Sticky for this process: a later successful write does not prove
            # that a previously missed report was recovered.
            self._history_write_error = 'HISTORY_WRITE_FAILED'

    def history_list(self, *, before=None, limit=25):
        if self._history is None:
            return {'schema':1, 'enabled':False, 'items':[], 'nextCursor':None}
        return {**self._history.list(before=before, limit=limit),
                'writeWarning':self._history_write_error}

    def history_report(self, identity):
        if self._history is None:
            raise GuardError('HISTORY_DISABLED', 'Постоянная история недоступна.',
                             stage='history', status=503)
        return self._history.report(identity)

    def delete_history(self, identity):
        if self._history is None:
            raise GuardError('HISTORY_DISABLED', 'Постоянная история недоступна.',
                             stage='history', status=503)
        return self._history.delete(identity)

'''

GET = '''            if path in (PREFIX + '/history', PREFIX + '/history/report'):
                query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
                if path.endswith('/report'):
                    if set(query) != {'job'} or len(query['job']) != 1:
                        raise GuardError('HISTORY_REQUEST', 'Неверный запрос истории.')
                    return self._json(handler,200,self.guard.history_report(self._identity(query['job'][0])))
                if set(query) - {'before','limit'} or any(len(v)!=1 for v in query.values()):
                    raise GuardError('HISTORY_REQUEST', 'Неверная страница истории.')
                def number(name, default):
                    raw = query.get(name)
                    if raw is None:
                        return default
                    if re.fullmatch(r'[0-9]{1,19}', raw[0]) is None:
                        raise GuardError('HISTORY_REQUEST', 'Неверная страница истории.')
                    return int(raw[0])
                return self._json(handler,200,self.guard.history_list(
                    before=number('before',None),limit=number('limit',25)))
'''

if sys.argv[1]=='prepare':
 for name,sha in BASES.items():
  assert blob(Path(name).read_bytes())==sha,('unexpected base',name)
 p=Path('app_backend/converter_guard/core.py');s=p.read_text(encoding='utf-8')
 s=replace(s,'def __init__(self, *, adapter=None, limits=None, spool_parent=None, recovery_parent=None, telemetry=None):',
  'def __init__(self, *, adapter=None, limits=None, spool_parent=None, recovery_parent=None, telemetry=None, history_parent=None):')
 anchor='        self._parent = self._recovery or (None if spool_parent is None else Path(spool_parent).resolve())\n'
 s=replace(s,anchor,anchor+'''        from .conversion_history import ConversionHistory
        self._history = None if history_parent is None else ConversionHistory(history_parent)
        self._history_write_error = None
        if self._history is not None and self._parent is not None:
            history_root = self._history.root.resolve()
            if history_root.is_relative_to(self._parent) or self._parent.is_relative_to(history_root):
                raise ValueError('History must be outside result storage')
''')
 s=replace(s,'    def _load_recovery(self):',METHODS+'    def _load_recovery(self):')
 s=replace(s,"                self._record(job, 'worker_finished', code=job.state)\n            self._cleanup(job)",
  "                self._record(job, 'worker_finished', code=job.state)\n            self._remember_history(job)\n            self._cleanup(job)")
 s=replace(s,"                self._record(job, 'job_finished')\n            self._cleanup(job)",
  "                self._record(job, 'job_finished')\n            self._remember_history(job)\n            self._cleanup(job)")
 anchor="                job.saving = False\n                job.io -= 1\n                self._record(job, 'save_idle')\n                self._io_drained.notify_all()\n            self._cleanup(job)"
 s=replace(s,anchor,anchor.replace('            self._cleanup(job)',
  '            self._remember_history(job)\n            self._cleanup(job)'),2)
 p.write_text(s,encoding='utf-8',newline='\n')
 p=Path('app_backend/converter_guard/http_api.py');s=p.read_text(encoding='utf-8')
 s=replace(s,"            if path == PREFIX + '/status':",GET+"            if path == PREFIX + '/status':")
 anchor="            identity = self._identity(body.get('jobId'))\n"
 s=replace(s,anchor,anchor+'''            if path == PREFIX + '/history/delete':
                if set(body) != {'jobId','confirm'} or body.get('confirm') is not True:
                    raise GuardError('HISTORY_CONFIRM', 'Подтвердите удаление только записи истории.')
                return self._json(handler,200,self.guard.delete_history(identity))
''')
 p.write_text(s,encoding='utf-8',newline='\n')
 p=Path('app_backend/server.py');s=p.read_text(encoding='utf-8')
 s=replace(s,'def start_server(port: int = 0, *, guard_recovery_parent=None):',
   'def start_server(port: int = 0, *, guard_recovery_parent=None, guard_history_parent=None):')
 s=replace(s,'            telemetry=server.telemetry)\n        server.guard_api = GuardAPI(server.guard,',
 '''            telemetry=server.telemetry,
            history_parent=(guard_history_parent if guard_history_parent is not None else
                data_directory() / 'history' if guard_recovery_parent is None else None))
        server.guard_api = GuardAPI(server.guard,''')
 p.write_text(s,encoding='utf-8',newline='\n')
 p=Path('index.html');s=p.read_text(encoding='utf-8')
 s=replace(s,'  <script src="src/guard-queue.js" defer></script>',
   '  <script src="src/conversion-history.js" defer></script>\n  <script src="src/guard-queue.js" defer></script>')
 p.write_text(s,encoding='utf-8',newline='\n')
 p=Path('src/guard-queue.js');s=p.read_text(encoding='utf-8')
 s=replace(s,'  let renderedRecoveryWarnings=null;',
   '  let historyPanel=null;\n  let renderedRecoveryWarnings=null;')
 s=replace(s,'    if(!next){if(root)refreshRevision++;',
   '    if(!next){historyPanel?.dispose();historyPanel=null;if(root)refreshRevision++;')
 s=replace(s,'    root=next;rowCache=paintedRows=null;renderedRecoveryWarnings=null;',
   '    historyPanel?.dispose();historyPanel=null;\n    root=next;rowCache=paintedRows=null;renderedRecoveryWarnings=null;')
 s=replace(s,'    footer.append(bar);attachTopButton();',
   "    footer.append(bar);attachTopButton();\n    historyPanel=window.AIOConversionHistory?.mount(bar.querySelector('.aio-guard-popup'));")
 p.write_text(s,encoding='utf-8',newline='\n')
 print('HISTORY_PREPARED_EXACT_BASE='+BASE)
elif sys.argv[1]=='publish':
 for name in PRODUCT:
  raw=Path(name).read_bytes();sha=blob(raw)
  data=json.dumps({'content':raw.decode('utf-8'),'encoding':'utf-8'}).encode()
  request=Request('https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']+'/git/blobs',data=data,
    headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Content-Type':'application/json','Accept':'application/vnd.github+json'})
  with urlopen(request,timeout=30) as response:result=json.load(response)
  assert result['sha']==sha
  print('HISTORY_TESTED_BLOB='+json.dumps({'path':name,'sha':sha,'bytes':len(raw)}),flush=True)
else:
 raise SystemExit('prepare or publish required')
