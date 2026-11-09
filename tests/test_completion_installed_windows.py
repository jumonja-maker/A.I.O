"""Installed acceptance readback controls; no installer or GUI is run here."""
import hashlib
import ctypes
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.qa import completion_installed_windows as acceptance


class InstalledAcceptanceReadbackTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.sha = 'a' * 40
        self.installer = self.root/'fixture-installer.exe'
        self.installer.write_bytes(b'fixture bytes, never executed')
        self.installed = self.root/'installed'
        self.installed.mkdir()
        files = {'A.I.O.exe':b'owned fixture, not executable',
                 '_internal/build-info/aio-source-revision.txt':(self.sha+'\n').encode('ascii'),
                 '_internal/libreoffice/program/soffice.bin':b'owned engine fixture'}
        self.inventory = []
        for name,data in files.items():
            target = self.installed/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(data)
            self.inventory.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        self.manifest = {'schema':1,'sourceRevision':self.sha,'appId':acceptance.APP_ID,
                         'payload':{'files':self.inventory,'fileCount':len(self.inventory)}}
        (self.installed/acceptance.SOURCE_MANIFEST).write_text(json.dumps(self.manifest),encoding='utf-8')
        self.receipt = {'schema':1,'status':'PASS','sourceRevision':self.sha,'appId':acceptance.APP_ID,
                        'installedManifestFilename':acceptance.SOURCE_MANIFEST,
                        'installedManifestSha256':acceptance.digest(self.installed/acceptance.SOURCE_MANIFEST),
                        'installer':{'path':str(self.installer),'size':self.installer.stat().st_size,
                                     'sha256':acceptance.digest(self.installer)},
                        'payload':{'files':self.inventory,'fileCount':len(self.inventory)}}
        self.receipt_path = self.root/'installer-build.json'
        self.write_receipt()

    def write_receipt(self):
        self.receipt_path.write_text(json.dumps(self.receipt),encoding='utf-8')

    def test_readback_verifies_every_installed_byte_and_exact_manifest(self):
        value,exe = acceptance.load_build_receipt(self.receipt_path,self.sha)
        self.assertEqual(exe,self.installer)
        checked = acceptance.verify_installed_payload(self.installed,value)
        self.assertEqual(checked['filesVerified'],3)
        self.assertTrue(checked['allPayloadHashesMatch'])

    def test_mutated_installed_payload_fails_even_when_size_is_unchanged(self):
        (self.installed/'A.I.O.exe').write_bytes(b'X'*len(b'owned fixture, not executable'))
        with self.assertRaisesRegex(AssertionError,'payload mismatch'):
            acceptance.verify_installed_payload(self.installed,self.receipt)

    def test_mutated_manifest_fails_before_trusting_its_inventory(self):
        self.manifest['sourceRevision'] = 'b'*40
        (self.installed/acceptance.SOURCE_MANIFEST).write_text(json.dumps(self.manifest),encoding='utf-8')
        with self.assertRaisesRegex(AssertionError,'receipt bytes'):
            acceptance.verify_installed_payload(self.installed,self.receipt)

    def test_wrong_source_or_uncompiled_build_cannot_install(self):
        with self.assertRaisesRegex(ValueError,'expected source'):
            acceptance.load_build_receipt(self.receipt_path,'b'*40)
        self.receipt['status'] = 'VALIDATED_NOT_COMPILED'
        self.write_receipt()
        with self.assertRaisesRegex(ValueError,'completed build'):
            acceptance.load_build_receipt(self.receipt_path,self.sha)

    def test_mutated_installer_is_rejected_before_execution(self):
        self.installer.write_bytes(b'altered fixture')
        with self.assertRaisesRegex(ValueError,'SHA-256'):
            acceptance.load_build_receipt(self.receipt_path,self.sha)

    def test_payload_path_traversal_windows_aliases_and_duplicates_are_rejected(self):
        for name in ('../outside','C:/outside','/absolute','x\\y','x/./y','x//y','x/../y','x:y','x.','x '):
            with self.subTest(name=name),self.assertRaises(ValueError):
                acceptance.payload_path(self.installed,name)
        self.receipt['payload']['files'] = [self.inventory[0],dict(self.inventory[0],path='a.i.o.EXE')]
        self.receipt['payload']['fileCount'] = 2
        self.write_receipt()
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            acceptance.load_build_receipt(self.receipt_path,self.sha)

    def test_installed_symlink_cannot_redirect_readback_to_another_tree(self):
        outside = self.root/'outside'
        outside.write_bytes(b'owned fixture, not executable')
        target = self.installed/'A.I.O.exe'
        if os.name=='nt':
            original=Path.is_symlink
            with patch.object(Path,'is_symlink',lambda path:path==target or original(path)):
                with self.assertRaisesRegex(ValueError,'Linked'):
                    acceptance.verify_installed_payload(self.installed,self.receipt)
        else:
            target.unlink();target.symlink_to(outside)
            with self.assertRaisesRegex(ValueError,'Linked'):
                acceptance.verify_installed_payload(self.installed,self.receipt)

    def test_installer_execution_refuses_normal_user_environment(self):
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false','RUNNER_OS':'Windows'}):
            with self.assertRaisesRegex(RuntimeError,'disposable GitHub Windows'):
                acceptance.require_disposable_output(self.root/'must-not-create')
        self.assertFalse((self.root/'must-not-create').exists())

    def test_save_dialog_selects_file_name_not_address_bar_or_search(self):
        controls=[{'handle':1,'class':'Edit','id':41477,'text':'C:/folder','parentIds':[]},
                  {'handle':2,'class':'Edit','id':1001,'text':'','parentIds':[]},
                  {'handle':3,'class':'Edit','id':1001,'text':'installed-control.pdf','parentIds':[1148]},
                  {'handle':4,'class':'Button','id':1,'text':'Save'}]
        edit,save=acceptance.choose_native_save_controls(controls)
        self.assertEqual((edit['handle'],save['handle']),(3,4))

    def test_save_dialog_waits_for_complete_unambiguous_enabled_controls(self):
        edit={'handle':1,'class':'Edit','id':1152,'text':'control.pdf'}
        save={'handle':2,'class':'Button','id':1,'text':'Save'}
        self.assertIsNone(acceptance.choose_native_save_controls([]))
        self.assertIsNone(acceptance.choose_native_save_controls([edit]))
        self.assertIsNotNone(acceptance.choose_native_save_controls([edit,save]))
        self.assertIsNone(acceptance.choose_native_save_controls([edit,dict(edit,handle=3),save]))

    def test_save_dialog_does_not_confuse_early_search_edit_with_filename_host(self):
        search={'handle':1,'class':'Edit','id':1001,'text':'','parentIds':[1234]}
        save={'handle':2,'class':'Button','id':1,'text':'Save'}
        self.assertIsNone(acceptance.choose_native_save_controls([search,save]))
        # Even a search query ending in PDF cannot establish filename identity.
        search['text']='query.pdf'
        self.assertIsNone(acceptance.choose_native_save_controls([search,save]))
        actual={'handle':3,'class':'Edit','id':1001,'text':'installed-control.pdf','parentIds':[1148]}
        self.assertEqual(acceptance.choose_native_save_controls([search,actual,save])[0]['handle'],3)

    def test_save_dialog_waits_for_actual_suggested_filename_before_typing(self):
        filename={'handle':1,'class':'Edit','id':1001,'text':'','parentIds':[1148]}
        save={'handle':2,'class':'Button','id':1,'text':'Save'}
        self.assertIsNone(acceptance.choose_native_save_controls([filename,save],expected_filename='installed-control.pdf'))
        filename['text']='installed-control.pdf'
        selected=acceptance.choose_native_save_controls([filename,save],expected_filename='installed-control.pdf')
        self.assertEqual(selected[0]['handle'],1)

    def test_captured_windows2025_filename_has_zero_id_combo_and_hidden_pdf_suffix(self):
        controls=[{'handle':1,'class':'ComboBox','id':0,'text':'installed-control',
                   'parentIds':[],'rect':[141,357,620,380]},
                  {'handle':2,'class':'Edit','id':1001,'text':'installed-control',
                   'parentIds':[0,0,0,0],'rect':[144,360,600,377]},
                  {'handle':3,'class':'ComboBox','id':0,'text':'PDF (*.pdf)',
                   'parentIds':[],'rect':[141,382,620,405]},
                  {'handle':4,'class':'Button','id':1,'text':'&Save','rect':[420,460,508,486]}]
        selected=acceptance.choose_native_save_controls(controls,expected_filename='installed-control.pdf')
        self.assertEqual(selected[0]['handle'],2)
        self.assertEqual(selected[0]['selectionBasis'],'expected_suggested_name_in_enclosing_native_ComboBox')
        # A generic Edit1001 or an unrelated search box is still insufficient.
        self.assertIsNone(acceptance.choose_native_save_controls(controls[1:],expected_filename='installed-control.pdf'))
        self.assertIsNone(acceptance.choose_native_save_controls(
            [c for c in controls if c['handle']!=3],expected_filename='installed-control.pdf'))

    def test_os_input_structures_match_the_windows_sendinput_abi(self):
        # Windows DWORD/LONG remain 32-bit even on a 64-bit host. Using the
        # host's c_ulong here would produce an invalid packet on Linux QA.
        wide=ctypes.sizeof(ctypes.c_void_p)==8
        self.assertEqual(ctypes.sizeof(acceptance._Input),40 if wide else 28)
        self.assertEqual(acceptance._Input.value.offset,8 if wide else 4)
        self.assertEqual(acceptance._KeyboardInput.wScan.offset,2)
        self.assertEqual(acceptance._KeyboardInput.dwFlags.offset,4)
        self.assertEqual(acceptance._MouseInput.dwFlags.offset,12)


if __name__=='__main__':unittest.main()
