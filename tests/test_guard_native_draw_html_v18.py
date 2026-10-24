"""All native drawing pages survive private flat snapshots and HTML packaging."""
import base64
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, unquote

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.native_draw_html import (
    native_draw_html_plan, prepare_draw_page_snapshots, wrap_svg_pages, convert_native_draw_html,
    DRAW_SERVICE, PRESENTATION_SERVICE)

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
MARKERS = ('AIO_VECTOR_PAGE_ONE_018', 'AIO_VECTOR_PAGE_TWO_018')


def make_flat_drawing(path, *, presentation=False):
    """Native-readable two-page positive control with text, vectors and PNGs."""
    mime = 'presentation' if presentation else 'graphics'
    model = 'presentation' if presentation else 'drawing'
    prefix = '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    prefix += 'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    prefix += 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    prefix += 'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    prefix += 'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    prefix += 'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    prefix += 'xmlns:xlink="http://www.w3.org/1999/xlink" '
    prefix += 'office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.' + mime + '">'
    prefix += '<office:automatic-styles><style:page-layout style:name="PM1"><style:page-layout-properties '
    prefix += 'fo:page-width="21cm" fo:page-height="14.8cm" style:print-orientation="landscape"/></style:page-layout>'
    prefix += '<style:style style:name="dp1" style:family="drawing-page"><style:drawing-page-properties '
    prefix += 'draw:fill="solid" draw:fill-color="#ffffff"/></style:style>'
    prefix += '<style:style style:name="P1" style:family="paragraph"><style:text-properties '
    prefix += 'fo:font-size="20pt" fo:color="#143f5b"/></style:style></office:automatic-styles>'
    prefix += '<office:master-styles><style:master-page style:name="Default" style:page-layout-name="PM1" '
    prefix += 'draw:style-name="dp1"/></office:master-styles><office:body><office:' + model + '>'
    pages = []
    for index, marker in enumerate(MARKERS, 1):
        page = '<draw:page draw:name="Page' + str(index) + '" draw:style-name="dp1" draw:master-page-name="Default">'
        page += '<draw:frame svg:x="1cm" svg:y="1cm" svg:width="18cm" svg:height="3cm"><draw:text-box>'
        page += '<text:p text:style-name="P1">' + marker + '</text:p></draw:text-box></draw:frame>'
        page += '<draw:rect svg:x="1cm" svg:y="5cm" svg:width="7cm" svg:height="2cm"/>'
        page += '<draw:frame svg:x="10cm" svg:y="5cm" svg:width="2cm" svg:height="2cm"><draw:image>'
        page += '<office:binary-data>' + base64.b64encode(PNG).decode() + '</office:binary-data>'
        page += '</draw:image></draw:frame></draw:page>'
        pages.append(page)
    Path(path).write_text(prefix + ''.join(pages) + '</office:' + model + '></office:body></office:document>')
    return Path(path)


