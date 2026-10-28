"""Atomic job metadata and append-only terminal-task records, without tokens.

Only application-owned output paths are restored. No source file is imported
and no conversion resumes automatically after restart. Unsaved output bytes
remain owned until an explicit save acknowledgement or discard.
"""
import json
import hashlib
import os
from pathlib import Path
import threading
import secrets
import stat
import shutil


def path_is_link(path):
    # pathlib.Path.is_junction is Python 3.12+, while packaged A.I.O uses
    # 3.11. Reject Windows reparse points as well as POSIX symbolic links.
    return path.is_symlink() or bool(getattr(path.lstat(),'st_file_attributes',0) &
                                     getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400))


def data_directory():
    if os.environ.get('AIO_GUARD_DATA_DIR'):
        return Path(os.environ['AIO_GUARD_DATA_DIR']).expanduser().resolve()
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    return base / 'AIO' / 'ConverterGuard'


def atomic_json(path, value):
    temporary = path.with_suffix('.new')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=True, separators=(',', ':'))
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


class ResultStore:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.owner = None

    def acquire(self):
        self.owner = (self.root / 'owner.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.owner.tell() == 0:
                    self.owner.write(b'0')
                    self.owner.flush()
                self.owner.seek(0)
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.close()
            return False

    def close(self):
        if self.owner:
            self.owner.close()
            self.owner = None

    @property
    def retirement_path(self):
        # Outside the tree being removed: rmtree may delete job.json/owner.lock
        # in any order, but cannot expose a half-deleted tree as recoverable.
        return self.root.parent / ('.' + self.root.name + '.retiring.json')

    def _retirement_info(self):
        marker = self.retirement_path
        if not os.path.lexists(marker):
            return None
        if path_is_link(marker) or marker.stat().st_size > 8192:
            raise ValueError('Invalid retirement marker')
        try:
            value = json.loads(marker.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('Invalid retirement marker')
            checksum = value.pop('checksum', None)
            actual = self._digest(value)
        except RecursionError as error:
            raise ValueError('Retirement marker nesting is unsupported') from error
        if (checksum != actual or value.get('schema') != 1
                or value.get('kind') != 'aio_guard_retirement'
                or value.get('rootName') != self.root.name
                or any(type(value.get(key)) is not int or value[key] < 0
                       for key in ('device', 'inode'))):
            raise ValueError('Invalid retirement marker')
        return value

    def retirement_pending(self):
        value = self._retirement_info()
        if value is None:
            return False
        if path_is_link(self.root) or not self.root.is_dir():
            raise ValueError('Retirement root changed')
        info = self.root.stat()
        if (info.st_dev, info.st_ino) != (value['device'], value['inode']):
            raise ValueError('Retirement root changed')
        return True

    def retire(self):
        """Durably remove a saved/discarded tree from recovery admission."""
        if self.owner is None and not self.acquire():
            raise OSError('Retirement storage is busy')
        try:
            if self.retirement_pending():
                return
        except ValueError as error:
            raise OSError('Invalid existing retirement marker') from error
        info = self.root.stat()
        value = {'schema': 1, 'kind': 'aio_guard_retirement',
                 'rootName': self.root.name, 'device': info.st_dev, 'inode': info.st_ino}
        value['checksum'] = self._digest(value)
        marker = self.retirement_path
        temporary = marker.with_name(marker.name + '.' + secrets.token_hex(8) + '.new')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(marker)
        finally:
            temporary.unlink(missing_ok=True)

    def cleanup_retired(self):
        """Retry only an identity-bound retirement, never an ordinary orphan."""
        if not self.retirement_pending():
            raise ValueError('Storage was not retired')
        self.close()  # Windows cannot remove an open owner.lock file.
        shutil.rmtree(self.root)
        # Do not remove the admission barrier until the whole root is gone.
        if not os.path.lexists(self.root):
            self.retirement_path.unlink(missing_ok=True)

    @classmethod
    def reap_finished_retirements(cls, parent):
        # A crash after root deletion but before marker deletion leaves only a
        # tiny barrier. Reap validated barriers with no root, never user trees.
        for marker in Path(parent).glob('.aio-guard-*.retiring.json'):
            root = marker.parent / marker.name[1:-len('.retiring.json')]
            store = cls(root)
            try:
                if store._retirement_info() is not None and not os.path.lexists(root):
                    marker.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                continue

    def initialize(self, job):
        value = {'schema': 1, 'jobId': job.id,
            'inputAccounting':job.input_accounting,
            'retainSources':getattr(job, 'retain_sources', False),
            'comparisonParentJobId':getattr(job, 'comparison_parent_job_id', None),
            'comparisonRecipe':getattr(job, 'comparison_recipe', None),
            'comparisonParentTasks':list(getattr(job, 'comparison_parent_tasks', ())),
            'files': [{'id': f.id, 'name': f.name, 'size': f.size,'nativeRoute':f.native_route,
                       'sourceSha256':getattr(f,'content_digest',None),
                       'sourcePath':(f.source.relative_to(self.root).as_posix()
                                     if getattr(job, 'retain_sources', False) else None),
                       'resources':[{'path':path, 'size':item['size'],
                                     'digest':item.get('digest'), 'ready':item.get('ready') is True}
                                    for path, item in f.resources.items()],
                       'targets': [{'index': t.index, 'format': t.format} for t in f.targets]}
                      for f in job.files]}
        value['checksum'] = self._digest(value)
        atomic_json(self.root / 'job.json', value)

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value,ensure_ascii=True,sort_keys=True,
                                         separators=(',',':')).encode('ascii')).hexdigest()

    def append(self, record):
        self.append_many((record,))

    def append_many(self, records):
        with self.lock, (self.root / 'results.jsonl').open('a', encoding='utf-8') as output:
            for record in records:
                output.write(json.dumps({'record':record,'checksum':self._digest(record)},
                                         ensure_ascii=True,separators=(',', ':'))+'\n')
            output.flush()
            os.fsync(output.fileno())

    def task(self, item, task):
        self.tasks(((item, task),))

    def tasks(self, entries):
        # Each terminal task still has its own begin/commit transaction.
        # A cancellation batch needs one flush/fsync rather than one per task.
        def records():
            for item, task in entries:
                yield from self._task_records(item, task)
        self.append_many(records())

    def _task_records(self, item, task):
        transaction=secrets.token_hex(16)
        yield {'kind':'task_begin','transaction':transaction,'index': task.index, 'fileId': item.id,
            'state': task.state, 'code': task.code, 'error': task.error,
            'outputCount':len(task.artifacts),
            'stage': task.stage, 'diagnostic': task.diagnostic,
            'startedMs': task.started_ms, 'finishedMs': task.finished_ms,
            'durationMs':task.duration_ms,'executionMode':task.execution_mode,
            'nativeTimings':task.native_timings,
            'resultChecks':task.result_checks, 'executionReason':task.execution_reason,
            'automaticRecipe':getattr(task, 'automatic_recipe', None),
            'automaticParameters':getattr(task, 'automatic_parameters', None),
            'exportFilter':task.export_filter,'warnings':task.warnings,'quality':task.quality,
            'loStarted': task.lo_started}
        for artifact in task.artifacts:
            yield {'kind':'output','transaction':transaction,
                       'output':{'path':artifact.path.relative_to(self.root).as_posix(),
                                 'archiveName':artifact.archive_name,'name':artifact.name,'size':artifact.size,
                                 'sha256':getattr(artifact,'sha256',None),
                                 'sourceSheet':getattr(artifact,'source_sheet',None)}}
        yield {'kind':'task_commit','transaction':transaction}

    def saved(self, artifacts, *, readback_verified=False):
        self.append_many({'kind':'saved','paths':[a.path.relative_to(self.root).as_posix()],
            'readbackVerified':readback_verified or getattr(a, 'readback_verified', False)} for a in artifacts)

    def finish(self, job):
        self.append({'kind':'summary','timings':dict(job.timings),'officeStarts':job.office_starts,
                     'fileTimeoutSeconds':job.file_timeout,'state':job.state})

    def read(self):
        header = self.root / 'job.json'
        if header.is_symlink() or header.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Invalid recovery header')
        value = json.loads(header.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('schema') != 1:
            raise ValueError('Unknown recovery schema')
        checksum=value.pop('checksum',None)
        if checksum != self._digest(value):
            raise ValueError('Recovery header checksum mismatch')
        records = {}
        saved = set()
        readback = set()
        pending = {}
        # A damaged middle record is not an ordinary EOF. Its unindexed
        # artifacts may still exist and MUST NOT be deleted after a partial save.
        integrity = 'complete'
        ledger = self.root / 'results.jsonl'
        if ledger.is_symlink():
            raise ValueError('Invalid recovery ledger')
        if ledger.exists():
            with ledger.open(encoding='utf-8') as stream:
                while line := stream.readline(8 * 1024 * 1024 + 1):
                    # A crash may leave an incomplete final append. Earlier
                    # fsynced records remain valid; never infer a ready output.
                    if len(line) > 8 * 1024 * 1024:
                        integrity = 'corrupt'
                        break
                    if not line.endswith('\n'):
                        integrity = 'torn_tail'
                        break
                    try:
                        envelope = json.loads(line)
                        record = envelope['record']
                        if envelope.get('checksum') != self._digest(record):
                            integrity = 'corrupt'
                            break
                        if record.get('kind') == 'task' and type(record.get('index')) is int:
                            records[record['index']] = record
                        elif record.get('kind') == 'task_begin':
                            # append_many serializes whole transactions. A new
                            # begin before the prior commit is evidence of an
                            # interrupted append, even if later transactions
                            # are valid. Keep recovering them, but never erase
                            # that evidence and allow routine cleanup.
                            if pending and integrity == 'complete':
                                integrity = 'incomplete_transaction'
                            pending={record['transaction']:{**record,'outputs':[]}}
                        elif record.get('kind') == 'output' and record.get('transaction') in pending:
                            pending[record['transaction']]['outputs'].append(record['output'])
                        elif record.get('kind') == 'task_commit' and record.get('transaction') in pending:
                            complete=pending.pop(record['transaction'])
                            # Individual checksums do not expose a wholly lost
                            # output line. A committed HTML bundle must retain
                            # every artifact promised by its begin record. Old
                            # journals remain readable but cannot gain this
                            # stronger completeness claim retroactively.
                            expected=complete.get('outputCount')
                            paths=[output['path'] for output in complete['outputs']]
                            if (len(set(paths)) != len(paths) or
                                    'outputCount' in complete and (type(expected) is not int
                                    or expected < 0 or expected != len(complete['outputs']))):
                                integrity='incomplete_transaction'
                                continue
                            records[complete['index']]=complete
                        elif record.get('kind') == 'saved':
                            saved.update(p for p in record.get('paths', []) if isinstance(p, str))
                            if record.get('readbackVerified') is True:
                                readback.update(p for p in record.get('paths', []) if isinstance(p, str))
                        elif record.get('kind') == 'summary':
                            value['summary']=record
                    except (ValueError, TypeError, AttributeError, KeyError):
                        integrity = 'corrupt'
                        break
        if pending and integrity == 'complete':
            integrity = 'incomplete_transaction'
        value['readbackVerifiedPaths'] = sorted(readback)
        return value, records, saved, integrity

    def output_path(self, relative, size):
        if not isinstance(relative, str) or type(size) is not int or size < 0:
            raise ValueError('Invalid output metadata')
        path = self.root / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('Output path escapes its job')
        for part in (path, *path.parents):
            if part == self.root.parent:
                break
            if path_is_link(part):
                raise ValueError('Symlink in recovery output')
        if (not path.resolve(strict=True).is_relative_to(self.root.resolve())
                or not path.is_file() or path.stat().st_size != size):
            raise ValueError('Output is missing or changed')
        return path

    def input_path(self, relative, size, digest):
        """Restore only a verified original inside this job's private input tree."""
        if (not isinstance(relative, str) or '\\' in relative
                or len(Path(relative).parts) < 3 or Path(relative).parts[0] != 'input'
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Invalid retained source metadata')
        path = self.output_path(relative, size)
        actual = hashlib.sha256()
        with path.open('rb') as source:
            while block := source.read(64 * 1024):
                actual.update(block)
        if actual.hexdigest() != digest:
            raise ValueError('Retained source changed')
        return path
