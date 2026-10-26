# Windows handle count — QA references versus product ownership

The final scale acceptance sample (179 handles baseline → 1190 converted → 1206 before discard → 1203 after close) is not a valid post-lifetime leak measurement: the QA function still holds the same job graph after product registries have been emptied.

Concrete strong references in `tools/qa/guard_v21_scale_acceptance.py`:

- The conversion wait loop assigns local `job = guard._jobs[job_id]`; this local survives until `main` returns.
- The cleanup assertions separately assign `saved_job = guard._jobs[job_id]`. Deleting only one alias would not release the graph.
- Both lead to `_Job.files` → 1000 `_File.cancel` instances. `_Task` itself has no Event; the per-file cancellation Event is in `_File`.
- Job-level cancel/bundle/pause/save Events, journal lock, ResultStore lock and a completed Future also remain reachable through that graph.
- Original analysis polling local `item` and cleanup local `saved_item` refer to different analysis generations after three service reopen cycles. They hold analysis Events/Futures. The completed original Future has a done-callback that captures the item and original service; collection of this small cycle is different from the 1000-file job graph.
- JSON snapshots (`converted`, report pages, rows) retain memory but do not themselves own those Events. `records` is explicitly deleted in the scale harness. ObservedTree does not retain a list of 1006 process objects.

Primary source verification (CPython 3.11.9):

- https://github.com/python/cpython/blob/v3.11.9/Include/internal/pycore_condvar.h sets `_PY_EMULATED_WIN_CV=1` by default and describes the `HANDLE sem` field.
- https://github.com/python/cpython/blob/v3.11.9/Python/condvar.h creates this semaphore with `CreateSemaphore` in `PyCOND_INIT` and closes it in `PyCOND_FINI`.
- https://github.com/python/cpython/blob/v3.11.9/Python/thread_nt.h allocates that condition variable for each lock and frees it when the Python lock is destroyed.

Thus one Windows semaphore per reachable per-file cancellation Event is a concrete explanation for the roughly +1000 increase already BEFORE comparison starts. This is a source-supported hypothesis for NIKITA, not a substitute for the Windows ablation run.

Added QA-only `tools/qa/guard_v21_windows_handles_acceptance.py`: two 1000-file controlled CSV conversion/discard cycles in the same process, with no LibreOffice, no comparison subprocesses, no installation. It measures before, converted, closed with QA aliases retained, explicitly dropped aliases BEFORE GC, and after GC. It does not clear any product collection, replace Events or call native CloseHandle. It keeps weak references to the job and every file and checks product registry/I/O/owner-lock cleanup separately. Windows additionally requires at least 80% of the per-file increase to disappear and the baseline/second cycle to remain within +32 handles.

Linux validation: PASS, 2×1000 files in 3.271 seconds. In both cycles the QA aliases kept 1 job + 1000 files alive after Guard close; dropping those aliases released all 1001 objects before GC, and gc.collect collected 0. File descriptors were 4 baseline → 5 converted → 4 after close and remained 4 across both cycles. RSS did not return to its cold value; this probe does not claim whole-process heap reclamation or absence of leaks elsewhere.

NIKITA handle result: pending parent-run one-off. The separate probe isolates the 1000-file ownership hypothesis; it does not independently certify the smaller residual analysis/service handle difference in the original scale run or overall application leak freedom.
