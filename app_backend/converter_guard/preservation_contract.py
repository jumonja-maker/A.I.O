"""Small preservation contracts: metadata in, execution requirement out.

The same trusted source is embedded into LibreOffice's isolated UNO worker.
No source parsing, I/O, process launch, registry query or optional quality scan
belongs here. These contracts describe the *imported model*, not an independent
proof that the source importer retained every property of the original file.
"""
PRESERVATION_POLICY_SOURCE = r'''
from collections import namedtuple

AioPreservationPlan = namedtuple('AioPreservationPlan',
    'policy_id unit expected target scope')

class AioPreservationIncomplete(RuntimeError):
    pass

class AioPreservationLoss(RuntimeError):
    pass

_AIO_CALC = 'com.sun.star.sheet.SpreadsheetDocument'
_AIO_WRITER = 'com.sun.star.text.TextDocument'
_AIO_DRAW = 'com.sun.star.drawing.DrawingDocument'
_AIO_IMPRESS = 'com.sun.star.presentation.PresentationDocument'
_AIO_SHEET_TARGETS = frozenset(('csv', 'tab', 'tsv', 'dbf', 'dif', 'slk', 'sylk'))
_AIO_PAGE_TARGETS = frozenset(('png', 'jpg', 'jpeg', 'jfif', 'jif', 'jpe',
                             'webp', 'bmp', 'gif', 'tif', 'tiff', 'svg', 'emf', 'wmf'))
_AIO_WRITER_TARGETS = frozenset(('png', 'jpg', 'jpeg', 'jfif', 'jif', 'jpe', 'webp'))


def aio_preservation_scope(services, target):
    """Select only currently implemented contracts. No extension-based source guess."""
    if _AIO_CALC in services and target in _AIO_SHEET_TARGETS:
        return ('native_all_sheets_v1', 'sheets', 'loaded_spreadsheet_sheets')
    if target in _AIO_PAGE_TARGETS and (_AIO_DRAW in services or _AIO_IMPRESS in services):
        return ('native_all_draw_pages_v1', 'pages', 'loaded_draw_pages')
    if _AIO_WRITER in services and target in _AIO_WRITER_TARGETS:
        return ('native_all_writer_pages_v1', 'pages', 'loaded_writer_layout')
    return None


def aio_plan_preservation(services, target, count):
    scope = aio_preservation_scope(services, target)
    if scope is None:
        return None
    if type(count) is not int or count < 1:
        # Missing/invalid metadata is not permission to assume a single page.
        raise AioPreservationIncomplete('native_unit_count_unconfirmed')
    policy_id, unit, basis = scope
    return AioPreservationPlan(policy_id, unit, count, target, basis)


def aio_verify_preservation(plan, output_names):
    """Check the executor's existing output list after its file validation.

    Sidecars do not count as pages/sheets. No output or source is reopened.
    Equal bytes do not imply a duplicate page: genuinely identical pages are
    legal. Page content and appearance remain separate from this count check.
    """
    if plan is None:
        return None
    suffix = '.' + plan.target
    primary = [name for name in output_names
               if isinstance(name, str) and name.lower().endswith(suffix)]
    if any('/' in name or '\\' in name or name in ('.', '..') for name in primary):
        raise AioPreservationIncomplete('native_output_name_not_local')
    if len(primary) != len({name.casefold() for name in primary}):
        raise AioPreservationLoss('native_unit_outputs_not_unique')
    if len(primary) != plan.expected:
        raise AioPreservationLoss('native_unit_count_mismatch:%d:%d' %
                                  (plan.expected, len(primary)))
    return {'check': 'native_preservation_contract', 'status': 'checked_subset',
            'schemaVersion': 1, 'policyId': plan.policy_id,
            'unit': plan.unit, 'expectedUnits': plan.expected,
            'observedOutputs': len(primary), 'scope': plan.scope,
            'basis': 'already_loaded_native_model', 'qualityCompared': False,
            'checkedFeatures': ['output_count', 'unique_output_names'],
            'notChecked': ['original_import_equivalence', 'visual_equivalence']}
'''

# Trusted constant only: the application and embedded UNO worker use exactly
# the same algorithm, rather than maintaining independent policy copies.
exec(compile(PRESERVATION_POLICY_SOURCE, '<aio_preservation_policy>', 'exec'))
