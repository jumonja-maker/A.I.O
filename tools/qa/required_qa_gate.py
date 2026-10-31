"""Fail-closed required-check policy; no artifacts, installation or publication."""
from __future__ import annotations

import argparse
import json
import os


def validate_steps(platform, steps):
    if platform not in ('Windows', 'Linux') or not isinstance(steps, dict):
        raise ValueError('Unknown QA platform or malformed step evidence')
    required = ['deps', 'units']
    required += (['windows_engine', 'fidelity', 'vector_pages', 'package']
                 if platform == 'Windows' else ['linux_engine'])
    rejected = []
    for name in required:
        step = steps.get(name)
        if not isinstance(step, dict) or any(
                step.get(field) != 'success' for field in ('outcome', 'conclusion')):
            rejected.append(name)
    if rejected:
        raise ValueError('Required QA steps did not succeed: ' + ', '.join(rejected))
    return required


def validate_matrix(result):
    if result != 'success':
        raise ValueError('Windows/Linux QA matrix did not succeed: ' + str(result))


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--platform', choices=('Windows', 'Linux'))
    mode.add_argument('--matrix', action='store_true')
    args = parser.parse_args()
    if args.matrix:
        validate_matrix(os.environ.get('AIO_QA_MATRIX_RESULT'))
        print('AIO_REQUIRED_QA_GATE=PASS')
    else:
        required = validate_steps(args.platform, json.loads(os.environ['AIO_QA_STEPS']))
        print('AIO_REQUIRED_PLATFORM_STEPS=' + json.dumps(required))
        print('AIO_REQUIRED_PLATFORM_GATE=PASS')


if __name__ == '__main__':
    main()
