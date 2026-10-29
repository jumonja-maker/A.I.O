"""One-shot source preparation on an isolated branch; candidate never moves refs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import unittest

REPO = 'jumonja-maker/A.I.O'
BASE = 'e95b65df3f7781b19b8426ae322694dcca1d3802'
BASE_TREE = 'f03b281b10912687428c50d143ccd8be34049acb'
assert os.environ['GITHUB_REPOSITORY'] == REPO
assert os.environ['GITHUB_REF'] == 'refs/heads/work/recovery-save-m02-m30-20261029'
EXPECTED = {
    'app_backend/converter_guard/core.py': '3253150b7c94cc73f7b758bfec8eb2f99b8486bf',
    'app_backend/converter_guard/http_api.py': '517a7131048cc13ea765a606cdcb11f87ed5c17a',
}
texts = {}
for name, expected in EXPECTED.items():
    data = Path(name).read_bytes()
    actual = hashlib.sha1(('blob %d\0' % len(data)).encode() + data).hexdigest()
    assert actual == expected, (name, actual)
    texts[name] = data.decode('utf-8')

# Reproduce the two concrete defects on the unchanged product code first.
sys.path[:0] = [str(Path.cwd()), str(Path.cwd()/'tests')]
from test_save_snapshot_report import FolderSnapshotIntegration
from test_recovery_cleanup_responsiveness import RecoveryCleanupTests
baseline = unittest.TestSuite([
    FolderSnapshotIntegration('test_completed_save_report_matches_verified_manifest'),
    RecoveryCleanupTests('test_disappearing_child_does_not_release_live_storage_accounting'),
])
result = unittest.TextTestRunner(verbosity=2).run(baseline)
assert len(result.failures) == 2 and not result.errors, 'Baseline did not reproduce both defects'
print('AIO_BASELINE_REPRODUCED=stale_saved_report,forgotten_partial_cleanup', flush=True)


def replace(name, before, after):
    assert texts[name].count(before) == 1, (name, before[:120], texts[name].count(before))
    texts[name] = texts[name].replace(before, after, 1)


core = 'app_backend/converter_guard/core.py'
http = 'app_backend/converter_guard/http_api.py'
replace(core, '    cleaning: bool = False\n',
'''    cleaning: bool = False
    cleanup_background: bool = False
    cleanup_retry_at: float = 0.0
''')
replace(core, "        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-guard')\n",
"        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-guard')\n        # Lazy, separate disk cleanup: never queue deletion behind native conversion.\n        self._cleanup_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='aio-cleanup')\n")
replace(core, '        for root in roots:\n            if len(self._jobs)>=self.limits.max_jobs:',
'''        for root in roots:
            with self._lock:
                if any(job.root == root for job in self._jobs.values()):
                    # A deleting job may already have closed owner.lock. It is
                    # still ours until physical cleanup releases its quota.
                    continue
            if len(self._jobs)>=self.limits.max_jobs:''')
replace(core, '    def close_conversion(self, job_id, *, discard=False):\n        if type(discard) is not bool:',
'''    def close_conversion(self, job_id, *, discard=False, defer_cleanup=False):
        if type(discard) is not bool or type(defer_cleanup) is not bool:''')
replace(core, '            job.bundle_cancel.set();job.save_cancel.set();job.released=True\n',
'''            job.bundle_cancel.set();job.save_cancel.set();job.released=True
            job.cleanup_background = job.cleanup_background or defer_cleanup
''')
replace(core, '        self._cleanup(job)\n        return outcome\n\n    def release(self, job_id, *, discard=False):\n        if type(discard) is not bool:',
'''        self._cleanup(job)
        with self._lock:
            outcome['cleanupPending'] = job.id in self._jobs
        return outcome

    def release(self, job_id, *, discard=False, defer_cleanup=False):
        if type(discard) is not bool or type(defer_cleanup) is not bool:''')
replace(core, '                job.released = True\n                callbacks = self._stop(job)\n',
'''                job.released = True
                job.cleanup_background = job.cleanup_background or defer_cleanup
                callbacks = self._stop(job)
''')
replace(core, '        if not preserve:\n            self._cleanup(job)\n\n    def _cleanup(self, job):',
'''        if not preserve:
            self._cleanup(job)
        with self._lock:
            return {'jobId': job.id, 'released': not preserve, 'retained': preserve,
                    'cleanupPending': not preserve and job.id in self._jobs}

    def _cleanup(self, job):''')
replace(core, '            if job.running or job.io or job.pending_journal or job.cleaning:\n                return\n',
'''            if job.running or job.io or job.pending_journal or job.cleaning:
                return
            if job.cleanup_background and not self._closed and time.monotonic() < job.cleanup_retry_at:
                return
''')
replace(core, '            job.cleaning = True\n        # Potentially slow filesystem deletion never holds the global job lock.\n        try:',
'''            job.cleaning = True
            if job.cleanup_background and not self._closed:
                try:
                    # Submit under the state lock so close() cannot shut this
                    # executor down between claiming and submitting the job.
                    self._cleanup_executor.submit(self._cleanup_owned, job, preserve)
                except RuntimeError:
                    self._cleanup_deferred(job)
                return
        self._cleanup_owned(job, preserve)

    def _cleanup_deferred(self, job):
        with self._lock:
            job.cleaning = False
            job.cleanup_retry_at = time.monotonic() + 2.0
            self._record(job, 'cleanup_deferred', code='CLEANUP_RETRY')

    def _cleanup_owned(self, job, preserve):
        # Potentially slow filesystem deletion never holds the global job lock.
        try:''')
replace(core, '''        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            with self._lock:
                job.cleaning = False
                self._record(job, 'cleanup_deferred', code='CLEANUP_RETRY')
            return
        with self._lock:
            for artifact in job.artifacts + job.diagnostic_artifacts + ([job.bundle] if job.bundle else []):''',
'''        except FileNotFoundError:
            # A disappearing CHILD is not proof that the owned tree is gone.
            # Keep the retirement barrier, ownership and quota until retried.
            if os.path.lexists(job.root):
                self._cleanup_deferred(job)
                return
        except Exception:
            self._cleanup_deferred(job)
            return
        with self._lock:
            for artifact in job.artifacts + job.diagnostic_artifacts + ([job.bundle] if job.bundle else []):''')
replace(core, '''            # Leased jobs deliberately remain registered until their final
            # caller exits. Clearing them here would unlock a live recovery
            # tree and lose accounting after the bounded shutdown wait.

    def storage_stats(self):''',
'''            # Leased jobs deliberately remain registered until their final
            # caller exits. Clearing them here would unlock a live recovery
            # tree and lose accounting after the bounded shutdown wait.
        self._cleanup_executor.shutdown(wait=True)

    def storage_stats(self):''')
replace(core, "                    'cleanupPending': sum(j.released for j in self._jobs.values()),\n",
"                    'cleanupPending': sum(j.released for j in self._jobs.values()),\n                    'cleanupRetryJobs': sum(j.released and j.cleanup_retry_at > 0 and not j.cleaning\n                                            for j in self._jobs.values()),\n")
replace(http, "outcome=self.guard.close_conversion(identity,discard=body.get('discard',False))",
"outcome=self.guard.close_conversion(identity,discard=body.get('discard',False),defer_cleanup=True)")
replace(http, "                self.guard.release(identity, discard=body.get('discard', False))\n                self.comparison.release(identity)\n                return self._json(handler,200, {'ok': True})",
"                outcome=self.guard.release(identity, discard=body.get('discard', False),defer_cleanup=True)\n                self.comparison.release(identity)\n                return self._json(handler,200, {'ok': True, **outcome})")

# Three saved files describe one immutable snapshot and the same saveId.
replace(core, '        from .save_verification import verify_folder\n',
'        from .save_verification import verify_folder\n        from .save_snapshot import snapshot_report\n')
replace(core, "            atomic_json(folder / 'AIO-conversion-report.json', report)\n",
'''            atomic_json(folder / 'AIO-conversion-report.json', snapshot_report(
                report, manifest, save_status('verifying'), job_state_at_start=state_at_start))
''')
replace(core, "            atomic_json(folder / 'AIO-save-status.json', save_status('completed'))\n",
'''            atomic_json(folder / 'AIO-conversion-report.json', snapshot_report(
                report, manifest, save_status('completed'), job_state_at_start=state_at_start))
            atomic_json(folder / 'AIO-save-status.json', save_status('completed'))
''')
replace(core, "                    atomic_json(folder / 'AIO-save-status.json',\n                                save_status('cancelled' if code == 'SAVE_CANCELLED' else 'failed', code))",
'''                    failed_status = save_status('cancelled' if code == 'SAVE_CANCELLED' else 'failed', code)
                    atomic_json(folder / 'AIO-conversion-report.json', snapshot_report(
                        report, manifest, failed_status, job_state_at_start=state_at_start))
                    atomic_json(folder / 'AIO-save-status.json', failed_status)''')

for name, content in texts.items():
    compile(content, name, 'exec')
    Path(name).write_text(content, encoding='utf-8', newline='\n')
# A fresh interpreter prevents the baseline imports from masking our new code.
for pattern in ('test_save_snapshot_report.py', 'test_recovery_cleanup_responsiveness.py',
                'test_guard_save*.py', 'test_comparison_recovery_v20.py'):
    subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p',pattern,'-v'],check=True)
print('AIO_RECOVERY_SAVE_TARGETED=PASS', flush=True)


def api(method, path, data=None):
    request=urllib.request.Request('https://api.github.com/repos/'+REPO+path,
        data=None if data is None else json.dumps(data).encode(),method=method,
        headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],
                 'Accept':'application/vnd.github+json','Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=60) as response:
        return json.load(response)

for name in ('app_backend/converter_guard/save_snapshot.py',
             'tests/test_save_snapshot_report.py','tests/test_recovery_cleanup_responsiveness.py',
             'docs/review/AIO_RECOVERY_SAVE_M02_M30_2026-10-29_RU.md'):
    texts[name]=Path(name).read_text(encoding='utf-8')
assert api('GET','/git/commits/'+BASE)['tree']['sha']==BASE_TREE
entries=[{'path':name,'mode':'100644','type':'blob','content':content} for name,content in texts.items()]
tree=api('POST','/git/trees',{'base_tree':BASE_TREE,'tree':entries})['sha']
sha=api('POST','/git/commits',{'message':'fix(recovery): defer UI cleanup safely and finalize verified save reports',
    'tree':tree,'parents':[BASE]})['sha']
print('AIO_CANDIDATE_COMMIT='+sha,flush=True)
print('AIO_CANDIDATE_PARENT='+BASE,flush=True)
print('AIO_REFS_UNCHANGED=true',flush=True)
