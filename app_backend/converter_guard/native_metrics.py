"""Bounded, technical durations. No sampling thread, paths or document data."""
import math

KEYS = frozenset({'adapterMs', 'processMs', 'launchMs', 'workerStartupMs',
                  'importMs', 'exportMs', 'closeMs', 'operationMs'})
SOURCES = frozenset({'cli_process', 'uno_document', 'qt_media'})


def clean_timings(value):
    if (not isinstance(value, dict) or not isinstance(value.get('source'), str)
            or value['source'] not in SOURCES):
        return None
    result = {'source': value['source']}
    for key in KEYS:
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 2 ** 53 and math.isfinite(number):
            result[key] = round(number)
    return result

