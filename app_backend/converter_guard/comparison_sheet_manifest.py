"""Independently bind retained CSV files to source sheet indexes and names."""
import hashlib
import json
from pathlib import Path


def mapped_csv_summary(manifest,paths,before,budget,findings,checked,unknown,*,identity,csv_summary,compare_grids):
    stamp=identity(manifest)
    with manifest.open('rb') as stream:data=json.loads(budget.read(stream,1024*1024))
    grids=before.get('_grids',[]);entries=data.get('sheets');by_name={p.name:p for p in paths}
    if (data.get('schemaVersion')!=1 or data.get('scope')!='whole_workbook' or data.get('sheetIndexBase')!=0
            or type(data.get('sheetCount')) is not int or not isinstance(entries,list)
            or not before.get('complete') or len(entries)!=len(grids) or len(entries)!=len(paths)
            or data['sheetCount']!=len(entries) or len(by_name)!=len(paths)):
        raise ValueError('sheet_manifest_mapping_unavailable')
    if set(by_name)!={entry.get('file') for entry in entries if isinstance(entry,dict)}:
        raise ValueError('sheet_manifest_file_set_changed')
    identities=[];all_values=True
    for index,(entry,grid) in enumerate(zip(entries,grids)):
        if (type(entry.get('sourceSheetIndex')) is not int or entry['sourceSheetIndex']!=index
                or not isinstance(entry.get('sourceSheetName'),str) or entry['sourceSheetName']!=grid.get('name')):
            raise ValueError('sheet_manifest_source_identity_changed')
        path=by_name[entry['file']];identities.append(identity(path))
        digest=hashlib.sha256();size=0
        with path.open('rb') as stream:
            while block:=stream.read(65536):budget.check(len(block));digest.update(block);size+=len(block)
        if type(entry.get('size')) is not int or size!=entry['size'] or digest.hexdigest()!=entry.get('sha256'):
            raise ValueError('sheet_manifest_bytes_changed')
        after=csv_summary(path,budget);local=[];proven=set()
        compare_grids({'_grids':[grid]},after,local,proven,unknown)
        for finding in local:
            finding['sourceSheetIndex']=index;finding['sourceSheetName']=entry['sourceSheetName']
            for location in finding.get('locations',[]):location.update(sheetIndex=index,sheetName=entry['sourceSheetName'])
        findings.extend(local)
        all_values=all_values and not local and 'cell_values_single_grid' in proven
        if identity(path)!=identities[-1]:raise ValueError('sheet_changed_during_analysis')
    if identity(manifest)!=stamp:raise ValueError('sheet_manifest_changed_during_analysis')
    checked.add('all_sheet_outputs_mapping')
    if all_values:checked.add('all_sheet_values_by_index_name')
    unknown.update(('cell_types','cell_formatting','formula_recalculation'))
    return {'kind':'csv','documentClass':'spreadsheet','complete':True,'counts':{'sheetFiles':len(paths)},
        '_text':'','_grids':[], '_identities':[identity(path) for path in paths],
        '_manifestIdentity':stamp,'_manifestPath':str(manifest)}
