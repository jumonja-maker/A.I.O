"""Additive, property-scoped evidence; ready and saved are not quality PASS."""


def task_evidence(task, *, quality_requested=False):
    artifacts = task.artifacts
    saved = sum(a.saved for a in artifacts)
    quality = task.quality or {}
    checked, unknown = [], []
    for check in task.result_checks:
        if not isinstance(check, dict):
            continue
        status = check.get('status')
        if status in ('checked', 'checked_subset', 'byte_preserved'):
            features = check.get('checkedFeatures') or [check.get('check')]
            checked.extend(f for f in features if isinstance(f, str))
        if status in ('not_checked', 'uncertain'):
            unknown.append(check.get('reason') or check.get('check') or 'check_incomplete')
        unknown.extend(r for r in check.get('unknownReasons', []) if isinstance(r, str))
    compared = quality.get('qualityCompared') is True
    if not compared:
        unknown.append(quality.get('reason') or ('quality_comparison_incomplete'
                       if quality_requested else 'quality_comparison_not_requested'))
    limitations = []
    if task.format in ('csv', 'tsv', 'tab'):
        limitations = ['formulas_as_values', 'styles_and_charts_not_represented', 'one_table_per_file']
    return {
        'conversionStatus': 'ready' if task.state in ('ready', 'saved') else task.state,
        'saveStatus': ('not_applicable' if not artifacts else 'saved' if saved == len(artifacts)
                       else 'partial' if saved else 'unsaved'),
        'readbackVerified': bool(artifacts) and all(a.readback_verified for a in artifacts),
        'qualityCheckRequested': quality_requested, 'qualityCompared': compared,
        'checkedFeatures': list(dict.fromkeys(checked)),
        'unknownReasons': list(dict.fromkeys(unknown)),
        'formatLimitations': limitations,
        'confirmedLoss': task.code in ('ENGINE_OUTPUT_CONTENT_LOSS', 'ENGINE_OUTPUT_CONTENT_EMPTY'),
    }
