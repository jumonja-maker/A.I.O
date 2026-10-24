"""Read-only acceptance of the two original FULLTEST WordML HTML regressions.

Run against an existing original corpus and reviewed source. The shared Guard
acceptance harness performs real preflight/queue/save and hashes every saved
artifact. This reader independently checks body/region text, ordered table
cells and exact embedded image payloads for HTML, HTM and XHTML. It does not
certify pagination or visual equivalence; page regions are labelled companions.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

# Running this tool as a script places its directory on sys.path. Keep the
# import explicit for programmatic callers too, without adding dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_v17_native_acceptance as native

W = 'http://schemas.microsoft.com/office/word/2003/wordml'
H = 'http://www.w3.org/1999/xhtml'
NAMES = ('02_LEVEL_2.xml', '03_LEVEL_3.xml')
TARGETS = ('html', 'htm', 'xhtml')


def normal(text):
    return re.sub(r'\s+', ' ', text).strip()


def make_cases(seeds, corpus):
    if corpus is None:
        raise ValueError('The original FULLTEST corpus is required')
    missing = [{'source': name, 'status': 'INCOMPLETE', 'reason': 'original_sample_not_available'}
               for name in NAMES if not (corpus / name).is_file()]
    cases = [dict(source=corpus / name, targets=list(TARGETS), kind='wordml')
             for name in NAMES if (corpus / name).is_file()]
    return cases, missing


def _cells(root, namespace, cell_tag, text_tag=None):
    result = []
    for cell in root.iter('{' + namespace + '}' + cell_tag):
        if text_tag is None:
            value = ''.join(cell.itertext())
        else:
            value = ''.join(''.join(node.itertext()) for node in cell.iter('{' + namespace + '}' + text_tag))
        result.append(normal(value))
    return result


def check_case(case, target, paths, work, executable, seconds):
    source = ET.parse(case['source']).getroot()
    if source.tag != '{' + W + '}wordDocument':
        raise AssertionError('Acceptance source is not Word 2003 XML')
    roots, images = [], []
    primary = companion = None
    for path in paths:
        if path.suffix.lower() in ('.html', '.htm', '.xhtml'):
            roots.append(ET.parse(path).getroot())
            if '.aio-page-regions.' in path.name:
                companion = path
            elif path.stem == case['source'].stem:
                primary = path
        elif path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.bin'):
            images.append(hashlib.sha256(path.read_bytes()).hexdigest())
    if primary is None or companion is None:
        raise AssertionError('Missing native primary or page-region companion')
    primary_root = ET.parse(primary).getroot()
    links = [node.get('href', '') for node in primary_root.iter('{' + H + '}a')]
    if not any(not urlsplit(href).scheme and unquote(urlsplit(href).path) == companion.name for href in links):
        raise AssertionError('Native primary does not visibly link its page regions')
    all_text = normal(' '.join(' '.join(root.itertext()) for root in roots))
    required = [normal(''.join(node.itertext())) for node in source.iter('{' + W + '}t')]
    counts = Counter(value for value in required if value)
    missing = [value for value, count in counts.items() if all_text.count(value) < count]
    if missing:
        raise AssertionError('Source text markers or their repeated occurrences are missing: ' + repr(missing))
    source_cells = _cells(source, W, 'tc', 't')
    result_cells = [cell for root in roots for cell in _cells(root, H, 'td')]
    if result_cells != source_cells:
        raise AssertionError('Ordered table cell content/count differs from the original WordML')
    expected_images = []
    for binary in source.iter('{' + W + '}binData'):
        payload = base64.b64decode(''.join(''.join(binary.itertext()).split()), validate=True)
        expected_images.append(hashlib.sha256(payload).hexdigest())
    for root in roots:
        for image in root.iter('{' + H + '}img'):
            uri = image.get('src', '')
            if uri.startswith('data:'):
                payload = base64.b64decode(''.join(uri.split(',', 1)[1].split()), validate=True)
                images.append(hashlib.sha256(payload).hexdigest())
    if any(images.count(value) < count for value, count in Counter(expected_images).items()):
        raise AssertionError('Original embedded image bytes are absent or changed')
    source_regions = [node for node in source.iter() if node.tag in ('{' + W + '}hdr', '{' + W + '}ftr')]
    companion_root = ET.parse(companion).getroot()
    sections = list(companion_root.iter('{' + H + '}section'))
    if len(source_regions) != len(sections):
        raise AssertionError('Page-region count differs from original WordML')
    for region, section in zip(source_regions, sections):
        section_text = normal(''.join(section.itertext()))
        for text in region.iter('{' + W + '}t'):
            if normal(''.join(text.itertext())) not in section_text:
                raise AssertionError('Page-region source text is missing from its companion section')
    return {'method': 'independent_WordML_text_ordered_cells_exact_images_and_region_companions',
            'textMarkers': len(required), 'tableCells': len(source_cells),
            'originalImageSha256': expected_images, 'pageRegions': len(source_regions),
            'visibleCompanionLink': True, 'paginationCertified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=90)
    args = parser.parse_args(argv)
    if not args.corpus.is_dir():
        parser.error('An existing original FULLTEST corpus is required')
    return native.main(['--corpus', str(args.corpus), '--output', str(args.output),
        '--executable', str(args.executable), '--seconds', str(args.seconds)],
        case_factory=make_cases, case_checker=check_case,
        scope='six original FULLTEST WordML HTML HTM XHTML routes with regional content')


if __name__ == '__main__':
    sys.exit(main())
