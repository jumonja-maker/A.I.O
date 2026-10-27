"""Exact stress2 CSV/TAB/TSV bytes, verified against the preserved 680-file ZIP.

These hashes belong to original sources, not newly converted outputs. TAB and
TSV have identical bytes per level but retain separate native route tests.
"""
import csv
from datetime import date, timedelta
import hashlib
import io

CSV_SHA = (
 '93626c4c28eb5a2338ac85c4cc1fe3f45877212ffee402c998516a4f270c242d',
 '11960149dfb4f9e4200547fb13f8a7b724192a2793b9cbbdf9dc3f03556cfc0a',
 'b5be76ac749c60c8f059e9e31a49ccfd1430ff51e30b3fa5495d00e8a808e3f4',
 'c1ef8506518e6efe91f673341159e3c2406ec773888fe79bd650e5b15c558f8a',
 '4f1c683c39e797db80383f0225f7520b36db71d3fa78b5de899617da22d7dab9',
 'f56f87db7021a50967573495cdeab5c83b50321d5fc7d169b8fbdb5334392b32',
 '14571c45e511ce7071e724b052c1809439ee29ecdda7768e9bf37f2800c5b8f9',
 '9e4e9a6333fa48c4ca5ef036cb70eefc7e7d8d64d6c0e1ab0f6d2f8510515bdc',
)
TAB_SHA = (
 '1811335c19e2de6a60c03427a1507466e3bd98e2f174a144a4267e70e197b354',
 'cd055a37beecf9cc7280cc48a62a9341fe54e069eb97eb74d7d2cc4fc057f727',
 '4a97d348c658685294a5168abb3841d0e0ccd0534952f9a3ec66376bef8c7708',
 '904caa9ed92196579b07fc782fc5da3258cd96f8ee55be002b855729d9bee3b6',
 'd453ff9dba24132a64677f5c7c8394a1fe4bdc5017306c78e1c922b420a8d8a4',
 '0e12b56930483ae8525453beac9388736cafbdc6fa7b65b02899a3e7a4f0be4b',
 '8e31e77deaef3286c2251445a60bb11c222b0087400c65bcd1bd1edaf11ecdd7',
 '07b3c8e84bf76bde29b34666ea1be0cc397ebe3ae2907a1f89d29e3451ac4b63',
)
SHA256 = {'csv': CSV_SHA, 'tab': TAB_SHA, 'tsv': TAB_SHA}


def original_csv(level, extension):
    if type(level) is not int or not 1 <= level <= 8 or extension not in SHA256:
        raise ValueError('fixture_level_or_extension')
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter=',' if extension == 'csv' else '\t')
    writer.writerow(('ID', 'Label', 'Amount', 'Rate', 'Date', 'Memo'))
    for i in range(1, 7 + 9 * level):
        writer.writerow((f'{i:03d}', f'Строка {i} / QA L{level}', f'{i*1.43:.2f}',
            f'{((i%11)-5)/100:.3f}', str(date(2026, 10, 26) + timedelta(days=i)),
            f'Batch {i}' if level < 4 else f'Комментарий {i}; рубрика, кавычка " и ё'))
    result = stream.getvalue().encode('utf-8')
    if hashlib.sha256(result).hexdigest() != SHA256[extension][level - 1]:
        raise AssertionError('original fixture bytes do not match preserved source')
    return result
