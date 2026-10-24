"""Cancellable local stdio worker using the bundled office's own Python/UNO.

One process pair per batch, no TCP listener and no application dependencies.
Each document is closed after the operation; model reuse is deliberately not
enabled until cross-format equivalence is proved on the owner's Windows LO.
"""
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import threading
import time

from ..native_process import ProcessTree
from .errors import GuardError
from .native_logs import NativeLogs

WORKER_SOURCE = r'''
import json, os, sys, time, subprocess
from pathlib import Path

PREFIX = 'AIO_GUARD_RPC '
def send(value):
    print(PREFIX + json.dumps(value, ensure_ascii=True), flush=True)

def dbf_mixed_columns(sheet, area):
    """Find columns native dBase's first-record type inference would truncate."""
    first = tuple(sheet.getCellRangeByPosition(0, 0, area.EndColumn, 0).getDataArray()[0])
    header = bool(first) and all(isinstance(value, str) and value != '' for value in first)
    kinds = [set() for _ in first]
    for row in range(1 if header else 0, area.EndRow + 1, 128):
        last_row = min(area.EndRow, row + 127)
        for column in range(0, area.EndColumn + 1, 128):
            last_column = min(area.EndColumn, column + 127)
            values = sheet.getCellRangeByPosition(column, row, last_column, last_row).getDataArray()
            for record in values:
                for offset, value in enumerate(record):
                    if value is None:
                        kinds[column + offset].add('text')
                    elif isinstance(value, str):
                        if value:
                            kinds[column + offset].add('text')
                    else:
                        kinds[column + offset].add('number')
    mixed = []
    for column, observed in enumerate(kinds):
        declared = str(first[column]).split(',')[1:2] if header else []
        if len(observed) > 1 or ('text' in observed and declared and declared[0].strip().upper() in ('N', 'D', 'L')):
            mixed.append(column)
    return {'header': header, 'originalHeaders': list(first) if header else None, 'mixed': mixed}

def export_table_sheets(model, desktop, prop, target, export_options):
    """One native legacy file per sheet, without approving a lossy dialog.

    DIF's native exporter always starts at sheet zero; it warns and drops all
    later sheets. Copy each sheet to a private Calc model instead. ImportSheet
    preserves number formats; restoring the already evaluated data array keeps
    formulas from changing when references to other sheets are absent. No
    calculation, update-links or macro permission is enabled on the copy.
    """
    sheets = model.getSheets()
    count = sheets.getCount()
    is_dbf = any(p.Name == 'FilterName' and p.Value == 'dBase' for p in export_options)
    profiles = []
    for index in range(count):
        sheet = sheets.getByIndex(index)
        cursor = sheet.createCursor()
        cursor.gotoEndOfUsedArea(False)
        area = cursor.getRangeAddress()
        profiles.append((area, dbf_mixed_columns(sheet, area) if is_dbf else {'mixed': []}))
    if count <= 1 and not profiles[0][1]['mixed']:
        model.storeToURL(target.as_uri(), tuple(export_options))
        return [target]
    outputs = []
    for index in range(count):
        source_sheet = sheets.getByIndex(index)
        temporary = desktop.loadComponentFromURL('private:factory/scalc', '_blank', 0,
            (prop('Hidden', True), prop('Silent', True), prop('MacroExecutionMode', 0),
             prop('UpdateDocMode', 0), prop('InteractionHandler',
                 next(p.Value for p in export_options if p.Name == 'InteractionHandler'))))
        if temporary is None:
            raise RuntimeError('LEGACY_SHEET_COPY_UNAVAILABLE')
        try:
            temporary.enableAutomaticCalculation(False)
            copied_sheets = temporary.getSheets()
            # Native Calc forbids duplicate sheet names. Rename its factory
            # defaults before importing a source commonly called "Sheet1".
            for default_index in range(copied_sheets.getCount()):
                default = copied_sheets.getByIndex(default_index)
                if hasattr(default, 'setName'):
                    default.setName('aio_empty_' + str(time.monotonic_ns()) + '_' + str(default_index))
            imported = copied_sheets.importSheet(model, source_sheet.getName(), 0)
            copied = copied_sheets.getByIndex(imported)
            keep_name = copied.getName()
            for name in tuple(copied_sheets.getElementNames()):
                if name != keep_name:
                    copied_sheets.removeByName(name)
            area, profile = profiles[index]
            path = target if index == 0 else target.with_name(
                target.stem + '__sheet_%03d' % (index + 1) + target.suffix)
            mapping = None
            if profile['mixed']:
                mapping = path.with_name(path.name + '.aio-columns.jsonl').open('x', encoding='utf-8')
                mapping.write(json.dumps({'schemaVersion': 1, 'format': 'dBase',
                    'sheetName': source_sheet.getName(), 'sourceRows': area.EndRow + 1,
                    'sourceColumns': area.EndColumn + 1, 'sourceHasHeader': profile['header'],
                    'originalHeaders': profile['originalHeaders'], 'memoColumns': profile['mixed'],
                    'numericColumnsRemainTyped': True, 'warning': 'NATIVE_DBF_MIXED_COLUMN_TEXT'},
                    ensure_ascii=True, allow_nan=False) + '\n')
            # Bounded bridge messages, including on very wide worksheets.
            for row in range(0, area.EndRow + 1, 128):
                last_row = min(area.EndRow, row + 127)
                for column in range(0, area.EndColumn + 1, 128):
                    last_column = min(area.EndColumn, column + 127)
                    values = source_sheet.getCellRangeByPosition(
                        column, row, last_column, last_row).getDataArray()
                    # XCellRangeData returns formula errors as void (None).
                    # Preserve their displayed error string rather than
                    # turning those cells into blanks in the value snapshot.
                    values = tuple(tuple(value if value is not None else
                        source_sheet.getCellByPosition(column + offset, row + row_offset).getString()
                        for offset, value in enumerate(record))
                        for row_offset, record in enumerate(values))
                    if profile['mixed']:
                        changed = [list(record) for record in values]
                        for row_offset, record in enumerate(values):
                            source_row = row + row_offset
                            if profile['header'] and source_row == 0:
                                continue
                            for source_column in profile['mixed']:
                                if not column <= source_column <= last_column:
                                    continue
                                offset = source_column - column
                                original = record[offset]
                                text = source_sheet.getCellByPosition(source_column, source_row).getString()
                                changed[row_offset][offset] = text
                                mapping.write(json.dumps({'row': source_row, 'column': source_column,
                                    'sourceType': 'string' if isinstance(original, str) else 'number',
                                    'sourceValue': original, 'exportedText': text},
                                    ensure_ascii=True, allow_nan=False) + '\n')
                        values = tuple(tuple(record) for record in changed)
                    copied.getCellRangeByPosition(
                        column, row, last_column, last_row).setDataArray(values)
            if profile['mixed']:
                if not profile['header']:
                    copied.getRows().insertByIndex(0, 1)
                    for column in range(area.EndColumn + 1):
                        copied.getCellByPosition(column, 0).setString('FIELD' + str(column + 1))
                for column in profile['mixed']:
                    header_name = (str(profile['originalHeaders'][column]).split(',')[0]
                                   if profile['header'] else 'FIELD' + str(column + 1))
                    copied.getCellByPosition(column, 0).setString(header_name + ',M')
                mapping.close()
                mapping = None
            temporary.storeToURL(path.as_uri(), tuple(export_options))
            outputs.append(path)
        finally:
            if 'mapping' in locals() and mapping is not None:
                mapping.close()
            if hasattr(temporary, 'close'):
                temporary.close(True)
            else:
                temporary.dispose()
    return outputs


def export_draw_svm(model, remote, prop, target):
    """Use LO's graphic encoder per page, never its import-only doc filter."""
    pages = model.getDrawPages()
    if pages.getCount() < 1:
        raise RuntimeError('NATIVE_DRAW_PAGES_MISSING')
    exporter = remote.ServiceManager.createInstanceWithContext(
        'com.sun.star.drawing.GraphicExportFilter', remote)
    if exporter is None or not exporter.supportsMimeType('image/x-svm'):
        raise RuntimeError('NATIVE_GRAPHIC_SVM_UNAVAILABLE')
    outputs = []
    for index in range(pages.getCount()):
        path = target if index == 0 else target.with_name(
            target.stem + '__page_%03d' % (index + 1) + target.suffix)
        exporter.setSourceDocument(pages.getByIndex(index))
        if not exporter.filter((prop('URL', path.as_uri()), prop('MediaType', 'image/x-svm'),
                                prop('FilterName', 'svm'), prop('Overwrite', False))):
            raise RuntimeError('NATIVE_GRAPHIC_EXPORT_FAILED')
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise RuntimeError('NATIVE_GRAPHIC_OUTPUT_MISSING')
        with path.open('rb') as stream:
            if stream.read(6) != b'VCLMTF':
                raise RuntimeError('NATIVE_GRAPHIC_SVM_INVALID')
        outputs.append(path)
    return outputs


def inspect_native_text_export(remote, text_document, services):
    """Resolve this one Writer export against LO's final live configuration.

    Static XCD/XCU merge order can leave a text extension uncertain. Runtime
    FilterFactory and TypeDetection are authoritative for this running office;
    no source detection or export is performed by this capability query.
    """
    writer = 'com.sun.star.text.TextDocument'
    web = 'com.sun.star.text.WebDocument'
    if text_document is not True or not set(services).intersection((writer, web)):
        return {}, 'text_document_not_confirmed'
    try:
        factory = remote.ServiceManager.createInstanceWithContext(
            'com.sun.star.document.FilterFactory', remote)
        if factory is None or not factory.hasByName('Text (encoded)'):
            return {}, 'filter_missing'
        properties = {p.Name: p.Value for p in factory.getByName('Text (encoded)')
                      if p.Name in ('Flags', 'DocumentService', 'Type')}
        if properties.get('DocumentService') != writer:
            return {}, 'document_service_mismatch'
        # The FilterFactory UNO Flags property is the native integer bitmask:
        # IMPORT=1, EXPORT=2. Never reinterpret strings as native permissions.
        flags = properties.get('Flags')
        if type(flags) is not int or not flags & 2:
            return {}, 'export_flag_missing'
        type_name = properties.get('Type')
        if not isinstance(type_name, str) or not type_name or len(type_name) > 256:
            return {}, 'type_missing'
        detector = remote.ServiceManager.createInstanceWithContext(
            'com.sun.star.document.TypeDetection', remote)
        if detector is None or not detector.hasByName(type_name):
            return {}, 'type_missing'
        types = {p.Name: p.Value for p in detector.getByName(type_name) if p.Name == 'Extensions'}
        extensions = types.get('Extensions')
        if (not isinstance(extensions, (tuple, list)) or len(extensions) > 128
                or not any(isinstance(value, str) and value.lower() == 'txt' for value in extensions)):
            return {}, 'extensions_mismatch'
        return {'txt': 'Text (encoded)'}, 'verified'
    except Exception:
        return {}, 'runtime_registry_unavailable'


def main(boot_name):
    boot = json.loads(Path(boot_name).read_text(encoding='utf-8'))
    until = time.monotonic() + 20
    while not Path(boot['start']).is_file():
        if time.monotonic() > until:
            raise RuntimeError('Job handshake unavailable')
        time.sleep(.02)
    import uno, unohelper
    from com.sun.star.task import XInteractionHandler
    class Abort(unohelper.Base, XInteractionHandler):
        blocked = False
        request_type = None
        error_code = None
        def handle(self, request):
            self.blocked = True
            try:
                payload = request.getRequest()
                name = type(payload).__name__
                if len(name) <= 128 and all(c.isalnum() or c in '._' for c in name):
                    self.request_type = name
                for field in ('ErrCode', 'ErrorCode'):
                    value = getattr(payload, field, None)
                    if type(value) is int:
                        self.error_code = value
                        break
            except Exception:
                pass
            for item in request.getContinuations():
                abort = item.queryInterface(uno.getTypeByName('com.sun.star.task.XInteractionAbort'))
                if abort is not None:
                    abort.select(); return
    def prop(name, value):
        p = uno.createUnoStruct('com.sun.star.beans.PropertyValue')
        p.Name, p.Value = name, value
        return p
    program = Path(boot['program'])
    subprocess.Popen([boot.get('executable',str(program / 'soffice.exe')),
        '-env:UserInstallation=' + Path(boot['profile']).as_uri(), '--headless', '--nodefault',
        '--nologo', '--norestore', '--accept=pipe,name=' + boot['pipe'] + ';urp;StarOffice.ServiceManager'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver', local)
    remote = None
    until = time.monotonic() + 40
    while time.monotonic() < until:
        try:
            remote = resolver.resolve('uno:pipe,name=' + boot['pipe'] + ';urp;StarOffice.ComponentContext')
            break
        except Exception:
            time.sleep(.05)
    if remote is None:
        raise RuntimeError('Native office connection unavailable')
    desktop = remote.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', remote)
    send({'id':'ready','ok':True})
    for raw in sys.stdin:
        req = json.loads(raw)
        model, result, stage = None, None, 'import'
        operation_tick = time.monotonic()
        import_tick = export_tick = export_handler = handler = None
        timings = {'source':'uno_document'}
        request_id = req['id']
        try:
            source = Path(req['source']).resolve(strict=True)
            send({'id':request_id,'stage':'import'})
            import_tick = time.monotonic()
            # Loading already invokes native detection. A separate deep query
            # can read/decompress a complex source twice before one preflight.
            # Only the live model and its actual import arguments admit routes.
            handler = Abort()
            options = [prop('Hidden', True), prop('ReadOnly', True), prop('Silent', True),
                       prop('MacroExecutionMode', 0), prop('UpdateDocMode', 0), prop('InteractionHandler', handler)]
            # Inspection uses native automatic import. Forcing a guessed text
            # filter here could turn an unsupported binary container into junk.
            chosen = req.get('importFilter')
            if chosen:
                options.append(prop('FilterName', chosen))
                if req.get('importOptions'):
                    options.append(prop('FilterOptions', req['importOptions']))
            model = desktop.loadComponentFromURL(source.as_uri(), '_blank', 0, tuple(options))
            timings['importMs'] = round((time.monotonic()-import_tick)*1000)
            if model is None:
                result = {'id':request_id,'ok':False,'code':'PREFLIGHT_INTERACTION_REQUIRED'
                          if handler.blocked else 'SOURCE_IMPORT_FAILED','stage':'import'}
            else:
                services = [s for s in req['services'] if model.supportsService(s)]
                arguments = {p.Name:p.Value for p in model.getArgs()
                             if p.Name in ('FilterName', 'TypeName')}
                actual_filter = arguments.get('FilterName')
                if req.get('expectedSourceServices') and not set(services).intersection(req['expectedSourceServices']):
                    result = {'id':request_id,'ok':False,'code':'SOURCE_MODEL_MISMATCH','stage':'import'}
                elif req['operation'] == 'inspect':
                    try:
                        text_document = bool(model.queryInterface(
                            uno.getTypeByName('com.sun.star.text.XTextDocument')))
                    except Exception:
                        text_document = False
                    result = {'id':request_id,'ok':True,'services':services,
                              'typeName':arguments.get('TypeName'), 'filterName':actual_filter,
                              'nativeTextDocument':text_document}
                    text_exports, text_export_reason = inspect_native_text_export(
                        remote, text_document, services)
                    if text_exports:
                        result['nativeExportFilters'] = text_exports
                    if text_document:
                        result['nativeExportInspection'] = {'txt': text_export_reason}
                    if 'com.sun.star.drawing.DrawingDocument' in services:
                        try:
                            result['drawPages'] = model.getDrawPages().getCount()
                        except Exception:
                            pass
                        try:
                            graphic = remote.ServiceManager.createInstanceWithContext(
                                'com.sun.star.drawing.GraphicExportFilter', remote)
                            if graphic is not None and graphic.supportsMimeType('image/x-svm'):
                                result['nativeGraphicExports'] = ['svm']
                        except Exception:
                            pass  # A document route does not require a graphic encoder.
                elif not set(services).intersection(req['expectedServices']):
                    result = {'id':request_id,'ok':False,'code':'NATIVE_MODEL_CHANGED','stage':'import'}
                elif req.get('expectedImportFilter') and actual_filter != req['expectedImportFilter']:
                    result = {'id':request_id,'ok':False,'code':'NATIVE_IMPORT_FILTER_CHANGED','stage':'import'}
                elif req.get('requiredDrawPages') is not None and (
                        'com.sun.star.drawing.DrawingDocument' not in services or
                        model.getDrawPages().getCount() != req['requiredDrawPages']):
                    result = {'id':request_id,'ok':False,'code':'NATIVE_MODEL_CHANGED','stage':'import'}
                else:
                    stage = 'export'
                    send({'id':request_id,'stage':'export'})
                    target = Path(req['output']) / (source.stem + '.' + req['format'])
                    export_handler = Abort()
                    export_options = [prop('FilterName', req['exportFilter']), prop('Overwrite', False),
                                      prop('Silent', True), prop('InteractionHandler', export_handler)]
                    if req.get('exportOptions') is not None:
                        export_options.append(prop('FilterOptions', req['exportOptions']))
                    export_tick = time.monotonic()
                    exported_paths = [target]
                    try:
                        if req['format'] == 'txt' and req['exportFilter'] == 'Text (encoded)':
                            try:
                                native_text = bool(model.queryInterface(
                                    uno.getTypeByName('com.sun.star.text.XTextDocument')))
                            except Exception:
                                native_text = False
                            text_exports, text_reason = inspect_native_text_export(remote, native_text, services)
                            if text_exports.get('txt') != 'Text (encoded)':
                                raise RuntimeError('NATIVE_TEXT_EXPORT_CAPABILITY_CHANGED:' + text_reason)
                        if (req.get('graphicExport') == 'svm' and req['format'] == 'svm'
                                and 'com.sun.star.drawing.DrawingDocument' in services):
                            exported_paths = export_draw_svm(model, remote, prop, target)
                        elif (req['exportFilter'] in ('DIF', 'dBase')
                                and 'com.sun.star.sheet.SpreadsheetDocument' in services):
                            exported_paths = export_table_sheets(model, desktop, prop, target, export_options)
                        else:
                            model.storeToURL(target.as_uri(), tuple(export_options))
                    finally:
                        timings['exportMs'] = round((time.monotonic()-export_tick)*1000)
                    present = all(p.is_file() and not p.is_symlink() and p.stat().st_size > 0
                                  for p in exported_paths)
                    # Result paths stay on disk. Thousands of HTML sidecars
                    # must not overflow the small control-message channel.
                    result = {'id':request_id,'ok':present and not export_handler.blocked,
                              'primary':target.name,'stage':'export',
                              'code':('EXPORT_INTERACTION_REQUIRED' if export_handler.blocked else
                                      'ENGINE_OUTPUT_MISSING' if not present else None)}
                    if req['exportFilter'] == 'dBase' and list(Path(req['output']).glob('*.aio-columns.jsonl')):
                        result['warnings'] = ['NATIVE_DBF_MIXED_COLUMN_TEXT']
        except Exception as error:
            result = {'id':request_id,'ok':False,'code':('PREFLIGHT_INTERACTION_REQUIRED'
                      if stage == 'import' and handler is not None and handler.blocked else
                      'SOURCE_IMPORT_FAILED' if stage == 'import' else
                      'EXPORT_INTERACTION_REQUIRED' if export_handler is not None and export_handler.blocked
                      else 'ENGINE_FAILED'),
                      'stage':stage,'diagnostic':str(error)[-4096:]}
        finally:
            interaction = export_handler if export_handler is not None and export_handler.blocked else handler
            if result is not None and interaction is not None and interaction.blocked:
                result['interaction'] = {'requestType': interaction.request_type,
                                         'errorCode': interaction.error_code}
            if import_tick is not None and 'importMs' not in timings:
                timings['importMs'] = round((time.monotonic()-import_tick)*1000)
            if model is not None:
                # A successful storeToURL is a commit point, independent from
                # model.close(). The controller can recover only these outputs
                # after killing its owned process on a close-only timeout.
                if result is not None and result.get('ok') and stage == 'export':
                    send({'id':request_id,'nativeResult':dict(result, timings=dict(timings))})
                send({'id':request_id,'stage':'close'})
                close_tick = time.monotonic()
                try:
                    if hasattr(model,'close'): model.close(True)
                    else: model.dispose()
                except Exception:
                    result['recycle'] = True
                finally:
                    timings['closeMs'] = round((time.monotonic()-close_tick)*1000)
            timings['operationMs'] = round((time.monotonic()-operation_tick)*1000)
            result['timings'] = timings
        send(result)
        if result.get('recycle'):
            break

if __name__ == '__main__':
    try: main(sys.argv[1])
    except Exception:
        send({'id':'ready','ok':False,'code':'UNO_SESSION_UNAVAILABLE'})
        sys.exit(1)
'''


