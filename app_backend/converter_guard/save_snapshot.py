"""Finalize an immutable folder-save report using already-read-back metadata.

No file reads, conversion, OCR or hashes are performed here. The final
AIO-save-status.json, with the same saveId, is the completion authority.
"""
from copy import deepcopy
import re

from .errors import GuardError


def snapshot_report(report, manifest, status, *, job_state_at_start):
    """Return a detached report scoped to this save, not the live queue."""
    result = deepcopy(report)
    result['reportScope'] = 'conversion_snapshot_at_save_start'
    result['saveSnapshot'] = {**deepcopy(status),
        'scope': 'saved_output_snapshot',
        'jobStateAtStart': job_state_at_start,
        'completionAuthority': 'AIO-save-status.json',
        'acknowledgementScope': 'destination_bytes_not_recovery_journal'}
    if status.get('state') != 'completed':
        return result

    def invalid():
        raise GuardError('SAVE_REPORT_MISMATCH',
            'Отчёт не совпадает с проверенным набором сохранённых файлов.', stage='save')

    count = len(manifest)
    if (status.get('readbackVerified') is not True or
            status.get('verificationMethod') != 'independent_read_sha256' or
            any(type(status.get(key)) is not int or status[key] != count
                for key in ('outputsExpected', 'outputsWritten', 'outputsVerified'))):
        invalid()
    verified = {}
    for entry in manifest:
        path = entry.get('path')
        if (not isinstance(path, str) or not path or path in verified or
                entry.get('readbackVerified') is not True or
                type(entry.get('size')) is not int or entry['size'] < 0 or
                type(entry.get('readbackSize')) is not int or
                entry.get('readbackSize') != entry['size'] or
                not re.fullmatch('[0-9a-f]{64}', str(entry.get('sha256'))) or
                entry.get('readbackSha256') != entry['sha256']):
            invalid()
        verified[path] = entry
    seen = set()
    for item in result.get('files', []):
        for task in item.get('targets', []):
            outputs = task.get('outputs', [])
            for output in outputs:
                path = output.get('path')
                entry = verified.get(path)
                if (entry is None or path in seen or
                        entry.get('fileId') != item.get('fileId') or
                        entry.get('format') != task.get('format') or
                        entry['size'] != output.get('size') or
                        output.get('sha256') not in (None, entry['sha256'])):
                    invalid()
                seen.add(path)
                output.update(saved=True, readbackVerified=True,
                              savedInSnapshot=True, saveId=status['saveId'])
            if outputs:
                # Save evidence describes this verified snapshot even when
                # conversion itself had an error or produced partial results.
                task['saveStatus'] = 'saved'
                task['readbackVerified'] = True
            if outputs and task.get('state') in ('ready', 'saved'):
                task['stateAtSaveStart'] = task['state']
                task['state'] = 'saved'
    if seen != set(verified):
        invalid()
    return result
