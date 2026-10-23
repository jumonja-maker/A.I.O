"""Private companion files for one HTML source; no shared tool state."""
from pathlib import Path
import codecs
import re
import time
import unicodedata
from urllib.parse import unquote, urlsplit

from .errors import GuardError
from .result_store import path_is_link


def resource_path(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or '\\' in value or value.startswith('/')):
        raise GuardError('RESOURCE_PATH', 'Недопустимый относительный путь ресурса HTML.')
    value = unicodedata.normalize('NFC', value)
    for part in value.split('/'):
        if (part in ('', '.', '..') or part.endswith((' ', '.'))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', part.split('.')[0], re.I)):
            raise GuardError('RESOURCE_PATH', 'Недопустимое имя ресурса HTML.')
    return value


def resource_manifest(entries, source_name):
    if not isinstance(entries, list) or not entries:
        raise GuardError('RESOURCE_METADATA', 'Выберите папку ресурсов HTML.')
    result, seen = {}, {unicodedata.normalize('NFC', source_name).casefold()}
    for entry in entries:
        if not isinstance(entry, dict):
            raise GuardError('RESOURCE_METADATA', 'Некорректное описание ресурса.')
        path = resource_path(entry.get('path'))
        size = entry.get('size')
        if type(size) is not int or size < 0:
            raise GuardError('RESOURCE_METADATA', 'Некорректный размер ресурса.')
        if path.casefold() in seen:
            raise GuardError('RESOURCE_COLLISION', 'Ресурс заменяет исходник или повторяет другое имя.')
        seen.add(path.casefold())
        result[path] = {'size':size, 'ready':False}
    ordered = sorted(seen)
    if any(b.startswith(a + '/') for a, b in zip(ordered, ordered[1:])):
        raise GuardError('RESOURCE_COLLISION', 'Путь ресурса одновременно обозначает файл и папку.')
    return result


def inspect_dependencies(source, parser_type, *, cancel=None, missing=None, seconds=2):
    if source.suffix.lower() not in {'.html', '.htm', '.xhtml'}:
        return None
    root = source.parent.resolve()
    pending, seen, absent = [source], set(), []
    scheduled = {source}
    budget_exhausted = False
    until = time.monotonic() + seconds
    while pending:
        if time.monotonic() >= until or len(seen) >= 128:
            return 'HTML_PREFLIGHT_UNDETERMINED'
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)

        def resolve(value):
            nonlocal budget_exhausted
            parsed = urlsplit(value)
            path = unquote(parsed.path)
            if parsed.scheme or parsed.netloc or path.startswith(('/', '\\')) or '\\' in path:
                valid = False
            else:
                target = current.parent / path
                try:
                    valid = target.resolve(strict=True).is_relative_to(root) and target.is_file()
                    for part in (target, *target.parents):
                        if part == root.parent:
                            break
                        if path_is_link(part):
                            valid = False
                    if valid and target.suffix.lower() in ('.css', '.svg', '.html', '.htm', '.xhtml'):
                        resolved = target.resolve()
                        if resolved not in scheduled:
                            if len(scheduled) >= 128:
                                budget_exhausted = True
                            else:
                                scheduled.add(resolved)
                                pending.append(resolved)
                except OSError:
                    valid = False
            if not valid and len(absent) < 32 and value not in absent:
                absent.append(value[:2048])
            return valid

        parser = parser_type(resolve)
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        first, css_tail = True, ''
        try:
            with current.open('rb') as stream:
                while block := stream.read(128 * 1024):
                    if cancel and cancel.is_set():
                        raise GuardError('CANCELLED', 'Проверка ресурсов отменена.', stage='preflight')
                    if time.monotonic() >= until:
                        return 'HTML_PREFLIGHT_UNDETERMINED'
                    if first:
                        first = False
                        declared = re.search(br'(?is)<meta\b[^>]{0,1024}\bcharset\s*=\s*["\']?([a-z0-9_-]+)', block[:4096])
                        encoding = ('utf-16' if block.startswith((b'\xff\xfe', b'\xfe\xff')) else
                                    declared[1].decode('ascii') if declared else 'utf-8')
                        if encoding.lower() in ('utf-7', 'unicode-1-1-utf-7'):
                            return 'HTML_PREFLIGHT_UNDETERMINED'
                        decoder = codecs.getincrementaldecoder(encoding)('strict')
                    text = decoder.decode(block)
                    if current.suffix.lower() == '.css':
                        parser._scan_css(css_tail + text)
                        css_tail = (css_tail + text)[-4096:]
                    else:
                        parser.feed(text)
                    if budget_exhausted or parser.undetermined or len(parser.rawdata) > 512 * 1024:
                        return 'HTML_PREFLIGHT_UNDETERMINED'
                final = decoder.decode(b'', final=True)
                if current.suffix.lower() == '.css':
                    parser._scan_css(css_tail + final)
                else:
                    parser.feed(final)
                    parser.close()
        except (UnicodeError, LookupError, ValueError):
            return 'HTML_PREFLIGHT_UNDETERMINED'
    if missing is not None:
        missing.extend(absent)
    return 'HTML_LOCAL_RESOURCES_NOT_UPLOADED' if absent else None