class UnoSession:
    def __init__(self, executable, workspace, prepare_profile, max_tasks=128):
        self.executable, self.workspace = Path(executable), Path(workspace)
        self.prepare_profile, self.max_tasks = prepare_profile, max_tasks
        self.tree = self.logs = self.reader = None
        self.completed = self.starts = 0
        self.responses = queue.Queue(maxsize=8)
        self.profile = self.pipe = None
        self.disabled = False
        self.import_timeout, self.close_timeout = 60, 15

    def _read(self, stream, responses=None):
        # Bind to this generation; a late dying reader must not feed a newly
        # started worker's queue after cancellation or recycling.
        responses = self.responses if responses is None else responses
        try:
            while line := stream.readline(65537):
                if len(line) > 65536:
                    break
                if not line.startswith(b'AIO_GUARD_RPC '):
                    continue
                try:
                    responses.put_nowait(json.loads(line[len(b'AIO_GUARD_RPC '):]))
                except (ValueError, queue.Full):
                    break
        finally:
            stream.close()

    def _receive(self, identity, cancel, timeout, on_stage=lambda stage:None, *, stage_budgets=None):
        until = None if timeout is None else time.monotonic() + timeout
        stage, sequence, committed = 'import', -1, None
        budgets = stage_budgets or {}
        stage_until = (time.monotonic() + budgets['import']) if 'import' in budgets else None
        while True:
            if cancel.is_set():
                raise GuardError('CANCELLED','Операция отменена.',stage='engine')
            now = time.monotonic()
            if (until is not None and now >= until) or (stage_until is not None and now >= stage_until):
                if stage == 'close' and committed is not None:
                    return {**committed, 'recycle':True, 'nativeCloseInterrupted':True}
                raise GuardError('ENGINE_TIMEOUT','Превышено время операции LibreOffice.',
                                 stage=stage if stage_budgets else 'engine')
            try:
                result = self.responses.get(timeout=.05)
            except queue.Empty:
                if self.reader is not None and not self.reader.is_alive():
                    raise GuardError('UNO_PROTOCOL','Поток ответов LibreOffice прерван.',stage='engine')
                if self.tree.process.poll() is not None:
                    raise GuardError('UNO_SESSION_UNAVAILABLE','Рабочий процесс LibreOffice завершился.',
                                     stage='launch' if identity == 'ready' else 'engine')
                continue
            if not isinstance(result,dict) or result.get('id') != identity:
                raise GuardError('UNO_PROTOCOL','Получен ответ другого задания.',stage='engine')
            if 'nativeResult' in result:
                value = result['nativeResult']
                if (stage != 'export' or not isinstance(value, dict) or value.get('id') != identity
                        or value.get('ok') is not True or value.get('stage') != 'export'):
                    raise GuardError('UNO_PROTOCOL','Недопустимое подтверждение экспорта.',stage='engine')
                committed = value
                continue
            if 'stage' in result and 'ok' not in result:
                next_stage = result['stage']
                order = {'import':0, 'export':1, 'close':2}.get(next_stage)
                if order is None or order < sequence:
                    raise GuardError('UNO_PROTOCOL','Недопустимая последовательность стадий.',stage='engine')
                if order > sequence:
                    # Duplicate stage notifications cannot renew a deadline.
                    first_import = order == 0 and sequence < 0
                    sequence, stage = order, next_stage
                    if not first_import:
                        stage_until = time.monotonic() + budgets[stage] if stage in budgets else None
                    on_stage(stage)
                continue
            return result

    def ensure_started(self, cancel, timeout=45):
        if self.tree and (self.completed >= self.max_tasks or self.tree.process.poll() is not None):
            self.recycle()
        if self.tree:
            return
        self.workspace.mkdir(parents=True,exist_ok=True)
        self.profile = self.workspace / secrets.token_hex(8)
        self.prepare_profile(self.profile)
        self.pipe = 'aio_guard_' + secrets.token_hex(16)
        worker, boot = self.workspace/'worker.py', self.workspace/'boot.json'
        worker.write_text(WORKER_SOURCE,encoding='utf-8')
        start = self.workspace/'start.token';start.unlink(missing_ok=True)
        boot.write_text(json.dumps({'program':str(self.executable.parent),'executable':str(self.executable),
                                   'profile':str(self.profile.resolve()),
                                   'pipe':self.pipe,'start':str(start)}),encoding='utf-8')
        environment = dict(os.environ)
        environment.pop('PYTHONHOME',None);environment.pop('PYTHONPATH',None)
        environment['PYTHONNOUSERSITE']='1'
        self.responses = queue.Queue(maxsize=8)
        self.tree = ProcessTree([str(self.executable.parent/'python.exe'),str(worker),str(boot)],
            cwd=self.workspace,env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.logs = NativeLogs(self.tree.process,capture_stdout=False)
        self.reader = threading.Thread(target=self._read,args=(self.tree.process.stdout,self.responses),daemon=True,name='aio-uno-response')
        self.reader.start()
        start.write_text('ready',encoding='ascii')
        self.starts += 1
        try:
            response = self._receive('ready',cancel,timeout)
        except GuardError as error:
            if error.code in ('CANCELLED','ENGINE_TIMEOUT'):
                raise
            raise GuardError('UNO_SESSION_UNAVAILABLE','Не удалось запустить нативный UNO.',stage='launch') from error
        if not response.get('ok'):
            raise GuardError('UNO_SESSION_UNAVAILABLE','Нативный UNO недоступен.',stage='launch')
        self.completed = 0

    def request(self, value, *, cancel, timeout, on_stage=lambda stage:None):
        try:
            tick = time.monotonic()
            self.ensure_started(cancel,min(45,timeout) if timeout is not None else 45)
            remaining = None if timeout is None else max(.001, timeout-(time.monotonic()-tick))
            identity = secrets.token_hex(16)
            self.tree.process.stdin.write((json.dumps({**value,'id':identity},ensure_ascii=True)+'\n').encode())
            self.tree.process.stdin.flush()
            # Inspection already has its explicit preflight budget. Exports
            # additionally bound import/close so an ODC cannot occupy every
            # remaining target for the full 30-minute file budget.
            budgets = ({'import':value.get('importTimeout', self.import_timeout),
                        'close':self.close_timeout} if value.get('operation') == 'export' else None)
            result = self._receive(identity,cancel,remaining,on_stage,stage_budgets=budgets)
            self.completed += 1
            if result.get('recycle'):
                self.recycle()
            return result
        except BaseException:
            self.recycle()
            raise

    def recycle(self):
        tree, reader, logs, profile = self.tree, self.reader, self.logs, self.profile
        self.tree = self.reader = self.logs = self.profile = self.pipe = None
        try:
            # Terminate the owned process before flushing/closing its stdin.
            # BrokenPipeError previously skipped every remaining cleanup step.
            if tree:
                tree.close()
        finally:
            try:
                if tree and tree.process.stdin:
                    try:
                        tree.process.stdin.close()
                    except (OSError, ValueError):
                        pass
            finally:
                try:
                    if reader:
                        reader.join(timeout=1)
                    if logs:
                        logs.finish()
                finally:
                    if profile:
                        shutil.rmtree(profile,ignore_errors=True)

    def __enter__(self):return self
    def __exit__(self,*_):
        self.recycle();shutil.rmtree(self.workspace,ignore_errors=True)

