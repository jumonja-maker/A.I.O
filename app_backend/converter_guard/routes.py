"""Native registry routes, with filename hints separate from observed LO proof.

No hand-written format families, document parsers, quality checks or probes.
Only the installed Filter/Types registry is used. Unknown data stays unknown.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from ..libreoffice_catalog import OOR, EXTENSION, properties, registry_files


class Registry:
    def __init__(self, files, executable):
        self.imports, self.exports, self.filters = {}, {}, {}
        self.external_filters = False
        self.valid = bool(files)
        self.fingerprint = hashlib.sha256(json.dumps(
            [str(executable), files], ensure_ascii=True).encode()).hexdigest()
        types, filters = {}, {}
        for name, _modified, _size in files:
            try:
                root = ET.parse(name).getroot()
            except (OSError, ET.ParseError):
                self.valid = False
                continue
            if Path(name).suffix == '.xcu' and any(
                    element.get(OOR + 'name') in ('Types', 'Filter') or
                    'org.openoffice.TypeDetection.' in element.get(OOR + 'path', '')
                    for element in root.iter()):
                # A shared extension can add exporters outside base XCDs.
                # Its activation/override order belongs to LO, not this reader.
                self.external_filters = True
                self.valid = False
            # GraphicFilter's encoder list is NOT the document FilterFactory.
            # Several XCD fragments extend the same native type/filter.
            for component in root.iter():
                if component.get(OOR + 'name') not in ('Types', 'Filter'):
                    continue
                for group in component.findall('node'):
                    if group.get(OOR + 'name') not in ('Types', 'Filters'):
                        continue
                    collection = types if group.get(OOR + 'name') == 'Types' else filters
                    for node in group.findall('node'):
                        identity = node.get(OOR + 'name')
                        if node.get(OOR + 'op') == 'remove':
                            collection.pop(identity, None)
                            continue
                        if node.get(OOR + 'op') == 'replace':
                            collection[identity] = {}
                        collection.setdefault(identity, {}).update(properties(node))
        self.types = {e.lower() for item in types.values() for e in item.get('Extensions', '').split()
                      if EXTENSION.fullmatch(e.lower())}
        for identity, item in filters.items():
            service = item.get('DocumentService')
            flags = set(item.get('Flags', '').split())
            if not service or not flags or 'NOTINSTALLED' in flags:
                continue
            extensions = {e.lower() for e in types.get(item.get('Type'), {}).get('Extensions', '').split()
                          if EXTENSION.fullmatch(e.lower())}
            self.filters[identity] = (service, frozenset(extensions), frozenset(flags))
            for flag, mapping in (('IMPORT', self.imports), ('EXPORT', self.exports)):
                if flag in flags:
                    for extension in extensions:
                        mapping.setdefault(extension, set()).add(service)
        self.valid = self.valid and bool(self.imports) and bool(self.exports)

    def hint(self, filename, target):
        services = self.imports.get(Path(filename).suffix.lower().lstrip('.'), set())
        return self.decide(target, services)

    def decide(self, target, services):
        exports = self.exports.get(target, set())
        if not self.valid:
            return 'undetermined'
        if not exports:
            # Native import/graphic encoders do not imply CLI document export.
            return 'unavailable' if target in self.types else 'undetermined'
        if not services:
            return 'undetermined'
        return 'registered' if exports.intersection(services) else 'unavailable'

    def proof(self, filter_name, target):
        item = self.filters.get(filter_name)
        if not self.valid or not item or 'EXPORT' not in item[2] or target not in item[1]:
            return None
        return {'documentService': item[0], 'filter': filter_name,
                'registryFingerprint': self.fingerprint, 'basis': 'native_selected_export_filter'}

    def observed_decision(self, target, proof):
        if (proof and proof.get('basis') == 'native_preflight_document_kind'
                and proof.get('registryFingerprint') == self.fingerprint
                and proof.get('state') == 'detected'
                and isinstance(proof.get('documentServices'), list)):
            return self.decide(target, set(proof['documentServices']))
        if (not proof or proof.get('registryFingerprint') != self.fingerprint
                or proof.get('basis') != 'native_selected_export_filter'):
            return 'undetermined'
        return self.decide(target, {proof['documentService']})


@lru_cache(maxsize=2)
def _load(files, executable):
    return Registry(files, executable)


def registry(executable):
    try:
        files = registry_files(executable)
        if executable:
            install = Path(executable).resolve().parent.parent
            overlays = []
            for folder in (install / 'share/extensions', install / 'share/uno_packages/cache'):
                if folder.is_dir():
                    overlays.extend((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                                    for p in sorted(folder.rglob('*.xcu')))
            files = files + tuple(overlays)
    except OSError:
        files = ()
    return _load(files, str(executable))
