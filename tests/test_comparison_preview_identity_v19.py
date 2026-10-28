"""The actual preview subprocess refuses changed bytes before decoding."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from app_backend.converter_guard import comparison_worker as worker
from app_backend.converter_guard import comparison_analysis as analysis


class PreviewIdentity(unittest.TestCase):
    def invoke(self, folder, source, **options):
        request=folder/'request.json'
        request.write_text(json.dumps({'source':str(source),**options}),encoding='utf-8')
        result=subprocess.run([sys.executable,'-m','app_backend.converter_guard.comparison_worker',str(request)],
                              capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace')[-1000:])
        return json.loads((folder/'preview.json').read_text(encoding='utf-8'))

    def test_changed_source_and_result_are_never_presented_as_the_old_report(self):
        for mode in ('preview','pdf_preview','image_preview'):
            for side,code in (('source','SOURCE_CHANGED'),('result','RESULT_CHANGED')):
                with self.subTest(mode=mode,side=side),tempfile.TemporaryDirectory() as temporary:
                    folder=Path(temporary);source=folder/'data.txt';old=b'OLD VALUE';source.write_bytes(b'NEW VALUE')
                    data=self.invoke(folder,source,mode=mode,previewSide=side,
                                     expectedSha256=hashlib.sha256(old).hexdigest())
                    self.assertEqual(data['kind'],'unavailable');self.assertEqual(data['code'],code)
                    self.assertNotIn('NEW VALUE',json.dumps(data));self.assertFalse((folder/'preview.png').exists())

    def test_matching_preview_remains_literal_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);source=folder/'data.txt';content=b'<script>source</script>';source.write_bytes(content)
            data=self.invoke(folder,source,mode='preview',previewSide='source',
                             expectedSha256=hashlib.sha256(content).hexdigest())
            self.assertEqual(data['kind'],'text');self.assertEqual(data['text'],content.decode())

    def test_mutation_during_decoder_never_publishes_old_identity(self):
        for side in ('source','result'):
            with self.subTest(side=side),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);source=root/'data.txt';source.write_bytes(b'ORIGINAL')
                request=root/'request.json';request.write_text(json.dumps({'mode':'preview','source':str(source),
                    'expectedSha256':hashlib.sha256(b'ORIGINAL').hexdigest(),'previewSide':side}))
                def mutate(path):
                    path.write_bytes(b'MUTATED AFTER HASH')
                    (root/'preview.png').write_bytes(b'stale image')
                    return {'kind':'text','text':path.read_text()}
                with mock.patch.object(worker,'_apply_memory_budget'),mock.patch.object(analysis,'preview_text',side_effect=mutate):
                    worker.main(request)
                packet=json.loads((root/'preview.json').read_text())
                self.assertEqual(packet['kind'],'unavailable')
                self.assertEqual(packet['code'],side.upper()+'_CHANGED')
                self.assertNotIn('MUTATED',json.dumps(packet));self.assertFalse((root/'preview.png').exists())

    def test_mutation_during_analysis_cannot_produce_quality_proof(self):
        for stage in ('summary','result','source_during_pair'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);source=root/'source.txt';target=root/'target.txt'
                source.write_bytes(b'ORIGINAL');target.write_bytes(b'ORIGINAL')
                digest=hashlib.sha256(b'ORIGINAL').hexdigest()
                request=root/'request.json';request.write_text(json.dumps({'source':str(source),'sourceSha256':digest,
                    'pairs':[{'taskId':'task','target':'txt','paths':[str(target)],'sha256':{str(target):digest}}]}))
                collect,compare=analysis.collect_source_summary,analysis.analyze_pair
                def summary(path,**kwargs):
                    if stage=='summary':path.write_bytes(b'NEW SOURCE')
                    return collect(path,**kwargs)
                def pair(*args,**kwargs):
                    if stage=='result':target.write_bytes(b'NEW RESULT')
                    if stage=='source_during_pair':source.write_bytes(b'NEW SOURCE')
                    return compare(*args,**kwargs)
                with mock.patch.object(worker,'_apply_memory_budget'),mock.patch.object(analysis,'collect_source_summary',side_effect=summary),mock.patch.object(analysis,'analyze_pair',side_effect=pair):
                    worker.main(request)
                packet=json.loads((root/'outcomes.jsonl').read_text())['outcome']
                self.assertEqual(packet['status'],'not_checked');self.assertEqual(packet['checkedFeatures'],[])
                self.assertEqual(packet['findings'][0]['code'],'RESULT_CHANGED' if stage=='result' else 'SOURCE_CHANGED')
