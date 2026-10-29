"""One-shot reviewed source edit; removed from active CI after source commit."""
import hashlib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
BASE = 'app_backend/converter_guard/'
EXPECTED = {'source_contract.py':'28aebfdf05da18dbeca3314cc49449d83cd07fef','uno_session.py':'cad06265f057e14b142453a315a40d7011617c80','adapter.py':'43703cfad183f72f011d60132155810e6edecaea','export_recipe.py':'8944dbff241de8180d7337b286f77760793aa292','media_fidelity.py':'0225ceccb6fd75a00c132a2c193ddefcc556c4d1'}
texts = {}
for name, expected in EXPECTED.items():
    raw = (ROOT / BASE / name).read_bytes()
    actual = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if actual != expected:
        raise SystemExit('SOURCE_BLOB_CHANGED: ' + name + ' ' + actual)
    texts[name] = raw.decode('utf-8')
def replace(name, old, new):
    if texts[name].count(old) != 1:
        raise SystemExit('AMBIGUOUS_PATCH_ANCHOR: ' + name + ' ' + repr(old[:90]))
    texts[name] = texts[name].replace(old, new)
replace('source_contract.py', '        path = Path(source)\n        with path.open', "        path = Path(source)\n        from .graphic_source import tga_contract\n        graphic = tga_contract(path, cancel=cancel)\n        if graphic is not None:\n            return graphic\n        with path.open")
replace('source_contract.py', "    if contract.get('state') != 'recognized' or contract.get('contentClass') != 'flat_odf':", "    if contract.get('state') == 'recognized' and contract.get('contentClass') == 'tga_image':\n        from .graphic_source import tga_import_hint\n        return tga_import_hint(contract, native)\n    if contract.get('state') != 'recognized' or contract.get('contentClass') != 'flat_odf':")
replace('uno_session.py', 'from .sheet_outputs import SHEET_OUTPUT_SOURCE', 'from .sheet_outputs import SHEET_OUTPUT_SOURCE\nfrom .native_fidelity import NATIVE_FIDELITY_SOURCE')
replace('uno_session.py', 'WORKER_SOURCE = DIF_CORE_SOURCE + SHEET_OUTPUT_SOURCE +', 'WORKER_SOURCE = DIF_CORE_SOURCE + SHEET_OUTPUT_SOURCE + NATIVE_FIDELITY_SOURCE +')
replace('uno_session.py', "                    exported_paths = [target]\n                    try:", "                    exported_paths = [target]\n                    presentation_text = (aio_presentation_text(model)\n                        if req['format'] == 'xhtml' and\n                        'com.sun.star.presentation.PresentationDocument' in services else None)\n                    try:")
replace('uno_session.py', "                        if (req.get('graphicExport') == 'svm' and req['format'] == 'svm'", "                        graphic_pages = (aio_export_graphic_pages(model, remote, prop, target)\n                            if set(services).intersection(('com.sun.star.drawing.DrawingDocument',\n                                'com.sun.star.presentation.PresentationDocument')) else None)\n                        if graphic_pages is not None:\n                            exported_paths = graphic_pages\n                        elif (req.get('graphicExport') == 'svm' and req['format'] == 'svm'")
replace('uno_session.py', "req['exportFilter'] in ('DIF', 'dBase', 'Text - txt - csv (StarCalc)')", "req['exportFilter'] in ('DIF', 'dBase', 'SYLK', 'Text - txt - csv (StarCalc)')")
replace('uno_session.py', "                                exported_paths = automatic_csv_outputs(req, model)\n                    finally:", "                                exported_paths = automatic_csv_outputs(req, model)\n                        if presentation_text is not None:\n                            aio_verify_presentation_xhtml(exported_paths, presentation_text)\n                    finally:")
replace('uno_session.py', "'ENGINE_OUTPUT_CONTENT_LOSS' if isinstance(error, SheetOutputMismatch) else", "'ENGINE_OUTPUT_CONTENT_LOSS' if isinstance(error, (SheetOutputMismatch, AioPresentationTextLoss)) else\n                      'ENGINE_OUTPUT_VALIDATION_INCOMPLETE' if isinstance(error, AioPresentationUnverified) else")
replace('adapter.py', "if (target in ('dbf', 'dif') and not live_uno and", "if (target in ('dbf', 'dif', 'slk', 'sylk') and not live_uno and")
replace('export_recipe.py', 'WARNING_TEXT = {', "WARNING_TEXT = {\n    'NATIVE_SYLK_ALL_SHEETS': ('Каждый лист сохранён отдельным SLK/SYLK. Формулы представлены вычисленными значениями; межлистовые связи этим режимом не сохраняются.'),")
replace('export_recipe.py', "    selected = native.export_filter(target, services)\n", "    selected = native.export_filter(target, services)\n    if target == 'xhtml':\n        # HTML filters also claim the .xhtml extension. Require the real exporter.\n        names = {CALC: 'XHTML Calc File', WRITER: 'XHTML Writer File',\n                 DRAW: 'XHTML Draw File',\n                 'com.sun.star.presentation.PresentationDocument': 'XHTML Impress File'}\n        eligible = list(services)\n        if proof.get('nativeTextDocument') is True and WRITER not in eligible:\n            eligible.append(WRITER)\n        for service in eligible:\n            name = names.get(service)\n            item = native.filters.get(name)\n            if item and item[0] == service and 'EXPORT' in item[2]:\n                return ExportRecipe(name)\n        raise GuardError('XHTML_EXPORT_UNAVAILABLE',\n            'Установленный движок не подтвердил настоящий XHTML-экспорт. HTML не будет выдан под расширением XHTML.', stage='route')\n    if selected == 'SYLK' and target in ('slk', 'sylk'):\n        return ExportRecipe(selected, warnings=('NATIVE_SYLK_ALL_SHEETS',))\n")
replace('media_fidelity.py', "    # JPEG/BMP metadata alone does not validate all compressed image bytes.\n    # Identity is restricted to containers whose structure is fully traversed.\n    if ALIASES.get(target, target) not in ('png', 'gif', 'webp', 'tiff'):\n        return None\n    info = inspect_media(source, cancel=cancel)", "    canonical = ALIASES.get(target, target)\n    if canonical not in ('png', 'gif', 'webp', 'tiff', 'jpeg'):\n        return None\n    # JPEG: complete marker traversal through EOI and decode before identity.\n    info = inspect_media(source, cancel=cancel, validate_container=canonical == 'jpeg')")
replace('media_fidelity.py', "    if target == 'apng' and not info.get('animationContainer'):\n        return None\n    output_dir.mkdir", "    if target == 'apng' and not info.get('animationContainer'):\n        return None\n    if canonical == 'jpeg':\n        from .qt_media import _qt, _bounds\n        _bounds(info['width'], info['height'])\n        reader = _qt()[5](str(source))\n        _cancel(cancel)\n        image = reader.read()\n        if image.isNull():\n            raise GuardError('MEDIA_OUTPUT_INVALID', 'JPEG не декодируется; копия не выдана как готовый результат.', stage='result')\n        _cancel(cancel)\n        del image, reader\n    output_dir.mkdir")
for name, text in texts.items():
    compile(text, BASE + name, 'exec')
for name, text in texts.items():
    (ROOT / BASE / name).write_bytes(text.encode('utf-8'))
    print('PATCHED_SOURCE=' + BASE + name)
