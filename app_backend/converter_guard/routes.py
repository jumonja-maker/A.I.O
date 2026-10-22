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
        self.imports, self.exports, self.filters, self.type_services = {}, {}, {}, {}
        self.filter_types = {}
        self.external_filters = False
        # Export targets touched by load-order-dependent extensions must stay
        # UNKNOWN rather than disabling the unaffected native registry.
        self.uncertain_extensions = set()
        self.valid = bool(files)
        self.fingerprint = hashlib.sha256(json.dumps(
            [str(executable), files], ensure_ascii=True).encode()).hexdigest()
        types, filters = {}, {}
        dirty_type_ids = set()
        for name, _modified, _size in files:
            try:
                root = ET.parse(name).getroot()
            except (OSError, ET.ParseError):
                self.valid = False
                continue
            overlay = Path(name).suffix.lower() == '.xcu'
            if overlay and any('org.openoffice.TypeDetection.' in
                               element.get(OOR + 'path', '') for element in root.iter()):
                # Incremental registry updates with unresolved paths cannot be
                # mapped to individual extensions: fail closed for all routes.
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
                    is_types = group.get(OOR + 'name') == 'Types'
                    collection = types if is_types else filters
                    for node in group.findall('node'):
                        identity = node.get(OOR + 'name')
                        if overlay:
                            self.external_filters = True
                            previous = collection.get(identity, {})
                            incoming = properties(node)
                            if is_types:
                                dirty_type_ids.add(identity)
                                self.uncertain_extensions.update(
                                    e.lower() for e in previous.get('Extensions', '').split()
                                    if EXTENSION.fullmatch(e.lower()))
                                self.uncertain_extensions.update(
                                    e.lower() for e in incoming.get('Extensions', '').split()
                                    if EXTENSION.fullmatch(e.lower()))
                            else:
                                dirty_type_ids.update(t for t in (
                                    previous.get('Type'), incoming.get('Type')) if t)
                        if node.get(OOR + 'op') == 'remove':
                            collection.pop(identity, None)
                            continue
                        if node.get(OOR + 'op') == 'replace':
                            collection[identity] = {}
                        collection.setdefault(identity, {}).update(properties(node))
        self.uncertain_type_ids = set(dirty_type_ids)
        for type_id in dirty_type_ids:
            self.uncertain_extensions.update(
                e.lower() for e in types.get(type_id, {}).get('Extensions', '').split()
                if EXTENSION.fullmatch(e.lower()))
        self.types = {e.lower() for item in types.values() for e in item.get('Extensions', '').split()
                      if EXTENSION.fullmatch(e.lower())}
        self.preferred_filters = {}
        for item in types.values():
            for extension in item.get('Extensions', '').split():
                if item.get('PreferredFilter'):
                    self.preferred_filters.setdefault(extension.lower(), set()).add(item['PreferredFilter'])
        for identity, item in filters.items():
            service = item.get('DocumentService')
            flags = set(item.get('Flags', '').split())
            if not service or not flags or 'NOTINSTALLED' in flags:
                continue
            extensions = {e.lower() for e in types.get(item.get('Type'), {}).get('Extensions', '').split()
                          if EXTENSION.fullmatch(e.lower())}
            self.filters[identity] = (service, frozenset(extensions), frozenset(flags))
            self.filter_types[identity] = item.get('Type')
            if 'IMPORT' in flags and item.get('Type'):
                self.type_services.setdefault(item['Type'], set()).add(service)
            for flag, mapping in (('IMPORT', self.imports), ('EXPORT', self.exports)):
                if flag in flags:
                    for extension in extensions:
                        mapping.setdefault(extension, set()).add(service)
        self.valid = self.valid and bool(self.imports) and bool(self.exports)
        self.import_services = frozenset(s for services in self.imports.values() for s in services)
        self.export_candidates = {}
        for name,(service,extensions,flags) in self.filters.items():
            if 'EXPORT' in flags:
                for extension in extensions:
                    self.export_candidates.setdefault((extension,service),set()).add(name)

    def export_filter(self, target, services):
        """Choose only an unambiguous installed default; otherwise retain CLI."""
        if not self.valid or target in self.uncertain_extensions:
            return None
        candidates = set().union(*(self.export_candidates.get((target,service),set()) for service in services))
        preferred = candidates.intersection(self.preferred_filters.get(target, ()))
        if len(preferred) == 1:
            return next(iter(preferred))
        return next(iter(candidates)) if len(candidates) == 1 else None

    def services_for_type(self, type_name, filter_name=None):
        # A verified UNO TypeDetection name, not a file suffix, is mapped
        # to native import services from the same installed FilterFactory.
        if (not self.valid or not isinstance(type_name, str)
                or type_name in self.uncertain_type_ids):
            return ()
        if filter_name is not None:
            item = self.filters.get(filter_name) if isinstance(filter_name, str) else None
            if (not item or 'IMPORT' not in item[2]
                    or self.filter_types.get(filter_name) != type_name):
                return ()
            return (item[0],)
        services = self.type_services.get(type_name, ())
        # A union of possible import services is not a detected model.
        return tuple(services) if len(services) == 1 else ()

    def hint(self, filename, target):
        source_ext = Path(filename).suffix.lower().lstrip('.')
        if source_ext in self.uncertain_extensions:
            return 'undetermined'
        services = self.imports.get(source_ext, set())
        return self.decide(target, services)

    def decide(self, target, services):
        exports = self.exports.get(target, set())
        if not self.valid or target in self.uncertain_extensions:
            return 'undetermined'
        if not exports:
            # Native import/graphic encoders do not imply CLI document export.
            return 'unavailable' if target in self.types else 'undetermined'
        if not services:
            return 'undetermined'
        return 'registered' if exports.intersection(services) else 'unavailable'

    def proof(self, filter_name, target):
        item = self.filters.get(filter_name)
        if (not self.valid or target in self.uncertain_extensions or not item
                or 'EXPORT' not in item[2] or target not in item[1]):
            return None
        return {'documentService': item[0], 'filter': filter_name,
                'registryFingerprint': self.fingerprint, 'basis': 'native_selected_export_filter'}

    def observed_decision(self, target, proof):
        if (proof and proof.get('basis')=='native_preflight_chart_content'
                and proof.get('state')=='detected' and type(proof.get('nativeKind')) is int
                and proof['nativeKind']==4 and proof.get('chartPackage') is True
                and proof.get('registryFingerprint')==self.fingerprint):
            service='com.sun.star.chart2.ChartDocument'
            if proof.get('documentServices')==[service] and service in self.import_services:
                return self.decide(target,{service})
            return 'undetermined'
        if (proof and proof.get('basis') == 'native_preflight_mathml_content'
                and proof.get('state') == 'detected'
                and proof.get('nativeKind') == 4
                and proof.get('mathmlRoot') is True
                and proof.get('registryFingerprint') == self.fingerprint):
            service = 'com.sun.star.formula.FormulaProperties'
            if (proof.get('documentServices') == [service]
                    and service in self.import_services):
                return self.decide(target, {service})
            return 'undetermined'
        if (proof and proof.get('basis') == 'native_preflight_uno_document'
                and proof.get('state') == 'detected' and proof.get('modelLoaded') is True
                and proof.get('registryFingerprint') == self.fingerprint):
            services = proof.get('documentServices')
            installed = self.import_services
            if isinstance(services, list) and services and all(s in installed for s in services):
                return self.decide(target, set(services))
            return 'undetermined'
        if (proof and proof.get('basis') == 'native_preflight_uno_graphic'
                and proof.get('state') == 'detected'
                and proof.get('registryFingerprint') == self.fingerprint
                and type(proof.get('graphicType')) is int
                and proof.get('graphicType') in (1, 2)
                and proof.get('documentServices') == ['com.sun.star.drawing.DrawingDocument']
                and any('com.sun.star.drawing.DrawingDocument' in services
                        for services in self.imports.values())):
            return self.decide(target, {'com.sun.star.drawing.DrawingDocument'})
        if (proof and proof.get('basis') == 'native_preflight_uno_type'
                and proof.get('state') == 'detected'
                and proof.get('registryFingerprint') == self.fingerprint):
            services = self.services_for_type(proof.get('typeName'), proof.get('filterName'))
            if services and sorted(services) == sorted(proof.get('documentServices', ())):
                return self.decide(target, set(services))
            return 'undetermined'
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
