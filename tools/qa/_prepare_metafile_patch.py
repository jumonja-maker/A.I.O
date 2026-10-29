"""Exact-base disposable preparation only; not part of the product branch."""
import hashlib
from pathlib import Path

expected = {
    'app_backend/converter_guard/native_metafile.py': 'c3028df84c298693ffb74dffbab1b1886d48e860',
    'app_backend/converter_guard/adapter.py': '3eefa7fbee87536e4275d62395d452938c90be2d',
    '.github/workflows/aio-hosted-qa.yml': '0813b89ed4721f9488aa6dd53e516c127563d951',
}
files = {}
for name, sha in expected.items():
    raw = Path(name).read_bytes()
    assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == sha, name
    files[name] = raw.decode('utf-8')
name = 'app_backend/converter_guard/native_metafile.py'
s = files[name]
assert s.count('def wrap_native_metafiles(') == 1
s = s[:s.index('def wrap_native_metafiles(')] + Path('tools/qa/_metafile_wrapper.pyfrag').read_text(encoding='utf-8')
s = s.replace('import hashlib\n', 'import hashlib\nimport json\nimport stat\nimport tempfile\n', 1)
files[name] = s
name = 'app_backend/converter_guard/adapter.py'
s = files[name]
old = "                self._local.export_info['resultChecks'] = [metafile_check]"
assert s.count(old) == 1
files[name] = s.replace(old, "                self._local.export_info['resultChecks'] = list(\n                    self._local.export_info.get('resultChecks', ())) + [metafile_check]")
name = '.github/workflows/aio-hosted-qa.yml'
s = files[name]
old = '          python tools/qa/fulltest_pptx_colours_native.py --executable "$AIO_QA_ENGINE" --output "$RUNNER_TEMP/aio-pptx-colours-native"'
assert s.count(old) == 1
s = s.replace(old, old + '\n          python tools/qa/fulltest_compressed_pages_native.py --executable "$AIO_QA_ENGINE" --output "$RUNNER_TEMP/aio-compressed-pages-native"')
old = "'aio-pptx-colours-native/evidence.json',"
assert s.count(old) == 1
files[name] = s.replace(old, old + "'aio-compressed-pages-native/evidence.json',")
for name, text in files.items():
    Path(name).write_bytes(text.encode('utf-8'))
print('METAFILE_EXACT_BASE_PATCH=3_FILES')
