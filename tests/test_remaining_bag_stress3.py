import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'tools/qa/guard_remaining_bag_stress3.py'
spec=importlib.util.spec_from_file_location('remaining_stress3',SCRIPT);qa=importlib.util.module_from_spec(spec);spec.loader.exec_module(qa)

class Stress3EvidenceContracts(unittest.TestCase):
    def test_exit_success_or_missing_cases_cannot_replace_expected_verdicts(self):
        for data in ({'status':'PASS','cases':[]},{'status':'FAIL','cases':[{'status':'PASS'}]},
                     {'status':'PASS','cases':[{'status':'not_checked'}]}):
            with self.assertRaises(ValueError):qa.validate_stage('control',data,{'cases':1})
        self.assertEqual(qa.validate_stage('control',{'status':'PASS','cases':[{'status':'PASS'}]},{'cases':1})['status'],'PASS')
        self.assertEqual(qa.validate_stage('negative',{'status':'PASS','negative':[{'status':'SAFE_REJECTION'}]},{'negative':1})['status'],'PASS')
    def test_corpus_original_hashes_and_path_ownership_are_mandatory(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'case';folder.mkdir();path=folder/'original.csv';path.write_bytes(b'ID,Amount\n001,42\n')
            sha=hashlib.sha256(path.read_bytes()).hexdigest();data={'sourceSHA256':{path.name:sha}}
            self.assertEqual(qa.referenced_sources(folder,data)[0]['sha256'],sha)
            path.write_bytes(b'changed')
            with self.assertRaises(ValueError):qa.referenced_sources(folder,data)
            external=folder.parent/'outside.csv';external.write_bytes(b'x')
            with self.assertRaises(ValueError):qa.referenced_sources(folder,{'sourceSHA256':{'../outside.csv':hashlib.sha256(b'x').hexdigest()}})
    def test_fixed_plan_has_real_positive_negative_and_ui_scopes(self):
        stages={name:expected for name,_,expected in qa.STAGES}
        self.assertEqual(stages['dbf-jpeg'],{'dbf':8,'largeDbf':1,'jpeg':120,'negative':6})
        self.assertEqual(stages['tiff'],{'cases':24,'negative':6})
        self.assertEqual(stages['html'],{'positive':18,'excluded':30,'negative':3})
        self.assertEqual(stages['viewer-150']['checks'],5)
    def test_native_sheet_count_keys_and_tiff_case_paths_bind_real_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'all-sheets';folder.mkdir()
            path=folder/'book-4.xlsx';path.write_bytes(b'generated workbook bytes')
            sha=qa.digest(path)
            self.assertEqual(qa.referenced_sources(folder,{'sourceSHA256':{'4':sha}})[0]['file'],path.name)
            tiff=Path(tmp)/'tiff';tiff.mkdir();source=tiff/'pages-8.tif';source.write_bytes(b'IFD control')
            rows=qa.referenced_sources(tiff,{'cases':[{'source':source.name,'sourceSha256':qa.digest(source)}]})
            self.assertEqual(rows[0]['file'],source.name)
            with self.assertRaises(ValueError):
                qa.referenced_sources(tiff,{'checks':[{'originalSha256':'0'*64}]})
    def test_new_parser_corpus_keeps_unknown_distinct_from_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows=qa.safety_negative_corpus(Path(tmp)/'new-controls')
            self.assertEqual(len(rows),12)
            self.assertEqual({row['name']:row['actualDecision'] for row in rows},qa.SAFETY_DECISIONS)
            self.assertEqual(next(r for r in rows if r['name']=='empty-result')['actualDecision'],'confirmed_loss')
            self.assertEqual(next(r for r in rows if r['name']=='dtd-entity')['actualDecision'],'not_checked')
