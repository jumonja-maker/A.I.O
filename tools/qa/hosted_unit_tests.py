"""Same full test discovery with explicit cross-platform accounting for hosted CI.

Only four exact Windows process tests are inapplicable on Linux; they must run
on the Windows matrix job. Unknown skips and expected failures stay INCOMPLETE.
"""
import argparse
import json
from pathlib import Path
import platform
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.qa.guard_v17_selfhosted_acceptance import suite_summary, write_json

LINUX_INAPPLICABLE = {
    'test_native_profile_safety_b27.RealWindowsOwnedTree.test_descendant_exit_and_unrelated_owned_process_survival': 'Real Windows Job Object control',
    'test_windows_suspended_start.SuspendedStartup.test_failed_assignment_never_resumes_uncontained_launcher': 'Real Windows suspended startup barrier',
    'test_windows_suspended_start.SuspendedStartup.test_failed_resume_reaps_launcher_without_running_its_code': 'Real Windows suspended startup barrier',
    'test_windows_suspended_start.SuspendedStartup.test_venv_launcher_is_suspended_before_assignment_and_child_is_in_owned_job': 'Real Windows suspended startup barrier',
}


def hosted_summary(result, system=None):
    system=system or platform.system()
    report=suite_summary(result, system=system)
    if system=='Linux':
        for row in report['skipped']:
            if LINUX_INAPPLICABLE.get(row['test'])==row['reason']:
                row['status']='not_applicable'
                row['requiredPlatform']='Windows'
        if report['status']!='FAIL':
            incomplete=bool(report['expectedFailures']) or any(row['status']=='INCOMPLETE' for row in report['skipped'])
            report['status']='INCOMPLETE' if incomplete else 'PASS'
    report['scope']='complete source suite on '+system+'; platform-specific tests require both matrix jobs'
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    report=hosted_summary(result)
    write_json(args.report, report)
    print('AIO_UNIT_EVIDENCE='+json.dumps(report,ensure_ascii=True),flush=True)
    return 1 if report['status']=='FAIL' else 2 if report['status']=='INCOMPLETE' else 0


if __name__=='__main__':raise SystemExit(main())
