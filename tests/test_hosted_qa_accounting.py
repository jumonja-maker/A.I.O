"""Hosted platform accounting never turns an unknown/missing check into PASS."""
from types import SimpleNamespace
import unittest
from tools.qa.hosted_unit_tests import hosted_summary, LINUX_INAPPLICABLE


def result(test_id, reason, *, errors=None):
    case=SimpleNamespace(id=lambda:test_id)
    return SimpleNamespace(testsRun=2, failures=[], errors=errors or [],
                           skipped=[(case,reason)], expectedFailures=[], unexpectedSuccesses=[])


class HostedAccounting(unittest.TestCase):
    def test_exact_windows_fixture_is_inapplicable_only_on_linux(self):
        name,reason=next(iter(LINUX_INAPPLICABLE.items()))
        report=hosted_summary(result(name,reason),system='Linux')
        self.assertEqual(report['status'],'PASS')
        self.assertEqual(report['passed'],1)
        self.assertEqual(report['skipped'][0]['requiredPlatform'],'Windows')
        self.assertEqual(hosted_summary(result(name,reason),system='Windows')['status'],'INCOMPLETE')
    def test_same_reason_on_unknown_test_is_not_whitelisted(self):
        self.assertEqual(hosted_summary(result('other.test','Real Windows Job Object control'),system='Linux')['status'],'INCOMPLETE')
    def test_known_test_with_other_skip_reason_is_incomplete(self):
        name=next(iter(LINUX_INAPPLICABLE))
        self.assertEqual(hosted_summary(result(name,'missing dependency'),system='Linux')['status'],'INCOMPLETE')
    def test_platform_skip_never_masks_an_error(self):
        name,reason=next(iter(LINUX_INAPPLICABLE.items()))
        report=hosted_summary(result(name,reason,errors=[(SimpleNamespace(id=lambda:'broken'),'traceback')]),system='Linux')
        self.assertEqual(report['status'],'FAIL')


if __name__=='__main__':unittest.main()
