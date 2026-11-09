import hashlib
from pathlib import Path
import shutil
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_STORED

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.macro_identity import (
    bind_xlsm_identity,copy_verified_xlsm,source_stamp,CALC,XLSM_TYPE)
from app_backend.converter_guard.source_contract import inspect_source_contract

FIXTURES=Path(__file__).parent/'fixtures/macros'


class VerifiedMacroIdentity(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'original.xlsm'
        shutil.copyfile(FIXTURES/'SimpleMacro.xlsm',self.source)
        self.native=SimpleNamespace(valid=True,fingerprint='current-registry',observed_decision=lambda target,proof:'registered')
        self.cancel=threading.Event()
        self.proof={'state':'detected','modelLoaded':True,'basis':'native_preflight_uno_document',
                    'documentServices':[CALC],'registryFingerprint':self.native.fingerprint,
                    'sourceContract':inspect_source_contract(self.source)}

    def bound(self):
        return bind_xlsm_identity(self.source,self.proof,self.native,source_stamp(self.source),cancel=self.cancel)

    def copy(self,proof=None,**kwargs):
        return copy_verified_xlsm(self.source,self.root/'output','xlsm',proof or self.bound(),self.native,
                                  cancel=self.cancel,**kwargs)

    def test_every_byte_macro_attribute_and_workbook_part_is_preserved(self):
        raw=self.source.read_bytes();paths,receipt=self.copy()
        self.assertEqual(paths[0].read_bytes(),raw)
        self.assertEqual(self.source.read_bytes(),raw)
        self.assertEqual(receipt['sourceSha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(receipt['sourceSha256'],receipt['outputSha256'])
        self.assertTrue(receipt['sourceCrcVerified']);self.assertTrue(receipt['readbackVerified'])
        self.assertTrue(receipt['nativeModelLoaded']);self.assertFalse(receipt['nativeExportPerformed'])

    def test_filename_alone_does_not_admit_wrong_macro_container(self):
        shutil.copyfile(FIXTURES/'SimpleMacro.docm',self.source)
        fake={**self.proof,'sourceContract':{**self.proof['sourceContract'],'mainContentType':XLSM_TYPE}}
        proof=bind_xlsm_identity(self.source,fake,self.native,source_stamp(self.source),cancel=self.cancel)
        self.assertIsNone(self.copy(proof))
        self.assertFalse((self.root/'output').exists())

    def test_actual_source_contract_cannot_be_replaced_by_suffix(self):
        self.proof['sourceContract']['mainContentType']='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
        self.assertNotIn('macroIdentitySource',self.bound())
        self.assertIsNone(self.copy(self.proof))

    def test_legacy_xls_cannot_become_an_xlsm_byte_copy(self):
        shutil.copyfile(FIXTURES/'SimpleMacro.xls',self.source)
        self.assertIsNone(self.copy(self.proof))

    def test_stale_registry_wrong_model_and_coarse_lok_proof_are_not_identity_proofs(self):
        for changes in ({'registryFingerprint':'old'}, {'modelLoaded':False},
                        {'documentServices':['com.sun.star.text.TextDocument']},
                        {'basis':'native_preflight_document_kind'}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.copy({**self.proof,**changes}))

    def test_prepared_or_retry_source_requires_real_export_not_identity(self):
        self.assertIsNone(self.copy({**self.bound(),'sourcePreparation':{'kind':'ooxml_content_types_namespace'}}))
        self.assertIsNone(self.copy(retry_recipe='anything'))

    def test_source_change_after_actual_inspection_blocks_copy(self):
        proof=self.bound();self.source.write_bytes(self.source.read_bytes()+b'changed')
        with self.assertRaises(GuardError) as caught:self.copy(proof)
        self.assertEqual(caught.exception.code,'SOURCE_CHANGED_AFTER_PREFLIGHT')
        self.assertFalse((self.root/'output').exists())

    def test_source_change_during_inspection_cannot_get_a_bound_proof(self):
        stamp=source_stamp(self.source);self.source.write_bytes(self.source.read_bytes()+b'changed')
        with self.assertRaises(GuardError) as caught:
            bind_xlsm_identity(self.source,self.proof,self.native,stamp,cancel=self.cancel)
        self.assertEqual(caught.exception.code,'SOURCE_CHANGED_AFTER_PREFLIGHT')

    def test_full_hash_catches_changed_zip_bytes_even_if_stat_is_unchanged(self):
        proof=self.bound();stamp=proof['macroIdentitySource']['identity']
        raw=bytearray(self.source.read_bytes());raw[10]^=1  # Valid local ZIP timestamp; member CRCs stay valid.
        self.source.write_bytes(raw)
        with patch('app_backend.converter_guard.macro_identity.source_stamp',return_value=stamp):
            with self.assertRaises(GuardError) as caught:self.copy(proof)
        self.assertEqual(caught.exception.code,'SOURCE_CHANGED_AFTER_PREFLIGHT')
        self.assertFalse(list((self.root/'output').glob('*.xlsm')))

    def test_zero_quota_is_enforced_for_small_sub_scan_interval_copy(self):
        with self.assertRaises(GuardError) as caught:self.copy(output_limit=lambda:0)
        self.assertEqual(caught.exception.code,'SPOOL_QUOTA')
        self.assertFalse((self.root/'output').exists())

    def test_cancel_before_copy_publishes_nothing(self):
        proof=self.bound();self.cancel.set()
        with self.assertRaises(GuardError) as caught:self.copy(proof)
        self.assertEqual(caught.exception.code,'CANCELLED')
        self.assertFalse((self.root/'output').exists())

    def test_cancel_after_destination_creation_removes_partial_copy(self):
        proof=self.bound();output=self.root/'output'
        cancel=SimpleNamespace(is_set=lambda:any(output.glob('*.xlsm')) if output.exists() else False)
        with self.assertRaises(GuardError) as caught:
            copy_verified_xlsm(self.source,output,'xlsm',proof,self.native,cancel=cancel)
        self.assertEqual(caught.exception.code,'CANCELLED')
        self.assertFalse(list(output.glob('*.xlsm')))

    def test_corrupt_unused_zip_part_is_not_copied_as_verified(self):
        with ZipFile(self.source,'a',compression=ZIP_STORED) as z:z.writestr('unused.bin',b'checksum')
        with ZipFile(self.source) as z:info=z.getinfo('unused.bin')
        raw=bytearray(self.source.read_bytes())
        name_length,extra_length=struct.unpack_from('<HH',raw,info.header_offset+26)
        raw[info.header_offset+30+name_length+extra_length]^=1
        self.source.write_bytes(raw)
        with self.assertRaises(GuardError) as caught:self.copy()
        self.assertEqual(caught.exception.code,'SOURCE_CONTAINER_INVALID')
        self.assertFalse((self.root/'output').exists())

    def test_readback_hash_failure_removes_created_output(self):
        import app_backend.converter_guard.macro_identity as identity
        proof=self.bound();real=identity._digest
        def digest(path,cancel):return '0'*64 if Path(path).parent.name=='output' else real(path,cancel)
        with patch.object(identity,'_digest',side_effect=digest):
            with self.assertRaises(GuardError) as caught:self.copy(proof)
        self.assertEqual(caught.exception.code,'IDENTITY_COPY_UNVERIFIED')
        self.assertFalse(list((self.root/'output').glob('*.xlsm')))

    def test_no_overwrite_of_existing_destination(self):
        out=self.root/'output';out.mkdir();existing=out/'original.xlsm';existing.write_bytes(b'user data')
        with self.assertRaises(FileExistsError):self.copy()
        self.assertEqual(existing.read_bytes(),b'user data')

    def test_adapter_keeps_full_identity_receipt_without_native_export(self):
        from app_backend.converter_guard import LibreOfficeAdapter
        adapter=LibreOfficeAdapter(self.root/'not-executed-soffice')
        self.native.html_route_reason=lambda target,proof:None
        adapter._local.proof=self.bound()
        with patch.object(adapter,'_registry',return_value=self.native),patch.object(adapter,'_convert_native') as export:
            paths=adapter.convert(self.source,'xlsm',self.root/'adapter-task',cancel=self.cancel,
                                  timeout=10,on_started=lambda:None)
        export.assert_not_called()
        self.assertEqual(adapter.execution_mode(),'verified_macro_identity')
        checks=adapter.export_info()['resultChecks']
        identity=next(c for c in checks if c['check']=='verified_macro_identity')
        self.assertFalse(identity['nativeExportPerformed']);self.assertTrue(identity['readbackVerified'])
        self.assertEqual(paths[0].read_bytes(),self.source.read_bytes())
        self.assertIn('VBA_VERIFIED_BYTE_IDENTITY',adapter.export_info()['warnings'])


if __name__=='__main__':unittest.main()
