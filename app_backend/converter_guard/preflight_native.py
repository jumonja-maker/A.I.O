"""Import-only native LibreOffice C API, used ONLY in an owned child process.

ABI reference: LibreOffice/core include/LibreOfficeKit/LibreOfficeKit.h.
No export, saveAs, rendering or user-profile access is performed here.
"""
import ctypes as C
import json
import os
from pathlib import Path
import sys


class _Kit(C.Structure):
    _fields_ = [('size', C.c_size_t)] + [(n, C.c_void_p) for n in (
        'destroy', 'load', 'error', 'load_options')]


class _Document(C.Structure):
    _fields_ = [('size', C.c_size_t)] + [(n, C.c_void_p) for n in (
        'destroy', 'save_as', 'document_type')]


def _table(pointer, structure, member):
    address = C.cast(pointer, C.POINTER(C.c_void_p))[0]
    if not address:
        raise ValueError('Missing native table')
    size = C.cast(address, C.POINTER(C.c_size_t))[0]
    if size < getattr(structure, member).offset + C.sizeof(C.c_void_p):
        raise ValueError('Unsupported native ABI')
    table = C.cast(address, C.POINTER(structure)).contents
    if not getattr(table, member):
        raise ValueError('Missing native method')
    return table


def _stage(work, name):
    # The parent reads this fixed vocabulary after timeout; no document
    # names or private paths are emitted. A Windows C call can hang indefinitely.
    with (work / 'stage.txt').open('w', encoding='ascii') as output:
        output.write(name)
        output.flush()
        os.fsync(output.fileno())


def detect(program, profile, source, work):
    _stage(work, 'dll_scope')
    dll_scope = os.add_dll_directory(str(program)) if os.name == 'nt' else None
    names = (('sofficeapp.dll', 'mergedlo.dll') if os.name == 'nt' else
             ('libsofficeapp.dylib', 'libmergedlo.dylib') if sys.platform == 'darwin' else
             ('libsofficeapp.so', 'libmergedlo.so'))
    library = next((program / n for n in names if (program / n).is_file()
                    and (program / n).stat().st_size > 1000), None)
    if library is None:
        return {'state': 'undetermined', 'code': 'PREFLIGHT_API_UNAVAILABLE'}
    _stage(work, 'dll_load')
    native = C.CDLL(str(library), mode=getattr(C, 'RTLD_GLOBAL', 0))
    _stage(work, 'dll_loaded')
    hook = native.libreofficekit_hook_2
    hook.argtypes, hook.restype = [C.c_char_p, C.c_char_p], C.c_void_p
    _stage(work, 'kit_init')
    # This calls the *exported hook* directly from sofficeapp.dll.
    # Its ABI takes the DLL's program directory, not the installation
    # root expected by the higher-level LibreOfficeKitInit.h loader.
    kit = hook(str(program).encode('utf-8'), profile.as_uri().encode('utf-8'))
    _stage(work, 'kit_initialized')
    if not kit:
        return {'state': 'undetermined', 'code': 'PREFLIGHT_INIT_FAILED'}
    # LO 26.8 on NIKITA stalls in documentLoadWithOptions on Windows.
    # The import-only documentLoad ABI is an independent native function;
    # no saveAs/render/export call is made. Profile blocks all macros.
    if os.name == 'nt':
        table = _table(kit, _Kit, 'load')
        load = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_char_p)(table.load)
        _stage(work, 'document_load_basic')
        doc = load(kit, source.as_uri().encode('utf-8'))
    else:
        table = _table(kit, _Kit, 'load_options')
        load = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_char_p, C.c_char_p)(table.load_options)
        _stage(work, 'document_load_options')
        # LOK consumes Batch itself, then passes the remaining string as the
        # import filter's FilterOptions. ReadOnly=true is NOT a LOK option:
        # it becomes invalid CSV parser options, instead of a read-only flag.
        # Batch uses LO's silent interaction mode and native import defaults.
        # Only our private copy is opened; this worker never saves it.
        doc = load(kit, source.as_uri().encode('utf-8'), b'Batch=true')
    _stage(work, 'document_loaded')
    if not doc:
        return {'state': 'undetermined', 'code': 'PREFLIGHT_IMPORT_UNDETERMINED'}
    table = _table(doc, _Document, 'document_type')
    _stage(work, 'document_type')
    kind = C.CFUNCTYPE(C.c_int, C.c_void_p)(table.document_type)(doc)
    _stage(work, 'document_typed')
    return ({'state': 'detected', 'kind': kind} if kind in (0, 1, 2, 3) else
            {'state': 'undetermined', 'code': 'PREFLIGHT_KIND_UNDETERMINED'})


def main(manifest_path):
    manifest = Path(manifest_path).resolve(strict=True)
    work = manifest.parent
    request = json.loads(manifest.read_text(encoding='utf-8'))
    program = Path(request['program']).resolve(strict=True)
    profile = (work / 'profile').resolve(strict=True)
    source = Path(request['source']).resolve(strict=True)
    if not source.is_file() or not program.is_dir():
        raise ValueError('Invalid preflight input')
    try:
        result = detect(program, profile, source, work)
    except (OSError, AttributeError, ValueError, TypeError):
        result = {'state': 'undetermined', 'code': 'PREFLIGHT_API_UNAVAILABLE'}
    part = work / 'result.part'
    with part.open('x', encoding='utf-8') as stream:
        json.dump(result, stream)
        stream.flush()
    part.replace(work / 'result.json')
    _stage(work, 'result_written')
    # All native state belongs to this process. Global office shutdown can
    # await an event loop; OS exit and the parent's ProcessTree release it.
    os._exit(0)


if __name__ == '__main__':
    main(sys.argv[1])
