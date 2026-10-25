"""Actual HTTP boundaries: stale UI commands and malformed selectors stay inert."""
import json
import unittest
from urllib.parse import urlencode

import test_comparison_service_v19 as fixtures


class ComparisonHttpEdges(unittest.TestCase):
    setUp=fixtures.ComparisonServiceContracts.setUp
    tearDown=fixtures.ComparisonServiceContracts.tearDown
    request=fixtures.ComparisonServiceContracts.request
    json_request=fixtures.ComparisonServiceContracts.json_request
    converted=fixtures.ComparisonServiceContracts.converted
    analyze=fixtures.ComparisonServiceContracts.analyze
    wait_analysis=fixtures.ComparisonServiceContracts.wait_analysis
    assert_http_code=fixtures.ComparisonServiceContracts.assert_http_code

    def test_restart_is_strict_boolean_and_does_not_create_a_new_session_on_bad_input(self):
        state=self.converted();current=self.analyze(state);identity=state['jobId']
        for invalid in (None,1,0,'true',[],{}):
            with self.subTest(value=invalid):
                self.assert_http_code(400,'compare/start',{'jobId':identity,'restart':invalid})
                same=self.json_request('compare?'+urlencode({'job':identity}))
                self.assertEqual(same['analysisId'],current['analysisId'])
        self.assertEqual(len(self.adapter.calls),1)

    def test_stale_control_restart_and_detail_cannot_affect_a_new_analysis(self):
        state=self.converted();old=self.analyze(state);identity=state['jobId']
        fresh=self.json_request('compare/start',{'jobId':identity,'restart':True,'analysisId':old['analysisId']})
        self.assertNotEqual(fresh['analysisId'],old['analysisId']);fresh=self.wait_analysis(identity)
        for action in ('pause','resume','cancel'):
            self.assert_http_code(409,'compare/control',{'jobId':identity,'action':action,'analysisId':old['analysisId']})
        self.assert_http_code(409,'compare/start',{'jobId':identity,'restart':True,'analysisId':old['analysisId']})
        task=fresh['items'][0]['taskId']
        self.assert_http_code(409,'compare/item?'+urlencode({'job':identity,'task':task,'analysisId':old['analysisId']}))
        detail=self.json_request('compare/item?'+urlencode({'job':identity,'task':task,'analysisId':fresh['analysisId']}))
        self.assertEqual(detail['analysisId'],fresh['analysisId'])
        final=self.json_request('compare?'+urlencode({'job':identity}))
        self.assertEqual(final['state'],'completed');self.assertEqual(final['analysisId'],fresh['analysisId'])

    def test_malformed_preview_and_history_queries_never_start_native_work(self):
        state=self.converted();data=self.analyze(state);identity=state['jobId'];task=data['items'][0]['taskId']
        preview='compare/preview?'+urlencode({'job':identity,'task':task,'side':'result'})
        for query in ('&outputIndex=-1','&outputIndex=1.5','&outputIndex=',
                      '&outputIndex=0&outputIndex=1','&page=0&page=1'):
            self.assert_http_code(400,preview+query)
        history='compare/retry-history?'+urlencode({'job':identity})
        for query in ('&offset=-1','&limit=0','&limit=101','&limit=2&limit=3','&runId=',
                      '&runId=../../outside','&runId='+('a'*33)):
            self.assert_http_code(400,history+query)
        self.json_request(history)
        self.assertEqual(len(self.adapter.calls),1);self.assertEqual(self.guard._jobs[identity].io,0)

    def test_changed_source_and_result_preview_are_not_the_cached_checked_file(self):
        state=self.converted(data=b'OLD VALUE');data=self.analyze(state);identity=state['jobId'];task=data['items'][0]['taskId']
        detail=self.json_request('compare/item?'+urlencode({'job':identity,'task':task}))
        self.guard._jobs[identity].files[0].source.write_bytes(b'NEW VALUE')
        source=self.json_request(detail['previews']['source']['url'].removeprefix('/api/converter-guard/'))
        self.assertEqual(source['code'],'SOURCE_CHANGED');self.assertEqual(source['kind'],'unavailable')
        self.guard._jobs[identity].artifacts[0].path.write_bytes(b'NEW VALUE')
        output=self.json_request(detail['previews']['result']['url'].removeprefix('/api/converter-guard/'))
        self.assertEqual(output['code'],'RESULT_CHANGED');self.assertEqual(output['kind'],'unavailable')
        self.assertNotIn('NEW VALUE',json.dumps(source)+json.dumps(output));self.assertEqual(len(self.adapter.calls),1)

    def test_new_endpoints_and_session_commands_require_current_credentials(self):
        state=self.converted();data=self.analyze(state);identity=state['jobId']
        self.assert_http_code(403,'compare/retry-history?'+urlencode({'job':identity}),auth=False)
        self.assert_http_code(403,'compare/control',{'jobId':identity,'action':'cancel','analysisId':data['analysisId']},auth=False)
        for value in (None,12,[],{},'bad', 'f'*33):
            self.assert_http_code(400,'compare/control',{'jobId':identity,'action':'cancel','analysisId':value})
        same=self.json_request('compare?'+urlencode({'job':identity}))
        self.assertEqual(same['state'],'completed');self.assertEqual(same['analysisId'],data['analysisId'])
