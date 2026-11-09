"""Verify and safely unpack the pinned owner-provided 178-source QA corpus."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
from zipfile import ZipFile

DATA_REF = '6a1a2571f67c1a8041ad28de290f4457b805d032'
ARCHIVE_SHA256 = '8ca1f19a0d72405dd6a4df289bab68a393be71b090517683e392e665936c137d'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    archive = args.data_root/'qa-fixtures/current178/AIO_NEW_SAMPLES_2026-11-08_178_FILES.zip'
    manifest_path = archive.with_name('provenance.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert archive.stat().st_size == 3658536
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ARCHIVE_SHA256 == manifest['archiveSha256']
    assert manifest['extractedFiles'] == len(manifest['files']) == 178
    members = {row['path']: row for row in manifest['files']}
    assert len(members) == 178
    args.output.mkdir(parents=True, exist_ok=False)
    originals = args.output/'originals'
    originals.mkdir()
    expanded = 0
    with ZipFile(archive) as z:
        assert len(z.infolist()) == 178 and set(z.namelist()) == set(members)
        for info in z.infolist():
            path = PurePosixPath(info.filename)
            assert len(path.parts) == 1 and not path.is_absolute() and path.name not in ('.', '..')
            assert '\\' not in path.name and ':' not in path.name and not path.name.endswith((' ','.'))
            assert not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16)
            row = members[info.filename]
            assert info.file_size == row['bytes'] <= 16*1024*1024
            expanded += info.file_size
            assert expanded <= 40*1024*1024
            raw = z.read(info)
            assert hashlib.sha256(raw).hexdigest() == row['sha256'], info.filename
            (originals/path.name).write_bytes(raw)
    assert expanded == manifest['expandedBytes'] == 34001777
    (args.output/'provenance.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status':'PASS','dataRef':DATA_REF,'archiveSha256':ARCHIVE_SHA256,'files':178,'bytes':expanded}))


if __name__ == '__main__':
    main()
