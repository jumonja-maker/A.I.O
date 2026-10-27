"""Real HTTP/report recovery keeps conversion failure separate from analysis."""
from app_backend.converter_guard import GuardError
from app_backend.converter_guard.comparison_service import ComparisonService
import test_comparison_service_v19 as fixtures
import unittest


class FailedConversionFilter(unittest.TestCase):
    setUp=fixtures.ComparisonServiceContracts.setUp
    tearDown=fixtures.ComparisonServiceContracts.tearDown
    converted=fixtures.ComparisonServiceContracts.converted
    request=fixtures.ComparisonServiceContracts.request
    json_request=fixtures.ComparisonServiceContracts.json_request
    wait_analysis=fixtures.ComparisonServiceContracts.wait_analysis

    def test_failed_filter_and_legacy_report_recovery_use_owned_task_state(self):
        original=self.adapter.convert
        def convert(source,target,*args,**kwargs):
            if target=='pdf':raise GuardError('ENGINE_FAILED','Controlled native unit boundary')
            return original(source,target,*args,**kwargs)
        self.adapter.convert=convert
        state=self.converted(formats=['txt','pdf']);job=state['jobId']
        self.assertEqual((state['succeeded'],state['failed']),(1,1))
        self.json_request('compare/start',{'jobId':job});report=self.wait_analysis(job)
        self.assertEqual(report['conversionFailures'],1)
        failed=self.json_request('compare?job='+job+'&filter=conversion_error')
        self.assertEqual(failed['totalFiltered'],1)
        row=failed['items'][0]
        self.assertEqual((row['format'],row['conversionState'],row['status']),('pdf','error','not_checked'))
        self.assertEqual(row['findings'][0]['code'],'RESULT_NOT_CREATED')
        self.assertEqual(self.json_request('compare?job='+job+'&filter=checked')['totalFiltered'],1)
        # Existing stored payloads have no conversionState field; recovery
        # derives the new additive field without a destructive DB migration.
        self.server.api.comparison.close();self.server.api.comparison=ComparisonService(self.guard)
        recovered=self.json_request('compare?job='+job+'&filter=conversion_error')
        self.assertEqual(recovered['items'][0]['taskId'],row['taskId'])
        self.assertEqual(recovered['items'][0]['conversionState'],'error')
        self.assertEqual(self.guard.status(job)['succeeded'],1)
