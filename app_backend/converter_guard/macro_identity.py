"""Byte-preserving XLSM identity after a real, source-bound native inspection.

No native export, macro execution, ZIP rewriting, project attachment or format
conversion occurs. Every copied byte and ZIP member CRC is verified. This is
unavailable for stale proofs, preparation/retry recipes, and other containers.
"""
import hashlib
import os
from pathlib import Path
import stat
import zipfile

from .errors import GuardError
from .source_contract import inspect_source_contract

CALC='com.sun.star.sheet.SpreadsheetDocument'
XLSM_TYPE='application/vnd.ms-excel.sheet.macroEnabled.main+xml'
MAX_MEMBERS=100_000


def source_stamp(source):
    source=Path(source)
    try:
        info=source.lstat()
        if not stat.S_ISREG(info.st_mode) or source.is_symlink():return None
        return [str(source.resolve(strict=True)),info.st_dev,info.st_ino,info.st_size,
                info.st_mtime_ns,info.st_ctime_ns]
    except OSError:
        return None


def _poll(cancel):
    if cancel is not None and cancel.is_set():
        raise GuardError('CANCELLED','Сохранение неизменённого XLSM отменено.',stage='result')


def _digest(path,cancel):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise GuardError('SOURCE_CHANGED_AFTER_PREFLIGHT','Исходный XLSM изменился после проверки.',stage='result')
        while block:=stream.read(65536):
            _poll(cancel);value.update(block)
    _poll(cancel)
    return value.hexdigest()


def _native_xlsm(proof,native):
    contract=proof.get('sourceContract',{})
    return (isinstance(contract,dict) and proof.get('state')=='detected' and proof.get('modelLoaded') is True
            and proof.get('basis')=='native_preflight_uno_document'
            and proof.get('documentServices')==[CALC]
            and not proof.get('sourcePreparation')
            and contract.get('state')=='recognized' and contract.get('contentClass')=='ooxml_package'
            and (contract.get('mainContentType') or '').lower()==XLSM_TYPE.lower()
            and getattr(native,'valid',False)
            and proof.get('registryFingerprint')==native.fingerprint
            and native.observed_decision('xlsm',proof)=='registered')


def bind_xlsm_identity(source,proof,native,initial_stamp,*,cancel):
    """Bind the actual loaded model to unchanged original bytes, not a suffix."""
    if not _native_xlsm(proof,native):return proof
    if initial_stamp is None or source_stamp(source)!=initial_stamp:
        raise GuardError('SOURCE_CHANGED_AFTER_PREFLIGHT','Исходный XLSM изменился во время проверки.',stage='preflight')
    digest=_digest(source,cancel)
    if source_stamp(source)!=initial_stamp:
        raise GuardError('SOURCE_CHANGED_AFTER_PREFLIGHT','Исходный XLSM изменился во время проверки.',stage='preflight')
    return {**proof,'macroIdentitySource':{'schema':1,'identity':initial_stamp,
            'sha256':digest,'size':initial_stamp[3]}}


def copy_verified_xlsm(source,output,target,proof,native,*,cancel,output_limit=None,retry_recipe=None):
    if target!='xlsm' or retry_recipe is not None or not _native_xlsm(proof,native):return None
    bound=proof.get('macroIdentitySource')
    if not isinstance(bound,dict) or bound.get('schema')!=1:return None
    source=Path(source)
    stamp=bound.get('identity')
    if not isinstance(stamp,list) or len(stamp)!=6 or source_stamp(source)!=stamp:
        raise GuardError('SOURCE_CHANGED_AFTER_PREFLIGHT','Исходный XLSM изменился после проверки.',stage='result')
    # A copied/forged extension or proof from another family cannot authorize
    # this route. Inspect the actual current package's declared main part too.
    actual=inspect_source_contract(source,cancel=cancel)
    if (actual.get('state')!='recognized' or actual.get('contentClass')!='ooxml_package'
            or (actual.get('mainContentType') or '').lower()!=XLSM_TYPE.lower()):return None
    expected=bound.get('sha256')
    if not isinstance(expected,str) or len(expected)!=64 or bound.get('size')!=stamp[3]:
        raise GuardError('SOURCE_CHANGED_AFTER_PREFLIGHT','Не подтверждена сохранность исходного XLSM.',stage='result')
    def budget(written):
        _poll(cancel)
        if output_limit is not None and written>output_limit():
            raise GuardError('SPOOL_QUOTA','Превышена настроенная квота временных файлов.',stage='result',status=507)
    budget(stamp[3])
    try:
        with zipfile.ZipFile(source) as archive:
            entries=archive.infolist()
            if len(entries)>MAX_MEMBERS:raise ValueError('xlsm_member_budget')
            names=set()
            for entry in entries:
                _poll(cancel)
                key=entry.filename.casefold()
                if key in names or entry.flag_bits & 1:raise ValueError('xlsm_ambiguous_or_encrypted')
                names.add(key)
                with archive.open(entry) as member:
                    while member.read(65536):_poll(cancel)
        _poll(cancel)
    except (zipfile.BadZipFile,RuntimeError,NotImplementedError,ValueError) as error:
        raise GuardError('SOURCE_CONTAINER_INVALID','Не подтверждена целостность исходного XLSM.',stage='result') from error
    if source_stamp(source)!=stamp:
        raise GuardError('SOURCE_CHANGED_AFTER_PREFLIGHT','Исходный XLSM изменился после проверки.',stage='result')
    folder=Path(output);folder.mkdir(parents=True,exist_ok=True)
    destination=folder/(source.stem+'.xlsm')
    created=False
    try:
        copied=hashlib.sha256();written=0
        with source.open('rb') as src,destination.open('xb') as dst:
            created=True
            while block:=src.read(65536):
                budget(written+len(block));dst.write(block);copied.update(block);written+=len(block)
        if (written!=stamp[3] or copied.hexdigest()!=expected or source_stamp(source)!=stamp
                or _digest(source,cancel)!=expected or source_stamp(source)!=stamp):
            raise GuardError('SOURCE_CHANGED_AFTER_PREFLIGHT','Исходный XLSM изменился после проверки.',stage='result')
        budget(written)
        if destination.is_symlink() or destination.stat().st_size!=written or _digest(destination,cancel)!=expected:
            raise GuardError('IDENTITY_COPY_UNVERIFIED','Не подтверждена сохранность копии XLSM.',stage='result')
        receipt={'check':'verified_macro_identity','status':'byte_preserved','qualityCompared':False,
            'basis':'actual_uno_calc_model_and_original_source_hash_and_zip_crc',
            'sourceSha256':expected,'outputSha256':expected,'bytes':written,'sourceCrcVerified':True,
            'readbackVerified':True,'nativeModelLoaded':True,'nativeExportPerformed':False,
            'macrosExecuted':False,'behaviorCompared':False}
        return [destination],receipt
    except BaseException:
        if created:
            try:destination.unlink(missing_ok=True)
            except OSError:pass
        raise
