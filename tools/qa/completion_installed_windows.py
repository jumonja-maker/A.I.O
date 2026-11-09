"""Install/run/save/close/uninstall acceptance on a disposable Windows CI runner.

Drives the ordinary installed EXE over loopback DevTools. Native Save is completed
through owned Windows dialog controls, not a product hook or QFileDialog patch.
Never runs on a real user PC; no release upload or publication is performed.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.qa.completion_frozen_desktop import DevTools, _close_windows

APP_ID = '{C1B5474A-4A1D-42EC-B2CD-A57A2011F34E}'
SOURCE_MANIFEST = 'AIO-install-source.json'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def payload_path(root, relative):
    if not isinstance(relative, str) or '\\' in relative or ':' in relative:
        raise ValueError('Invalid payload path')
    path = PurePosixPath(relative)
    if path.is_absolute() or PureWindowsPath(relative).is_absolute() or any(
            part in ('', '.', '..') or part.endswith((' ', '.')) for part in path.parts):
        raise ValueError('Payload path escapes the installation')
    if not path.parts or path.as_posix() != relative:
        raise ValueError('Empty payload path')
    target = Path(root).joinpath(*path.parts)
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != Path(root).parent):
        raise ValueError('Linked payload path')
    return target


def load_build_receipt(path, expected_sha):
    if not re.fullmatch(r'[0-9a-f]{40}', expected_sha):
        raise ValueError('Expected source SHA must have40 lowercase hex characters')
    receipt = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (receipt.get('schema') != 1 or receipt.get('status') != 'PASS'
            or receipt.get('sourceRevision') != expected_sha or receipt.get('appId') != APP_ID
            or receipt.get('installedManifestFilename') != SOURCE_MANIFEST):
        raise ValueError('Installer receipt is not a completed build for the expected source/AppID')
    uuid.UUID(receipt['appId'].strip('{}'))
    installer = Path(receipt['installer']['path']).resolve(strict=True)
    if installer.suffix.lower() != '.exe' or not installer.is_file() or digest(installer) != receipt['installer']['sha256']:
        raise ValueError('Installer executable SHA-256 differs from the build receipt')
    files = receipt.get('payload', {}).get('files')
    if not isinstance(files, list) or not files or len(files) != receipt['payload'].get('fileCount'):
        raise ValueError('Missing complete payload inventory')
    seen = set()
    for item in files:
        name = item.get('path')
        payload_path(Path('/payload-validation'), name)
        if name.casefold() in seen or not re.fullmatch(r'[0-9a-f]{64}', item.get('sha256', '')):
            raise ValueError('Duplicate or invalid payload entry')
        if type(item.get('size')) is not int or item['size'] < 0:
            raise ValueError('Invalid payload size')
        seen.add(name.casefold())
    return receipt, installer


def verify_installed_payload(root, receipt):
    root = Path(root)
    installed_manifest = root / SOURCE_MANIFEST
    if digest(installed_manifest) != receipt.get('installedManifestSha256'):
        raise AssertionError('Installed source receipt bytes differ from the reviewed build')
    manifest = json.loads(installed_manifest.read_text(encoding='utf-8-sig'))
    if manifest.get('sourceRevision') != receipt['sourceRevision'] or manifest.get('appId') != APP_ID:
        raise AssertionError('Installed source identity differs from the reviewed installer')
    actual_files = manifest.get('payload', {}).get('files')
    if actual_files != receipt['payload']['files']:
        raise AssertionError('Installed source inventory differs from the reviewed installer')
    total = 0
    for item in actual_files:
        target = payload_path(root, item['path'])
        if not target.is_file() or target.stat().st_size != item['size'] or digest(target) != item['sha256']:
            raise AssertionError('Installed payload mismatch: ' + item['path'])
        total += item['size']
    source_file = root / '_internal/build-info/aio-source-revision.txt'
    if source_file.read_text(encoding='ascii').strip() != receipt['sourceRevision']:
        raise AssertionError('Installed frozen build identity differs')
    return {'filesVerified': len(actual_files), 'bytesVerified': total,
            'installedSourceManifestSha256': digest(installed_manifest), 'allPayloadHashesMatch': True}


def require_disposable_output(output):
    if os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_OS') != 'Windows':
        raise RuntimeError('This install/uninstall acceptance runs only on a disposable GitHub Windows runner')
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    output = Path(output).resolve()
    if output == temporary or temporary not in output.parents or output.exists():
        raise ValueError('Acceptance output must be a new child directory of RUNNER_TEMP')
    return output


def registrations():
    import winreg
    key_name = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\' + APP_ID + '_is1'
    result = []
    for hive_name, hive in (('HKCU', winreg.HKEY_CURRENT_USER), ('HKLM', winreg.HKEY_LOCAL_MACHINE)):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                    values = {}
                    for field in ('DisplayName', 'DisplayVersion', 'InstallLocation', 'UninstallString', 'QuietUninstallString'):
                        try:
                            values[field] = winreg.QueryValueEx(key, field)[0]
                        except FileNotFoundError:
                            pass
                    result.append({'hive': hive_name, 'view': view, **values})
            except FileNotFoundError:
                pass
    return result


def run_owned(command, output, label, *, timeout=240):
    from app_backend.native_process import ProcessTree
    log = output / (label + '-process.log')
    with log.open('wb') as stream, ProcessTree(command, cwd=output, stdout=stream, stderr=stream) as tree:
        code = tree.process.wait(timeout=timeout)
        if code != 0:
            raise AssertionError(label + ' failed with exit' + str(code))
        end = time.monotonic() + 30
        while tree._windows_job_active():
            if time.monotonic() >= end:
                raise AssertionError(label + ' descendants remained after process exit')
            time.sleep(.05)
        return {'exitCode': code, 'ownedDescendantsExitedBeforeCleanup': True}


def owned_process_images(tree):
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    buffer = ctypes.create_string_buffer(8 + ctypes.sizeof(ctypes.c_size_t) * 2048)
    if not tree._kernel.QueryInformationJobObject(tree._job, 3, buffer, len(buffer), None):
        raise ctypes.WinError(ctypes.get_last_error())
    count = ctypes.c_ulong.from_buffer(buffer, 4).value
    if count > 2048:
        raise AssertionError('Unexpected owned process count')
    result = []
    for index in range(count):
        pid = ctypes.c_size_t.from_buffer(buffer, 8 + ctypes.sizeof(ctypes.c_size_t) * index).value
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            continue  # An owned child can exit between the two kernel calls.
        try:
            size = w.DWORD(32768)
            text = ctypes.create_unicode_buffer(size.value)
            if kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(size)):
                result.append(text.value)
        finally:
            kernel.CloseHandle(handle)
    return result


class ObservedDevTools(DevTools):
    def __init__(self, app, url, observe):
        self.observe = observe
        super().__init__(app, url)

    def pump_until(self, test, seconds, label):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.observe()
            self.app.processEvents()
            value = test()
            if value:
                return value
            time.sleep(.01)
        raise AssertionError(label + ' timed out')


def choose_native_save_controls(controls, expected_filename=None):
    """Return only an unambiguous enabled File name Edit and Save button pair."""
    candidates=[]
    for control in controls:
        if control['class']!='Edit':continue
        if expected_filename is not None and PureWindowsPath(
                str(control.get('text','')).strip().strip('"')).name.casefold() != expected_filename.casefold():
            continue
        score=(30 if control['id']==1152 else 20 if 1148 in control.get('parentIds',[]) else
               20 if control['id']==1148 else 0)
        # Modern common dialogs also give the Search Edit ID1001. It may
        # appear while the actual File name host is still initializing. A
        # bare ID1001 is not filename evidence, even if Save already exists.
        if not score:continue
        if control.get('text','').casefold().endswith('.pdf'):score+=100
        if score:candidates.append((score,control))
    candidates.sort(key=lambda value:value[0],reverse=True)
    saves=[c for c in controls if c['class']=='Button' and c['id']==1]
    if not candidates or len(saves)!=1 or len(candidates)>1 and candidates[0][0]==candidates[1][0]:
        return None
    return candidates[0][1],saves[0]


def complete_native_save_dialog(pid, destination, output, *, expected_filename):
    """Interact only with this test app's native Windows Save dialog."""
    from ctypes import wintypes as w
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumChildWindows.argtypes = [w.HWND, callback_type, w.LPARAM]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetWindowTextLengthW.argtypes = [w.HWND]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetDlgCtrlID.argtypes = [w.HWND]
    user.GetParent.argtypes = [w.HWND]
    user.GetParent.restype = w.HWND
    user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    user.IsWindowVisible.argtypes = [w.HWND]
    user.IsWindowEnabled.argtypes = [w.HWND]
    user.IsWindow.argtypes = [w.HWND]
    user.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user.SendMessageTimeoutW.restype = w.LPARAM

    def text_of(handle):
        # GetWindowText cannot read another process's Edit control. These
        # standard WM_GETTEXT messages marshal their buffers across processes.
        length = ctypes.c_size_t()
        if not user.SendMessageTimeoutW(handle, 0x000E, 0, 0, 2, 1000, ctypes.byref(length)):
            return ''
        text = ctypes.create_unicode_buffer(min(length.value,32768) + 1)
        copied = ctypes.c_size_t()
        if not user.SendMessageTimeoutW(handle, 0x000D, len(text), ctypes.cast(text,ctypes.c_void_p).value,
                                        2, 1000, ctypes.byref(copied)):
            return ''
        return text.value

    def class_of(handle):
        text = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(handle, text, len(text))
        return text.value

    def windows():
        found = []
        def visit(handle, _):
            actual = w.DWORD()
            user.GetWindowThreadProcessId(handle, ctypes.byref(actual))
            if actual.value == pid and user.IsWindowVisible(handle) and class_of(handle) == '#32770':
                found.append(handle)
            return True
        user.EnumWindows(callback_type(visit), 0)
        return found

    deadline = time.monotonic() + 30
    dialog = None
    while time.monotonic() < deadline:
        found = windows()
        if len(found) == 1:
            dialog = found[0]
            break
        if len(found) > 1:
            raise AssertionError('More than one owned native dialog; refusing ambiguous interaction')
        time.sleep(.05)
    if dialog is None:
        raise AssertionError('Actual owned Windows Save dialog did not appear')
    def read_controls():
        controls=[]
        def child(handle, _):
            if len(controls)>=512:
                return False
            if user.IsWindowVisible(handle) and user.IsWindowEnabled(handle):
                cls=class_of(handle)
                parents=[]
                if cls=='Edit':
                    parent=user.GetParent(handle)
                    while parent and parent!=dialog:
                        parents.append(user.GetDlgCtrlID(parent));parent=user.GetParent(parent)
                rect=w.RECT()
                user.GetWindowRect(handle,ctypes.byref(rect))
                controls.append({'handle':handle,'class':cls,'id':user.GetDlgCtrlID(handle),
                    'text':text_of(handle) if cls in ('Edit','Button','Static','ComboBox') else '',
                    'parentIds':parents,'rect':[rect.left,rect.top,rect.right,rect.bottom]})
            return True
        user.EnumChildWindows(dialog,callback_type(child),0)
        return controls

    controls=[]
    selected=None
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        controls=read_controls()
        selected=choose_native_save_controls(controls,expected_filename=expected_filename)
        if selected:break
        time.sleep(.05)
    if selected is None:
        raise AssertionError('Native File name/Save controls did not become unambiguous and enabled: '+
                             repr([{k:v for k,v in c.items() if k!='handle'} for c in controls]))
    edit,save=selected
    original_name = edit.get('text','')
    control_tree_before=controls
    shot = {'status':'NOT_CAPTURED'}
    try:
        from PIL import ImageGrab
        rect = w.RECT()
        user.GetWindowRect(dialog, ctypes.byref(rect))
        image = ImageGrab.grab(bbox=(rect.left,rect.top,rect.right,rect.bottom))
        image.save(output/'native-save-dialog.png')
        shot = {'status':'CAPTURED','size':list(image.size)}
    except Exception as error:
        shot = {'status':'NOT_AVAILABLE','reason':type(error).__name__}
    value = ctypes.create_unicode_buffer(str(destination))
    answer = ctypes.c_size_t()
    if not user.SendMessageTimeoutW(edit['handle'], 0x000C, 0, ctypes.cast(value, ctypes.c_void_p).value, 2, 5000, ctypes.byref(answer)):
        raise ctypes.WinError(ctypes.get_last_error())
    if text_of(edit['handle']) != str(destination):
        raise AssertionError('Native File name field did not accept the exact output path')
    control_tree_entered=read_controls()
    if not user.SendMessageTimeoutW(save['handle'], 0x00F5, 0, 0, 2, 5000, ctypes.byref(answer)):
        raise ctypes.WinError(ctypes.get_last_error())
    deadline=time.monotonic()+30
    while user.IsWindow(dialog) and user.IsWindowVisible(dialog):
        if time.monotonic()>=deadline:
            raise AssertionError('Native Save dialog did not close after confirmed filename entry: '+repr(read_controls()))
        time.sleep(.05)
    return {'status':'PASS','dialogClass':'#32770','ownedByAppPID':True,'fileNameControlID':edit['id'],
            'fileNameParentIDs':edit.get('parentIds',[]),'fileNameBefore':original_name,
            'fileNameAfter':str(destination),'fileNameSelectionBasis':'filename_edt1_or_combo1148_host',
            'controlTreeBeforeEntry':control_tree_before,'controlTreeAfterEntry':control_tree_entered,
            'dialogClosedAfterSave':True,
            'saveButtonID':1,'QFileDialogOverrideUsed':False,'productHookUsed':False,'screenshot':shot}


