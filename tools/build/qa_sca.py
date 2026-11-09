"""Fail-closed OSV audit of the exact declared Python distribution profile.

This is not an audit of vendored libraries, Qt/LibreOffice or system binaries.
Matching records are not distinct, proven exploitable A.I.O vulnerabilities.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.build.verify_qa_dependency_lock import profile_lock

ENDPOINT = 'https://api.osv.dev/v1/querybatch'
SCOPE = 'declared Python distributions; vendored/native/system components not audited'


def queries_for(phase):
    pins, hashes = profile_lock(phase)
    return [{'package': {'name': name, 'ecosystem': 'PyPI'}, 'version': item[1]}
            for name, item in sorted(pins.items())], hashes


def classify_response(response, queries):
    rows = response.get('results') if isinstance(response, dict) else None
    if not isinstance(rows, list) or len(rows) != len(queries):
        raise ValueError('Incomplete OSV batch response')
    records = []
    for query, row in zip(queries, rows):
        if not isinstance(row, dict) or set(row) - {'vulns', 'next_page_token'} or row.get('next_page_token'):
            raise ValueError('Incomplete/ambiguous OSV package response')
        vulnerabilities = row.get('vulns', [])
        if not isinstance(vulnerabilities, list):
            raise ValueError('Invalid OSV advisory inventory')
        for item in vulnerabilities:
            identity = item.get('id') if isinstance(item, dict) else None
            if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', identity):
                raise ValueError('Invalid OSV advisory identifier')
            records.append({'package': query['package']['name'], 'version': query['version'], 'id': identity})
    return {'status': 'FINDINGS' if records else 'PASS', 'matchingRecords': len(records),
            'advisoryIds': sorted({item['id'] for item in records}), 'records': records}


def fetch_osv(queries):
    request = Request(ENDPOINT, data=json.dumps({'queries': queries}).encode('utf-8'),
                      headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=25) as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('OSV response exceeds audit budget')
    return json.loads(raw)


def audit(phase, source, *, fetch=None):
    if not re.fullmatch(r'[a-f0-9]{40}', source):
        raise ValueError('SCA requires an exact source SHA')
    queries, hashes = queries_for(phase)
    value = {'schema': 1, 'sourceRevision': source, 'phase': phase,
             'checkedAt': datetime.now(timezone.utc).isoformat(), 'provider': ENDPOINT,
             'scope': SCOPE, 'queries': queries, 'resolvedLockSha256': hashes,
             'status': 'INCOMPLETE'}
    try:
        value['response'] = (fetch or fetch_osv)(queries)
        value.update(classify_response(value['response'], queries))
    except Exception as error:
        value.update(status='INCOMPLETE', error=type(error).__name__ + ': ' + str(error))
    return value


def verify_report(path, phase, source):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    queries, hashes = queries_for(phase)
    if (value.get('schema') != 1 or value.get('sourceRevision') != source or
            value.get('phase') != phase or value.get('provider') != ENDPOINT or
            value.get('scope') != SCOPE or value.get('queries') != queries or
            value.get('resolvedLockSha256') != hashes):
        raise ValueError('SCA report belongs to different sources/dependencies')
    derived = classify_response(value.get('response'), queries)
    if derived['status'] != 'PASS' or any(value.get(key) != item for key, item in derived.items()):
        raise ValueError('SCA report has findings or unconfirmed results')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('runtime', 'build'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = audit(args.phase, os.environ.get('AIO_QA_SOURCE_SHA', ''))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print('AIO_QA_SCA=' + value['status'] + '; scope=' + SCOPE, flush=True)
    return 0 if value['status'] == 'PASS' else 1 if value['status'] == 'FINDINGS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
