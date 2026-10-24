"""Explicit single-queue matrix run over an immutable local FULLTEST corpus.

Every source is inventoried and uploaded. The plan uses every currently admitted
catalogue/Qt target for each source; blocked inputs retain their exact reason.
No fixture repair, format shortcut, engine concurrency or universal fidelity
claim is introduced. Outputs receive mandatory independent save readback.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_backend.converter_guard import ConverterGuard, GuardLimits, LibreOfficeAdapter
from app_backend.converter_guard.result_store import atomic_json
from guard_v16_bugbag_acceptance import digest
from guard_v16_route_media_acceptance import machine_facts


def source_inventory(corpus):
    corpus = Path(corpus).resolve(strict=True)
    if not corpus.is_dir(): raise ValueError('Corpus must be a directory')
    rows = []
    for path in sorted(corpus.rglob('*'), key=lambda p: p.relative_to(corpus).as_posix()):
        if path.is_symlink(): raise ValueError('Corpus symlinks are not admitted')
        if path.is_file():
            relative = path.relative_to(corpus).as_posix()
            rows.append({'fileId': 'source%04d' % len(rows), 'relativePath': relative,
                'name': path.name, 'size': path.stat().st_size, 'sha256': digest(path)})
    if not rows: raise ValueError('Corpus is empty')
    return rows


def load_fixture_negatives(path, inventory):
    """Only independently reviewed, hash-bound exclusions may satisfy acceptance.

    A diagnosis emitted by the implementation under test is evidence to review,
    not permission for that implementation to waive one of its own failures.
    """
    if path is None: return {}
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or payload.get('schema') != 1 or not isinstance(payload.get('files'), list):
        raise ValueError('Fixture negatives require schema 1 and a files list')
    originals = {row['relativePath']: row for row in inventory}
    reviewed = {}
    classes = {'SOURCE_CONTAINER_INCONSISTENT': 'inconsistent_fixture',
               'HTML_LOCAL_RESOURCES_NOT_UPLOADED': 'fixture_missing_resources'}
    for row in payload['files']:
        if not isinstance(row, dict): raise ValueError('Invalid fixture-negative record')
        relative, sha, code = row.get('relativePath'), row.get('sha256'), row.get('code')
        if (not isinstance(relative, str) or relative not in originals or relative in reviewed
                or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha)
                or originals[relative]['sha256'] != sha or code not in classes
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            raise ValueError('Fixture negative must identify one reviewed original, exact SHA-256, code and reason')
        reviewed[relative] = {**row, 'classification': classes[code]}
    return reviewed


def build_plan(inspection, inventory, reviewed_negatives=None):
    reviewed_negatives = reviewed_negatives or {}
    known = {r['fileId'] for r in inventory}
    selected = {key: [] for key in known}
    catalogue, seen = [], set()
    for target in inspection['formats']:
        identity = target['id']
        if identity not in seen:
            seen.add(identity); catalogue.append(identity)
        for file_id in target.get('selectableFileIds', []):
            if file_id not in known: raise ValueError('Native plan contains an unknown input')
            if identity not in selected[file_id]: selected[file_id].append(identity)
    proofs = {f['id']: f.get('preflight', {}) for f in inspection['files']}
    plan, accounting = [], []
    for source in inventory:
        key = source['fileId']; targets = selected[key]; proof = proofs.get(key, {})
        if targets: plan.append({'fileId': key, 'formats': targets})
        code = proof.get('code') or (None if targets else 'NO_ADMITTED_NATIVE_TARGET')
        contract = proof.get('sourceContract', {})
        reported_class = (
            'inconsistent_fixture' if code == 'SOURCE_CONTAINER_INCONSISTENT' or contract.get('state') == 'invalid' else
            'fixture_missing_resources' if code == 'HTML_LOCAL_RESOURCES_NOT_UPLOADED' else
            'unresolved_input')
        reviewed = reviewed_negatives.get(source['relativePath'])
        approved_negative = (not targets and reviewed is not None
            and reviewed.get('sha256') == source.get('sha256') and reviewed.get('code') == code)
        classification = ('planned' if targets else
            reviewed['classification'] if approved_negative else 'unresolved_input')
        accounting.append({'fileId': key, 'relativePath': source['relativePath'],
            'preflightState': proof.get('state'), 'code': code, 'classification': classification,
            'stage': proof.get('stage') or 'preflight', 'contractReason': contract.get('reason'),
            'reportedFixtureClass': reported_class if not targets else None,
            'reviewedFixtureNegative': approved_negative,
            **({'reviewReason': reviewed['reason']} if approved_negative else {}),
            'targetCount': len(targets), 'targets': targets,
            **({'sourceContract': contract} if contract else {})})
    return plan, accounting, catalogue


def counts(state):
    return {key: state.get(key) for key in ('state', 'total', 'completed', 'attempted',
        'succeeded', 'failed', 'cancelled', 'filtered', 'pending',
        'preflightCompleted', 'preflightTotal', 'preflightUndetermined') if key in state}


def run_matrix(args):
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    inventory = source_inventory(args.corpus)
    if args.expected_sources and len(inventory) != args.expected_sources:
        raise ValueError('Expected %d originals, found %d' % (args.expected_sources, len(inventory)))
    atomic_json(args.output / 'source-inventory-before.json', {
        'schema': 1, 'files': inventory, 'count': len(inventory),
        'bytes': sum(r['size'] for r in inventory),
        'extensions': dict(sorted(Counter(Path(r['name']).suffix.lower() for r in inventory).items()))})
    negatives_path = getattr(args, 'fixture_negatives', None)
    reviewed_negatives = load_fixture_negatives(negatives_path, inventory)
    evidence = {'schema': 1, 'status': 'running', 'scope': 'all_admitted_pairs_single_queue',
        'machine': machine_facts(args.executable), 'sources': len(inventory),
        'qualityCheckRequested': args.quality_check, 'semanticQualityCertified': False,
        'windowsGuiTested': False, 'supervisorSeconds': args.max_seconds,
        'perTaskSeconds': args.task_seconds, 'preflightSeconds': args.preflight_seconds,
        'reviewedFixtureNegatives': len(reviewed_negatives),
        'requireCompletePreflight': bool(getattr(args, 'require_complete_preflight', False)),
        'fixtureNegativesSha256': digest(negatives_path) if negatives_path else None}
    atomic_json(args.output / 'reviewed-fixture-negatives.json', {'schema': 1,
        'files': list(reviewed_negatives.values()), 'sha256': evidence['fixtureNegativesSha256']})
    atomic_json(args.output / 'matrix-status.json', evidence)
    guard = ConverterGuard(adapter=LibreOfficeAdapter(args.executable),
        limits=GuardLimits(file_timeout=args.task_seconds, preflight_timeout=args.preflight_seconds,
            retention_seconds=max(1800, args.max_seconds + 600)), recovery_parent=args.output / 'owned')
    job, saved = None, None
    uploaded = {}
    next_progress = 0
    cancel_reason = None
    save_cancel, watchdog_stop = threading.Event(), threading.Event()
    progress_lock, supervision_lock = threading.Lock(), threading.Lock()

    def progress(phase, state=None, *, force=False, **details):
        nonlocal next_progress
        with progress_lock:
            now = time.monotonic()
            if force or now >= next_progress:
                row = {'phase': phase, 'elapsedSeconds': round(now - started, 1),
                       **(counts(state) if state else {}), **details}
                atomic_json(args.output / 'progress.json', row)
                print(json.dumps(row, ensure_ascii=True), flush=True)
                next_progress = now + args.progress_seconds

    def supervise():
        nonlocal cancel_reason
        with supervision_lock:
            if cancel_reason: return
            if time.monotonic() - started >= args.max_seconds:
                cancel_reason = 'QA_SUPERVISOR_DEADLINE'
            elif args.min_free_gb and shutil.disk_usage(args.output).free < args.min_free_gb * 1024 ** 3:
                cancel_reason = 'QA_DISK_FREE_FLOOR'
            if cancel_reason:
                save_cancel.set()
                if job is not None:
                    try: guard.cancel(job)
                    except Exception: pass  # A completed/released job needs no native cancellation.
                progress('cancelling', force=True, code=cancel_reason)

    def watch():
        while not watchdog_stop.wait(.25):
            try: supervise()
            except OSError:
                save_cancel.set()
    watchdog = threading.Thread(target=watch, name='AIO-fullmatrix-QA-supervisor', daemon=True)
    watchdog.start()

    try:
        metadata = [{'id': r['fileId'], 'name': r['name'], 'size': r['size']} for r in inventory]
        job = guard.create(metadata, prepare_only=True)['jobId']; evidence['jobId'] = job
        for number, source in enumerate(inventory, 1):
            supervise()
            if cancel_reason: raise RuntimeError(cancel_reason)
            with (args.corpus / source['relativePath']).open('rb') as stream:
                guard.upload(job, source['fileId'], stream, source['size'])
            progress('upload', force=number == len(inventory), uploaded=number, totalSources=len(inventory))
        # start() removes inputs without an admitted plan from the live job.
        # Preserve every actual upload proof before that selection takes place.
        upload_report = guard.report(job)
        uploaded.update({f['fileId']: f.get('sourceSha256') for f in upload_report['files']})
        atomic_json(args.output / 'source-upload-provenance.json', {'schema': 1,
            'files': [{'fileId': r['fileId'], 'uploadedSourceSha256': uploaded.get(r['fileId'])}
                      for r in inventory]})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(guard.preflight, job)
            state = guard.status(job)
            while not future.done():
                newer = guard.status(job, since_revision=state.get('revision'), delta=True)
                if not newer.get('unchanged'): state = newer
                supervise(); progress('preflight', state); time.sleep(1)
            inspection = future.result()
        atomic_json(args.output / 'preflight.json', inspection)
        plan, accounting, catalogue = build_plan(inspection, inventory, reviewed_negatives)
        pairs = sum(len(p['formats']) for p in plan)
        evidence.update(plannedPairs=pairs, inputsPlanned=len(plan), inputsWithoutTasks=len(inventory) - len(plan),
            catalogueTargets=catalogue, cataloguePairs=len(catalogue) * len(inventory),
            excludedPairs=len(catalogue) * len(inventory) - pairs,
            inputReasonCounts=dict(sorted(Counter(r['code'] for r in accounting if r['code']).items())),
            inputClassifications=dict(sorted(Counter(r['classification'] for r in accounting).items())))
        atomic_json(args.output / 'input-accounting.json', {'schema': 1, 'inputs': accounting})
        atomic_json(args.output / 'conversion-plan.json', {'schema': 1, 'pairs': pairs, 'plan': plan})
        unresolved_inputs = [{key: row.get(key) for key in
            ('relativePath', 'code', 'stage', 'contractReason', 'preflightState')}
            for row in accounting if row['classification'] == 'unresolved_input']
        evidence['unresolvedInputs'] = unresolved_inputs
        if evidence['requireCompletePreflight'] and unresolved_inputs:
            evidence.update(status='preflight_unresolved', acceptancePassed=False)
            progress('preflight_unresolved', force=True, acceptancePassed=False,
                     unresolvedInputs=unresolved_inputs)
            return evidence
        if not plan:
            evidence['status'] = 'no_admitted_tasks'; return evidence
        guard.start(job, file_ids=[p['fileId'] for p in plan], plan=plan, only_verified=True,
                    archive_mode='on_demand', quality_check=args.quality_check)
        state = guard.status(job)
        while True:
            newer = guard.status(job, since_revision=state.get('revision'), delta=True)
            if not newer.get('unchanged'): state = newer
            supervise(); progress('conversion', state, force=state['finished'])
            if state['finished']: break
            time.sleep(1)
        report = guard.report(job)
        atomic_json(args.output / 'conversion-report-before-save.json', report)
        evidence['tasks'] = counts(state)
        if state.get('result'):
            required = guard.storage_stats()['unsavedBytes']
            if shutil.disk_usage(args.output).free < required + args.min_free_gb * 1024 ** 3:
                raise RuntimeError('QA_SAVE_WOULD_CROSS_DISK_FREE_FLOOR')
            progress('save', force=True, expectedBytes=required)
            def saved_progress(current, total):
                progress('save_or_readback', files=current, totalFiles=total)
            saved = guard.save_folder(state['result']['ticket'], args.output, verify=True, cancel=save_cancel,
                on_progress=saved_progress, on_verifying=lambda: progress('independent_readback', force=True))
            evidence['save'] = saved
            if not saved.get('readbackVerified') or saved.get('outputsVerified') != saved.get('outputs'):
                raise AssertionError('Incomplete independent save readback')
        else:
            evidence['save'] = {'status': 'not_run', 'reason': 'no_ready_outputs'}
        supervise()
        evidence['cancelReason'] = cancel_reason
        unresolved = sum(r['classification'] == 'unresolved_input' for r in accounting)
        reconciled = (state['total'] == pairs and state['completed'] == pairs and
            state['succeeded'] == pairs and not state['failed'] and not state['cancelled'] and
            not state['filtered'] and not state['pending'] and state['state'] in ('ready', 'saved'))
        evidence['plannedPairsFullySucceeded'] = reconciled
        evidence['status'] = ('cancelled' if cancel_reason or state['cancelled'] else
            'completed_with_failures' if not reconciled or unresolved else
            'completed_with_fixture_negatives' if len(plan) != len(inventory) else 'completed')
        evidence['acceptancePassed'] = (not cancel_reason and reconciled
            and not unresolved and (not state['succeeded'] or bool(saved and saved.get('readbackVerified'))))
        return evidence
    except Exception as error:
        evidence.update(status='cancelled' if cancel_reason else 'failed', error={'type': type(error).__name__,
            'code': getattr(error, 'code', None), 'message': str(error)}, cancelReason=cancel_reason)
        return evidence
    finally:
        if job is not None:
            try:
                final_report = guard.report(job)
                atomic_json(args.output / 'conversion-report-final.json', final_report)
                uploaded.update({f['fileId']: f.get('sourceSha256') for f in final_report.get('files', [])
                                 if f.get('sourceSha256') is not None})
            except Exception as error: evidence['finalReportError'] = type(error).__name__
        after = []
        for source in inventory:
            path = args.corpus / source['relativePath']
            read_error = None
            try:
                actual_size = path.stat().st_size if path.is_file() else None
                actual_sha256 = digest(path) if path.is_file() else None
            except OSError as error:
                actual_size, actual_sha256, read_error = None, None, type(error).__name__
            unchanged = actual_size == source['size'] and actual_sha256 == source['sha256']
            after.append({'fileId': source['fileId'], 'relativePath': source['relativePath'], 'unchanged': unchanged,
                'size': actual_size, 'sha256': actual_sha256,
                **({'readError': read_error} if read_error else {}),
                'uploadedSourceSha256': uploaded.get(source['fileId']),
                'uploadMatchesInventory': uploaded.get(source['fileId']) == source['sha256']})
        evidence['sourcesUnchanged'] = all(r['unchanged'] for r in after)
        evidence['uploadsMatchInventory'] = all(r['uploadMatchesInventory'] for r in after)
        if evidence.get('acceptancePassed') and not evidence['uploadsMatchInventory']:
            evidence['status'], evidence['acceptancePassed'] = 'source_upload_mismatch', False
        if not evidence['sourcesUnchanged']:
            evidence['status'], evidence['acceptancePassed'] = 'source_changed', False
        atomic_json(args.output / 'source-inventory-after.json', {'schema': 1, 'files': after})
        supervise()
        if cancel_reason:
            evidence.update(cancelReason=cancel_reason, acceptancePassed=False)
            if evidence['status'] in ('completed', 'completed_with_fixture_negatives'):
                evidence['status'] = 'cancelled'
        watchdog_stop.set(); watchdog.join(timeout=1)
        guard.close()
        evidence['elapsedSeconds'] = round(time.monotonic() - started, 1)
        atomic_json(args.output / 'matrix-status.json', evidence)
        progress('finished', force=True, status=evidence['status'],
                 acceptancePassed=evidence.get('acceptancePassed', False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--expected-sources', type=int, default=510)
    parser.add_argument('--task-seconds', type=float, default=180)
    parser.add_argument('--preflight-seconds', type=float, default=120)
    parser.add_argument('--max-seconds', type=float, default=4 * 60 * 60)
    parser.add_argument('--progress-seconds', type=float, default=30)
    parser.add_argument('--min-free-gb', type=float, default=5)
    parser.add_argument('--quality-check', action='store_true')
    parser.add_argument('--require-complete-preflight', action='store_true',
        help='Fail before starting conversions if any original has an unreviewed unresolved preflight result')
    parser.add_argument('--fixture-negatives', type=Path,
        help='Optional independently reviewed exclusions JSON: schema 1, files with relativePath, sha256, code, reason')
    args = parser.parse_args()
    args.corpus, args.output, args.executable = args.corpus.resolve(), args.output.resolve(), args.executable.resolve()
    if (not args.corpus.is_dir() or not args.executable.is_file() or args.output.is_relative_to(args.corpus)
            or args.expected_sources < 0 or any(not math.isfinite(n) for n in
                (args.task_seconds, args.preflight_seconds, args.max_seconds, args.progress_seconds, args.min_free_gb))
            or min(args.task_seconds, args.preflight_seconds, args.max_seconds, args.progress_seconds) <= 0
            or args.min_free_gb < 0):
        parser.error('Existing corpus/executable, separate new output directory and positive QA deadlines required')
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtGui import QGuiApplication
    application = QGuiApplication.instance() or QGuiApplication([])
    evidence = run_matrix(args)
    return int(not evidence.get('acceptancePassed', False))


if __name__ == '__main__':
    sys.exit(main())