def installed_gui(executable, output, user_directory, expected_sha):
    from PySide6.QtCore import QCoreApplication
    from app_backend.native_process import ProcessTree
    from pypdf import PdfReader
    import urllib.request

    output.mkdir()
    user_directory.mkdir()
    app = QCoreApplication.instance() or QCoreApplication([])
    marker = 'AIO_INSTALLED_NATIVE_' + expected_sha[:12]
    control = ('{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 Arial;}}\\f0\\fs24 ' + marker + ' installed desktop control.}').encode('ascii')
    source = user_directory/'installed-control.rtf'
    source.write_bytes(control)
    destination = user_directory/'AIO-installed-result.pdf'
    before = digest(source)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0))
        port = reservation.getsockname()[1]
    environment = dict(os.environ)
    for name in ('PYTHONPATH','PYTHONHOME','AIO_LIBREOFFICE','AIO_LIBREOFFICE_ROOT','AIO_LIBREOFFICE_REGISTRY','AIO_QA_ENGINE','AIO_BUILD_REVISION'):
        environment.pop(name,None)
    environment.update(QT_QPA_PLATFORM='windows',QTWEBENGINE_REMOTE_DEBUGGING='127.0.0.1:'+str(port),
                       AIO_GUARD_DATA_DIR=str(output/'application-data'))
    observed = set()
    result = {'status':'INCOMPLETE','engineSelection':'installed bundled engine; external overrides removed',
              'physicalMonitorDPI':'NOT_RUN','externalDisks':'NOT_RUN','sourceSha256':before,'sourcePath':str(source)}
    devtools = None
    with (output/'desktop.log').open('wb') as log, ProcessTree([str(executable)],cwd=output,env=environment,stdout=log,stderr=log) as tree:
        last_observation = [0.0]
        def observe():
            if time.monotonic()-last_observation[0]>=.05:
                observed.update(owned_process_images(tree));last_observation[0]=time.monotonic()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic()+90
            page = None
            while time.monotonic()<deadline and tree.process.poll() is None:
                observe()
                try:
                    with opener.open(f'http://127.0.0.1:{port}/json',timeout=1) as response:
                        page=next((p for p in json.load(response) if p.get('type')=='page' and p.get('url','').startswith('http://127.0.0.1:')),None)
                    if page:break
                except (OSError,ValueError):pass
                time.sleep(.1)
            if page is None:raise AssertionError('Installed ordinary desktop did not expose its loopback app page')
            devtools=ObservedDevTools(app,page['webSocketDebuggerUrl'],observe)
            devtools.wait('!!document.querySelector(".nav-item")')
            if not devtools.evaluate('window.AllInOneNativeShell===true'):
                raise AssertionError('Installed page is not the native desktop')
            devtools.evaluate('''(()=>{window.__installedState=null;window.__installedSaveEvents=[];window.addEventListener('aio-guard-state',e=>window.__installedState=e.detail);window.addEventListener('aio-native-save-result',e=>{if(window.__installedSaveEvents.length<32)window.__installedSaveEvents.push(e.detail)});[...document.querySelectorAll('.nav-item')].find(e=>e.textContent.includes('Конвертер')).click();return true;})()''')
            devtools.wait('!!document.querySelector("[data-cv-picker]")&&!document.querySelector("[data-cv-add]").disabled')
            payload=json.dumps(base64.b64encode(control).decode('ascii'))
            devtools.evaluate('''(()=>{const d=new DataTransfer();d.items.add(new File([Uint8Array.from(atob(PAYLOAD),x=>x.charCodeAt(0))],'installed-control.rtf'));const p=document.querySelector('[data-cv-picker]');p.files=d.files;p.dispatchEvent(new Event('change',{bubbles:true}));return true;})()'''.replace('PAYLOAD',payload))
            devtools.wait('document.querySelectorAll(".aio-cv__file[data-file-id]").length===1&&!document.querySelector("[data-cv-add]").disabled',180)
            uploaded=list((output/'application-data').rglob('*.rtf'))
            if len(uploaded)!=1 or digest(uploaded[0])!=before:
                raise AssertionError('Installed upload differs from the source control')
            devtools.evaluate('''(()=>{const c=[...document.querySelectorAll('.aio-cv__category')].find(e=>e.textContent==='Документы');if(c)c.click();document.querySelector('[aria-label="Выбрать PDF"]').click();document.querySelector('[data-cv-run]').click();return true;})()''')
            devtools.wait('window.__installedState?.finished===true&&window.__installedState.succeeded===1',180)
            devtools.wait('!document.querySelector("[data-cv-save]").disabled&&document.querySelector("[data-cv-run]").hidden')
            report=devtools.evaluate('''(async()=>{const r=await fetch('/api/converter-guard/report?job='+encodeURIComponent(window.__installedState.jobId)+'&lookup=current',{headers:{'X-AIO-Token':document.body.dataset.aioToken},cache:'no-store'});if(!r.ok)throw Error('Report HTTP'+r.status);return r.json();})()''')
            identity=report.get('buildIdentity',{})
            if identity.get('sourceRevision')!=expected_sha or identity.get('basis')!='bundled':
                raise AssertionError('Running installed EXE did not report the expected bundled source SHA')
            target=report['files'][0]['targets'][0]
            if not target.get('libreOfficeStarted') or target.get('state') not in ('ready','saved'):
                raise AssertionError('Installed native LibreOffice export was not confirmed')
            result['buildIdentity']=identity
            result['nativeReport']=report
            # Clicking the ordinary main Save opens the actual Windows dialog.
            devtools.evaluate('document.querySelector("[data-cv-save]").click()')
            result['nativeFileDialog']=complete_native_save_dialog(tree.process.pid,destination,output,
                expected_filename=Path(target['outputs'][0]['path']).name)
            devtools.wait('document.querySelector("[data-cv-status]").classList.contains("aio-cv__saved")',90)
            result['nativeSaveEvents']=devtools.evaluate('window.__installedSaveEvents')
            saved_report=devtools.evaluate('''(async()=>{const r=await fetch('/api/converter-guard/report?job='+encodeURIComponent(window.__installedState.jobId)+'&lookup=current',{headers:{'X-AIO-Token':document.body.dataset.aioToken},cache:'no-store'});if(!r.ok)throw Error('Saved report HTTP'+r.status);return r.json();})()''')
            result['savedReport']=saved_report
            if not destination.is_file():
                result['observedPdfPaths']=[{'path':str(p),'bytes':p.stat().st_size}
                    for folder in (output,user_directory) for p in list(folder.rglob('*.pdf'))[:200] if p.is_file()]
                raise AssertionError('Native Save did not create the exact requested user output; see nativeSaveEvents and observedPdfPaths')
            result['savedOutput']={'path':str(destination),'bytes':destination.stat().st_size,'sha256':digest(destination)}
            with destination.open('rb') as stream:
                pdf=PdfReader(stream);pages=len(pdf.pages);text='\n'.join(p.extract_text() or '' for p in pdf.pages)
            result['savedOutput']['pages']=pages
            if marker not in text:raise AssertionError('Saved installed-native PDF lost the independent source marker')
            saved_target=saved_report['files'][0]['targets'][0]
            if saved_target.get('saveStatus')!='saved' or saved_target.get('readbackVerified') is not True:
                raise AssertionError('Installed desktop did not verify saved destination bytes')
            shot=devtools.command('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
            (output/'installed-saved.png').write_bytes(base64.b64decode(shot['data']))
            bundled=executable.parent/'_internal/libreoffice/program/soffice.bin'
            if not any(Path(p)==bundled for p in observed):
                raise AssertionError('No owned process from the installed bundled LibreOffice was observed')
            result.update(nativeConversion='PASS',markerVerified=True,nativeSavedReadbackVerified=True,
                sourceUnchanged=digest(source)==before,sourceSha256=before,
                savedOutput={'path':str(destination),'bytes':destination.stat().st_size,'sha256':digest(destination),'pages':pages},
                observedOwnedProcessImages=sorted(observed),installedBundledOfficeObserved=True)
            if not result['sourceUnchanged']:raise AssertionError('Source control changed')
            devtools.socket.close();devtools=None
            _close_windows(tree.process.pid)
            if tree.process.wait(timeout=45)!=0:raise AssertionError('Installed desktop failed normal WM_CLOSE')
            deadline=time.monotonic()+30
            while tree._windows_job_active():
                if time.monotonic()>=deadline:raise AssertionError('Installed desktop descendants survived graceful close')
                time.sleep(.05)
            result.update(gracefulClose='PASS',ownedDescendantsExitedBeforeForcedCleanup=True,status='PASS')
        except Exception as error:
            result.update(status='FAIL',error=type(error).__name__+': '+str(error))
            if devtools is not None:
                try:
                    shot=devtools.command('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
                    (output/'installed-failure.png').write_bytes(base64.b64decode(shot['data']))
                except Exception:pass
            raise
        finally:
            if devtools is not None:devtools.socket.close()
            result['observedOwnedProcessImages']=sorted(observed)
            (output/'evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def execute(receipt_path, output, expected_sha):
    receipt,installer=load_build_receipt(receipt_path,expected_sha)
    output=require_disposable_output(output)
    if registrations():raise RuntimeError('Dedicated acceptance AppID is already registered; no overwrite allowed')
    output.mkdir(parents=True,exist_ok=False)
    install=output/'installed-app'
    evidence={'schema':1,'status':'INCOMPLETE','scope':'actual per-user installer + installed ordinary EXE + native Windows Save + graceful close + uninstall',
        'sourceRevision':expected_sha,'installerSha256':digest(installer),'appId':APP_ID,'releasePublication':'NOT_PERFORMED',
        'physicalMonitorsDPI':'NOT_RUN','allFormats':'NOT_CLAIMED','realUserPC':'NOT_USED'}
    sentinel=install/'USER-OUTPUT-MUST-SURVIVE.txt'
    installed=False
    failure=None
    try:
        evidence['installProcess']=run_owned([str(installer),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',
            '/DIR='+str(install),'/LOG='+str(output/'install.log')],output,'install',timeout=300)
        installed=True
        registered=registrations()
        if not registered or any(Path(r.get('InstallLocation','')).resolve()!=install for r in registered):
            raise AssertionError('Actual per-user installation was not registered at the owned destination')
        if any(r['hive']!='HKCU' for r in registered):raise AssertionError('Acceptance unexpectedly registered machine-wide')
        evidence['registration']=registered
        evidence['installedPayload']=verify_installed_payload(install,receipt)
        sentinel.write_text('User-created unlogged output; uninstall must preserve this.\n',encoding='ascii')
        evidence['appRootSentinelSha256']=digest(sentinel)
        evidence['installedGUI']=installed_gui(install/'A.I.O.exe',output/'gui',output/'user-outputs',expected_sha)
    except Exception as error:
        failure=error;evidence.update(status='FAIL',error=type(error).__name__+': '+str(error))
        partial=output/'gui/evidence.json'
        if partial.is_file():
            try:
                evidence['installedGUI']=json.loads(partial.read_text(encoding='utf-8'))
            except (OSError,ValueError) as receipt_error:
                evidence['guiReceiptReadError']=type(receipt_error).__name__
    finally:
        if installed or registrations():
            try:
                uninstallers=list(install.glob('unins*.exe'))
                if len(uninstallers)!=1:raise AssertionError('Owned installation uninstaller is not unique')
                evidence['uninstallProcess']=run_owned([str(uninstallers[0]),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                    '/LOG='+str(output/'uninstall.log')],output,'uninstall',timeout=300)
                if registrations():raise AssertionError('Dedicated uninstall registration survived uninstall')
                if (install/'A.I.O.exe').exists() or (install/'_internal/libreoffice/program/soffice.bin').exists():
                    raise AssertionError('Logged application/engine files survived uninstall')
                evidence['uninstalled']=True
                evidence['unloggedAppRootOutputPreserved']=sentinel.is_file() and digest(sentinel)==evidence.get('appRootSentinelSha256')
                saved=evidence.get('installedGUI',{}).get('savedOutput')
                evidence['savedUserOutputPreserved']=(Path(saved['path']).is_file() and digest(Path(saved['path']))==saved['sha256']) if saved else 'NOT_APPLICABLE_NO_SAVED_OUTPUT'
                source_sha=evidence.get('installedGUI',{}).get('sourceSha256')
                evidence['sourceControlPreserved']=((output/'user-outputs/installed-control.rtf').is_file() and digest(output/'user-outputs/installed-control.rtf')==source_sha) if source_sha else 'NOT_CHECKED_NO_GUI_RECEIPT'
                preserved=[evidence.get(k) for k in ('unloggedAppRootOutputPreserved','savedUserOutputPreserved','sourceControlPreserved')]
                if any(value is False for value in preserved) or failure is None and any(value is not True for value in preserved):
                    raise AssertionError('A user output/source was absent or changed after uninstall')
                if failure is None:evidence['status']='PASS'
            except Exception as error:
                evidence['uninstallError']=type(error).__name__+': '+str(error)
                evidence['status']='FAIL'
                if failure is None:failure=error
        (output/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        summary={k:v for k,v in evidence.items() if k not in ('installedGUI','registration')}
        print('AIO_COMPLETION_INSTALLED_WINDOWS='+json.dumps(summary,ensure_ascii=True),flush=True)
    if failure is not None:raise failure
    return evidence


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer-receipt',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source-sha',required=True)
    args=parser.parse_args()
    execute(args.installer_receipt,args.output,args.source_sha)


if __name__=='__main__':main()
