"""Native Base report definitions use storage APIs, not Desktop/XStorable.

The fixed worker source opens only content-confirmed report packages. Its actual
native XML filter must return success before a ReportDefinition admits routes.
It never connects to a database or executes the report's query.
"""

REPORT_STORAGE_SOURCE = r'''
import hashlib as _report_hashlib
import json as _report_json
import math as _report_math

_AIO_REPORT_SERVICE = 'com.sun.star.report.ReportDefinition'
_AIO_REPORT_FILTER = 'StarOffice XML (Base) Report'

class AioReportStorageUnverified(RuntimeError):
    pass


def aio_report_filter(remote, flag):
    factory = remote.ServiceManager.createInstanceWithContext(
        'com.sun.star.document.FilterFactory', remote)
    if factory is None or not factory.hasByName(_AIO_REPORT_FILTER):
        raise AioReportStorageUnverified('report_native_filter_missing')
    fields = {p.Name:p.Value for p in factory.getByName(_AIO_REPORT_FILTER)}
    flags = fields.get('Flags')
    if (fields.get('DocumentService') != _AIO_REPORT_SERVICE
            or type(flags) is not int or not flags & flag):
        raise AioReportStorageUnverified('report_native_filter_unconfirmed')
    if fields.get('FilterService') not in (None, '', 'com.sun.star.comp.report.OReportFilter'):
        raise AioReportStorageUnverified('report_native_importer_changed')
    return fields


def aio_load_report_storage(remote, prop, source, options):
    """One native XML import with its boolean result, into a real live model.

    ReportDefinition.load()/loadFromStorage() discard the filter's boolean;
    checking only the model object could therefore accept a partial import.
    Use the same storage/filter APIs explicitly and retain that success result.
    """
    import uno
    aio_report_filter(remote, 1)
    service = remote.ServiceManager
    factory = service.createInstanceWithContext('com.sun.star.embed.StorageFactory', remote)
    model = storage = None
    try:
        storage = factory.createInstanceWithArguments((source.as_uri(), 1))
        model = service.createInstanceWithContext(_AIO_REPORT_SERVICE, remote)
        if (model is None or not model.supportsService(_AIO_REPORT_SERVICE)
                or not model.queryInterface(uno.getTypeByName('com.sun.star.document.XStorageBasedDocument'))):
            raise AioReportStorageUnverified('report_native_storage_model_missing')
        model.switchToStorage(storage)
        arguments = tuple(options) + (prop('DocumentBaseURL', source.as_uri()),
                                       prop('FilterName', _AIO_REPORT_FILTER))
        if model.attachResource(source.as_uri(), arguments) is not True:
            raise AioReportStorageUnverified('report_native_resource_unconfirmed')
        importer = service.createInstanceWithContext('com.sun.star.comp.report.OReportFilter', remote)
        if importer is None:
            raise AioReportStorageUnverified('report_native_importer_missing')
        importer.setTargetDocument(model)
        if importer.filter(arguments + (prop('Storage', storage),)) is not True:
            raise AioReportStorageUnverified('report_native_import_failed')
        return model
    except BaseException:
        if model is not None:
            try: model.dispose()
            except Exception: pass
        if storage is not None:
            try: storage.dispose()
            except Exception: pass
        raise


def aio_report_profile(model):
    """Bounded native design semantics; never execute SQL or a database link."""
    if not model.supportsService(_AIO_REPORT_SERVICE):
        raise AioReportStorageUnverified('report_model_service_changed')
    budget = [0, 0]
    def scalar(value):
        if isinstance(value, str):
            budget[1] += len(value.encode('utf-8'))
            if budget[1] > 4 * 1024 * 1024:
                raise AioReportStorageUnverified('report_design_text_budget')
            return value
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float and _report_math.isfinite(value):
            return value
        if hasattr(value, 'typeName') and hasattr(value, 'value'):
            return [str(value.typeName), str(value.value)]
        raise AioReportStorageUnverified('report_design_property_unconfirmed')
    def fields(obj, names):
        result = {}
        info = obj.getPropertySetInfo()
        for name in names:
            if info.hasPropertyByName(name):
                result[name] = scalar(obj.getPropertyValue(name))
        return result
    def section(obj):
        count = obj.getCount()
        if type(count) is not int or not 0 <= count <= 16384:
            raise AioReportStorageUnverified('report_component_count_unconfirmed')
        result = {'properties':fields(obj, ('Height', 'Visible', 'KeepTogether',
                  'ForceNewPage', 'NewRowOrCol', 'ConditionalPrintExpression')), 'components':[]}
        for index in range(count):
            budget[0] += 1
            if budget[0] > 16384:
                raise AioReportStorageUnverified('report_component_budget')
            component = obj.getByIndex(index)
            point, size = component.getPosition(), component.getSize()
            result['components'].append({'services':sorted(component.getSupportedServiceNames()),
                'position':[point.X, point.Y], 'size':[size.Width, size.Height],
                'properties':fields(component, ('Name', 'Label', 'DataField',
                    'ConditionalPrintExpression', 'PrintRepeatedValues', 'PrintWhenGroupChange',
                    'ImageURL', 'HyperLinkURL', 'ScaleMode', 'Orientation', 'ParaAdjust',
                    'CharHeight', 'CharColor', 'CharWeight'))})
        return result
    # These IDL members are readonly attributes in Python UNO. Java's generated
    # getDetail/getGroups/getFunctions accessors are not Python UNO methods.
    profile = {'properties':fields(model, ('Caption', 'Command', 'CommandType',
                   'DataSourceName', 'EscapeProcessing', 'Filter', 'MimeType',
                   'PageHeaderOn', 'PageFooterOn', 'ReportHeaderOn', 'ReportFooterOn')),
               'sections':{'Detail':section(model.Detail)}, 'groups':[], 'functions':[]}
    for name in ('PageHeader', 'PageFooter', 'ReportHeader', 'ReportFooter'):
        if profile['properties'].get(name+'On') is True:
            profile['sections'][name] = section(getattr(model, name))
    groups = model.Groups
    if not 0 <= groups.getCount() <= 256:
        raise AioReportStorageUnverified('report_group_budget')
    for index in range(groups.getCount()):
        group = groups.getByIndex(index)
        item = {'properties':fields(group, ('Expression', 'GroupOn', 'GroupInterval',
                    'SortAscending', 'KeepTogether', 'ResetPageNumber', 'StartNewColumn',
                    'HeaderOn', 'FooterOn'))}
        for name in ('Header', 'Footer'):
            if item['properties'].get(name+'On') is True:
                item[name] = section(getattr(group, name))
        profile['groups'].append(item)
    functions = model.Functions
    if not 0 <= functions.getCount() <= 4096:
        raise AioReportStorageUnverified('report_function_budget')
    for index in range(functions.getCount()):
        profile['functions'].append(fields(functions.getByIndex(index),
            ('Name', 'Formula', 'InitialFormula', 'PreEvaluated', 'DeepTraversing')))
    payload = _report_json.dumps(profile, sort_keys=True, ensure_ascii=True, allow_nan=False)
    return profile, {'controls':budget[0], 'groups':len(profile['groups']),
                     'functions':len(profile['functions']),
                     'designSha256':_report_hashlib.sha256(payload.encode('ascii')).hexdigest()}


def aio_export_report_storage(model, remote, prop, source, target, options):
    """Native definition package, then independent native import/readback."""
    aio_report_filter(remote, 2)
    before, summary = aio_report_profile(model)
    if target.exists() or target.is_symlink():
        raise AioReportStorageUnverified('report_output_collision')
    factory = remote.ServiceManager.createInstanceWithContext('com.sun.star.embed.StorageFactory', remote)
    output = checked = None
    try:
        with target.open('xb'):
            pass
        # ElementModes.READWRITE is 7 (READ=1, SEEKABLE=2, WRITE=4).
        # 11 is READ|SEEKABLE|TRUNCATE and lacks WRITE. The new owned file
        # above is already empty, so no truncation flag is needed.
        output = factory.createInstanceWithArguments((target.as_uri(), 7))
        model.storeToStorage(output, tuple(options) + (prop('DocumentBaseURL', source.as_uri()),))
        output.commit()
        output.dispose()
        output = None
        load_options = [prop('Hidden', True), prop('ReadOnly', True), prop('Silent', True),
                        prop('MacroExecutionMode', 0), prop('UpdateDocMode', 0)]
        load_options.extend(option for option in options if option.Name == 'InteractionHandler')
        checked = aio_load_report_storage(remote, prop, target, load_options)
        after, checked_summary = aio_report_profile(checked)
        if before != after:
            raise AioReportStorageUnverified('report_design_roundtrip_changed')
        return {'check':'native_report_definition_roundtrip', 'status':'checked_subset',
                'basis':'loaded_native_report_storage_and_independent_native_readback',
                **summary, 'readbackDesignSha256':checked_summary['designSha256'],
                'checkedFeatures':['sections', 'control_count', 'control_names', 'labels',
                    'data_fields', 'positions', 'sizes', 'groups', 'functions'],
                'notChecked':['executed_database_rows', 'rendered_layout', 'visual_equivalence'],
                'qualityCompared':False}
    finally:
        if checked is not None:
            checked.dispose()
        if output is not None:
            output.dispose()
'''
