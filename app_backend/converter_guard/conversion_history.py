"""M04: local report history, independent of retained conversion bytes.

Construction and an empty read do not create storage. Writes occur only at
explicit lifecycle boundaries, never in preflight or a per-target progress poll.
SQLite transactions order revisions; tombstones prevent delayed writes from
resurrecting a record the user removed. This is not a backup of output files.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time

from .errors import GuardError
from .result_store import path_is_link


_SCHEMA = 1
_PRIVATE_KEYS = frozenset(('ticket', 'downloadUrl', 'previewUrl', 'token'))


def _identity(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise GuardError('HISTORY_REQUEST', 'Неверный идентификатор записи истории.', stage='history')
    return value


def _detached(value):
    # Reports contain names and diagnostics, never source/output file bytes.
    # Old or future report producers must not persist ephemeral capabilities.
    if isinstance(value, dict):
        return {key: _detached(item) for key, item in value.items() if key not in _PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_detached(item) for item in value]
    return value


class ConversionHistory:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.path = self.root / 'conversions.sqlite3'

    @contextmanager
    def _connection(self, *, write=False):
        connection = None
        try:
            if path_is_link(self.root) or path_is_link(self.path):
                raise OSError('history link')
            if not write and not self.path.exists():
                yield None
                return
            if write:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self.root.is_dir():
                raise OSError('history directory')
            for suffix in ('-journal', '-wal', '-shm'):
                if path_is_link(Path(str(self.path) + suffix)):
                    raise OSError('history journal link')
            uri = self.path.as_uri() + ('?mode=rwc' if write else '?mode=ro')
            connection = sqlite3.connect(uri, uri=True, timeout=0.5)
            if write:
                connection.execute('BEGIN IMMEDIATE')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 0 and write:
                if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise OSError('unknown history schema')
                connection.execute('CREATE TABLE history ('
                    'seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL, '
                    'revision INTEGER NOT NULL, recorded_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, '
                    'summary TEXT, report TEXT, deleted INTEGER NOT NULL DEFAULT 0)')
                connection.execute('CREATE INDEX history_visible ON history(deleted, seq)')
                connection.execute('PRAGMA user_version=1')
            elif version != _SCHEMA:
                raise OSError('unsupported history schema')
            yield connection
            if write:
                connection.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError) as error:
            raise GuardError('HISTORY_STORAGE_FAILED',
                'История недоступна. Файлы конвертации не удалены; повторите запрос.',
                stage='history', status=503) from error
        finally:
            if connection is not None:
                connection.close()

    def record(self, job_id, revision, report):
        identity = _identity(job_id)
        if type(revision) is not int or not 0 <= revision < 2**63 or not isinstance(report, dict):
            raise GuardError('HISTORY_REQUEST', 'Некорректный снимок истории.', stage='history')
        # JSON encoding and summarisation happen outside ConverterGuard's lock.
        value = _detached(report)
        value['historySnapshot'] = {'schema': 1, 'jobId': identity, 'revision': revision,
            'scope': 'terminal_conversion_report', 'containsResultFiles': False,
            'saveStateScope': 'last_observed_not_current_filesystem'}
        files = value.get('files', [])
        outputs = [output for item in files for task in item.get('targets', [])
                   for output in task.get('outputs', [])]
        summary = {'jobId': identity, 'state': value.get('state', 'unknown'),
            'firstInput': files[0].get('name', '') if files else '', 'inputCount': len(files),
            'total': value.get('total', 0), 'succeeded': value.get('succeeded', 0),
            'failed': value.get('failed', 0), 'cancelled': value.get('cancelled', 0),
            'outputs': len(outputs), 'savedOutputs': sum(o.get('saved') is True for o in outputs),
            'revision': revision}
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        encoded_summary = json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        now = time.time_ns() // 1000000
        with self._connection(write=True) as connection:
            connection.execute('INSERT INTO history '
                '(job_id,revision,recorded_ms,updated_ms,summary,report) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(job_id) DO UPDATE SET revision=excluded.revision, '
                'updated_ms=excluded.updated_ms,summary=excluded.summary,report=excluded.report '
                'WHERE history.deleted=0 AND excluded.revision>history.revision',
                (identity, revision, now, now, encoded_summary, encoded))

    def list(self, *, before=None, limit=25):
        if (type(limit) is not int or not 1 <= limit <= 50 or
                before is not None and (type(before) is not int or not 0 < before < 2**63)):
            raise GuardError('HISTORY_REQUEST', 'Неверная страница истории.', stage='history')
        with self._connection() as connection:
            rows = [] if connection is None else connection.execute(
                'SELECT seq,recorded_ms,updated_ms,summary FROM history '
                'WHERE deleted=0 AND seq<? ORDER BY seq DESC LIMIT ?',
                (before if before is not None else 2**63-1, limit+1)).fetchall()
            items = [{**json.loads(row[3]), 'recordedMs': row[1], 'updatedMs': row[2]}
                     for row in rows[:limit]]
        return {'schema': 1, 'enabled': True, 'items': items,
                'nextCursor': rows[limit-1][0] if len(rows) > limit else None,
                'scope': 'terminal_reports_not_result_storage'}

    def report(self, job_id):
        identity = _identity(job_id)
        with self._connection() as connection:
            row = None if connection is None else connection.execute(
                'SELECT report FROM history WHERE job_id=? AND deleted=0', (identity,)).fetchone()
            if row is None:
                raise GuardError('HISTORY_NOT_FOUND', 'Запись истории не найдена.', stage='history', status=404)
            return json.loads(row[0])

    def delete(self, job_id):
        identity = _identity(job_id)
        now = time.time_ns() // 1000000
        with self._connection(write=True) as connection:
            # Retain only a tombstone ID; delayed report writes cannot restore it.
            # No output path is ever opened, removed or acknowledged here.
            connection.execute('INSERT INTO history '
                '(job_id,revision,recorded_ms,updated_ms,summary,report,deleted) '
                'VALUES(?,0,?,?,NULL,NULL,1) ON CONFLICT(job_id) DO UPDATE SET '
                'summary=NULL,report=NULL,deleted=1,updated_ms=excluded.updated_ms',
                (identity, now, now))
        return {'jobId': identity, 'deleted': True, 'resultFilesTouched': False}