class NativeDrawHtml(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cancel = threading.Event()

    def test_flat_snapshots_keep_styles_geometry_media_and_exact_selected_pages(self):
        source = make_flat_drawing(self.root / 'source.fodg')
        before = source.read_bytes()
        paths, check = prepare_draw_page_snapshots(source, self.root / 'snapshots', cancel=self.cancel)
        self.assertEqual(check['pageCount'], 2)
        self.assertEqual([p['imageElements'] for p in check['pages']], [1, 1])
        for index, path in enumerate(paths):
            data = path.read_bytes()
            self.assertIn(MARKERS[index].encode(), data)
            self.assertNotIn(MARKERS[1 - index].encode(), data)
            self.assertIn(b'<office:master-styles>', data)
            self.assertIn(base64.b64encode(PNG), data)
            tree = ET.fromstring(data)
            pages = tree.findall('.//{urn:oasis:names:tc:opendocument:xmlns:office:1.0}drawing/'
                                 '{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}page')
            self.assertEqual(len(pages), 1)
        self.assertEqual(source.read_bytes(), before)

    def test_presentation_uses_native_flat_presentation_without_changing_page_data(self):
        source = make_flat_drawing(self.root / 'source.fodp', presentation=True)
        paths, check = prepare_draw_page_snapshots(source, self.root / 'snapshots', cancel=self.cancel)
        self.assertEqual([p.suffix for p in paths], ['.fodp', '.fodp'])
        self.assertEqual(check['pageCount'], 2)

    def test_external_visual_resources_fail_without_fetching(self):
        source = make_flat_drawing(self.root / 'source.fodg')
        source.write_text(source.read_text().replace('<draw:image>', '<draw:image xlink:href="https://remote.invalid/p.png">'))
        with self.assertRaises(GuardError) as error:
            prepare_draw_page_snapshots(source, self.root / 'snapshots', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'NATIVE_DRAW_RESOURCE_UNSUPPORTED')
        self.assertFalse((self.root / 'snapshots').exists())

    def test_xml_entities_and_wrong_model_fail_before_copying(self):
        source = make_flat_drawing(self.root / 'source.fodg')
        source.write_text('<!DOCTYPE x [<!ENTITY x "unsafe">]>' + source.read_text())
        with self.assertRaises(GuardError):
            prepare_draw_page_snapshots(source, self.root / 'snapshots', cancel=self.cancel)
        self.assertFalse((self.root / 'snapshots').exists())

    def test_cancelled_snapshot_does_not_create_output(self):
        source = make_flat_drawing(self.root / 'source.fodg')
        self.cancel.set()
        with self.assertRaises(GuardError) as error:
            prepare_draw_page_snapshots(source, self.root / 'snapshots', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertFalse((self.root / 'snapshots').exists())

    def test_route_requires_both_real_installed_filters_and_model_proof(self):
        native = SimpleNamespace(valid=True, fingerprint='test', filters={
            'OpenDocument Drawing Flat XML': (DRAW_SERVICE, {'fodg'}, {'IMPORT', 'EXPORT'}),
            'draw_svg_Export': (DRAW_SERVICE, {'svg'}, {'EXPORT'})})
        proof = {'state': 'detected', 'registryFingerprint': 'test', 'documentServices': [DRAW_SERVICE]}
        self.assertEqual(native_draw_html_plan(native, proof, 'html')['svgFilter'], 'draw_svg_Export')
        self.assertIsNone(native_draw_html_plan(native, {**proof, 'registryFingerprint': 'other'}, 'html'))
        native.filters.pop('draw_svg_Export')
        self.assertIsNone(native_draw_html_plan(native, proof, 'html'))

    def test_vector_wrapper_keeps_each_page_as_separate_svg_sidecar(self):
        paths = []
        for index, marker in enumerate(MARKERS, 1):
            path = self.root / ('native_%d.svg' % index)
            path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="148mm" '
                'viewBox="0 0 21000 14800"><text x="1000" y="2000">' + marker + '</text></svg>')
            paths.append(path)
        results, check = wrap_svg_pages(paths, 'xhtml', self.root / 'output', stem='book', cancel=self.cancel,
            expected_pages=[{'contentExpected': True}, {'contentExpected': True}])
        self.assertEqual(check['pageCount'], 2)
        self.assertFalse(check['rasterizedByAio'])
        self.assertEqual(len(results), 3)
        tree = ET.fromstring(results[0].read_bytes())
        objects = tree.findall('.//{http://www.w3.org/1999/xhtml}object')
        self.assertEqual(len(objects), 2)
        self.assertEqual(results[1].read_bytes(), paths[0].read_bytes())
        self.assertEqual(results[2].read_bytes(), paths[1].read_bytes())

    def test_presentation_requires_observed_native_per_page_svg_capability(self):
        native = SimpleNamespace(valid=True, fingerprint='test', uncertain_extensions=set(), filters={
            'OpenDocument Presentation Flat XML': (PRESENTATION_SERVICE, {'fodp'}, {'IMPORT', 'EXPORT'}),
            'impress_svg_Export': (PRESENTATION_SERVICE, {'svg'}, {'EXPORT'})})
        proof = {'state': 'detected', 'registryFingerprint': 'test',
                 'documentServices': [PRESENTATION_SERVICE]}
        self.assertIsNone(native_draw_html_plan(native, proof, 'html'))
        proof.update(basis='native_preflight_uno_document', nativeGraphicExports=['svg'])
        self.assertIsNotNone(native_draw_html_plan(native, proof, 'html'))
        native.uncertain_extensions.add('svg')
        self.assertIsNone(native_draw_html_plan(native, proof, 'html'))

    def test_uno_worker_exports_original_page_objects_and_host_checks_all_pages(self):
        from app_backend.converter_guard.uno_session import WORKER_SOURCE
        namespace = {'__name__': 'aio_worker_unit_test'}
        exec(WORKER_SOURCE, namespace)
        visited = []
        class Page:
            def __init__(self, index): self.index = index
            def getCount(self): return 2
        class Exporter:
            def supportsMimeType(self, mime): return mime == 'image/svg+xml'
            def setSourceDocument(self, page): self.page = page; visited.append(page.index)
            def filter(self, properties):
                values = {p.Name: p.Value for p in properties}
                self_outer.assertEqual(values['MediaType'], 'image/svg+xml')
                path = Path(unquote(urlsplit(values['URL']).path))
                path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" '
                    'viewBox="0 0 21000 29700"><text x="10" y="20">' + MARKERS[self.page.index] +
                    '</text><image href="data:image/png;base64,' + base64.b64encode(PNG).decode() + '"/></svg>')
                return True
        self_outer = self
        exporter = Exporter()
        remote = SimpleNamespace(ServiceManager=SimpleNamespace(createInstanceWithContext=lambda *args: exporter))
        model = SimpleNamespace(getDrawPages=lambda: SimpleNamespace(
            getCount=lambda: 2, getByIndex=lambda index: Page(index)))
        def request(value, **kwargs):
            output = Path(value['output'])
            target = output / 'source.svg'
            paths, manifest = namespace['export_draw_svg_pages'](model, remote,
                lambda name, val: SimpleNamespace(Name=name, Value=val), target)
            self.assertEqual(value['requiredDrawPages'], 2)
            return {'ok': True, 'svgPageManifest': manifest.name, 'drawPagesCount': len(paths), 'timings': {}}
        office = SimpleNamespace(disabled=False, request=request)
        native = SimpleNamespace(valid=True, fingerprint='test', uncertain_extensions=set(),
            import_services={PRESENTATION_SERVICE}, filters={
            'OpenDocument Presentation Flat XML': (PRESENTATION_SERVICE, {'fodp'}, {'IMPORT', 'EXPORT'}),
            'impress_svg_Export': (PRESENTATION_SERVICE, {'svg'}, {'EXPORT'})})
        proof = {'state': 'detected', 'registryFingerprint': 'test', 'basis': 'native_preflight_uno_document',
                 'documentServices': [PRESENTATION_SERVICE], 'drawPages': 2, 'nativeGraphicExports': ['svg']}
        source = make_flat_drawing(self.root / 'source.fodp', presentation=True)
        paths, check, metrics = convert_native_draw_html(source, 'xhtml', self.root / 'work',
            native=native, proof=proof, executable=self.root / 'unused', prepare_profile=lambda _: None,
            cancel=self.cancel, on_started=lambda: None, office=office)
        self.assertEqual(visited, [0, 1])
        self.assertEqual(check['pageCount'], 2)
        self.assertTrue(check['nativePageApi'])
        self.assertEqual(metrics['source'], 'uno_document')
        for index, path in enumerate(paths[1:]):
            self.assertIn(MARKERS[index], path.read_text())
            self.assertEqual(check['pages'][index]['embeddedImages'][0]['bytes'], len(PNG))


if __name__ == '__main__': unittest.main()
