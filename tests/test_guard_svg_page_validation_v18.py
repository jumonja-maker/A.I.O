"""Native vector-page structure and self-contained media checks."""
import base64
from pathlib import Path
import tempfile
import threading
import unittest

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.svg_page_validation import inspect_svg_page, sanitize_native_svg_doctype

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
SVG = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="210mm" height="297mm" viewBox="0 0 21000 29700">{}</svg>'


class NativeSVGPage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def inspect(self, text, **kwargs):
        path = self.root / 'page.svg'
        path.write_text(text, encoding='utf-8')
        return inspect_svg_page(path, cancel=kwargs.pop('cancel', threading.Event()), **kwargs)

    def rejected(self, text, code='NATIVE_SVG_OUTPUT_INVALID'):
        with self.assertRaises(GuardError) as caught:
            self.inspect(text)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_vector_text_geometry_and_local_clip_are_checked(self):
        text = SVG.format('<defs><clipPath id="c"><rect width="10" height="10"/></clipPath></defs>'
                          '<g clip-path="url(#c)"><path d="M0 0 L100 100"/>'
                          '<text><tspan>Контрольная страница</tspan></text></g>')
        result = self.inspect(text)
        self.assertEqual(result['width'], 210)
        self.assertEqual(result['widthUnit'], 'mm')
        self.assertEqual(result['widthCss'], '210mm')
        self.assertEqual(result['heightCss'], '297mm')
        self.assertEqual(result['viewBox'], [0, 0, 21000, 29700])
        self.assertEqual(result['vectorElements'], 1)
        self.assertGreater(result['textCharacters'], 0)
        self.assertFalse(result['qualityCompared'])

    def test_embedded_image_bytes_are_hashed_without_external_files(self):
        import hashlib
        text = SVG.format('<image width="40" height="30" xlink:href="data:image/png;base64,' +
                          base64.b64encode(PNG).decode() + '"/>')
        result = self.inspect(text)
        self.assertEqual(result['imageElements'], 1)
        self.assertEqual(result['embeddedImages'][0]['sha256'], hashlib.sha256(PNG).hexdigest())

    def test_defs_alone_are_not_page_content_but_referenced_shapes_are(self):
        definitions = '<defs><g id="glyph"><path d="M0 0 L10 10"/></g></defs>'
        self.rejected(SVG.format(definitions), 'ENGINE_OUTPUT_CONTENT_EMPTY')
        result = self.inspect(SVG.format(definitions + '<use xlink:href="#glyph"/>'))
        self.assertTrue(result['resolvedUsesPresent'])
        self.rejected(SVG.format('<defs id="d"><path d="M0 0 L10 10"/></defs><use xlink:href="#d"/>'),
                      'ENGINE_OUTPUT_CONTENT_EMPTY')

    def test_use_cycles_and_empty_references_do_not_prove_content(self):
        self.rejected(SVG.format('<defs><g id="a"><use href="#b"/></g>'
            '<g id="b"><use href="#a"/></g></defs><use href="#a"/>'), 'ENGINE_OUTPUT_CONTENT_EMPTY')
        self.rejected(SVG.format('<g id="root"><defs><path id="shape" d="M0 0 L10 10"/>'
            '</defs><use href="#root"/></g>'), 'ENGINE_OUTPUT_CONTENT_EMPTY')

    def test_blank_pages_require_explicit_source_expectation(self):
        self.rejected(SVG.format(''), 'ENGINE_OUTPUT_CONTENT_EMPTY')
        self.assertEqual(self.inspect(SVG.format(''), content_expected=False)['status'], 'blank_page')

    def test_hidden_content_and_zero_geometry_are_not_accepted(self):
        self.rejected(SVG.format('<g style="display:none"><path d="M0 0 L10 10"/></g>'),
                      'ENGINE_OUTPUT_CONTENT_EMPTY')
        self.rejected(SVG.format('<rect width="0" height="10"/>'), 'ENGINE_OUTPUT_CONTENT_EMPTY')
        self.rejected(SVG.format('<path d="M0 0 L10 10"/>').replace('width="210mm"', 'width="0"'))

    def test_missing_ids_and_duplicate_ids_fail(self):
        self.rejected(SVG.format('<path clip-path="url(#missing)" d="M0 0 L10 10"/>'))
        self.rejected(SVG.format('<path id="same" d="M0 0 L10 10"/><g id="same"/>'))

    def test_remote_and_local_visual_resources_are_rejected_without_fetch(self):
        for value in ('https://remote.invalid/a.png', '../a.png', 'file:///private/a.png'):
            with self.subTest(value=value):
                self.rejected(SVG.format('<image width="10" height="10" href="' + value + '"/>'),
                              'NATIVE_SVG_RESOURCE_UNSUPPORTED')
        self.rejected(SVG.format('<rect width="10" height="10" style="fill:url(https://remote.invalid/a.svg)"/>'),
                      'NATIVE_SVG_RESOURCE_UNSUPPORTED')
        self.rejected(SVG.format('<g xml:base="https://remote.invalid/"><path d="M0 0 L10 10"/></g>'),
                      'NATIVE_SVG_RESOURCE_UNSUPPORTED')

    def test_inert_web_and_mail_hyperlinks_are_preserved_not_fetched(self):
        for value in ('https://example.invalid/', 'http://example.invalid/', 'mailto:person@example.invalid'):
            result = self.inspect(SVG.format('<a href="' + value + '"><text>Link</text></a>'))
            self.assertEqual(result['externalHyperlinks'], 1)
        self.rejected(SVG.format('<a href="javascript:alert(1)"><text>Link</text></a>'),
                      'NATIVE_SVG_RESOURCE_UNSUPPORTED')

    def test_images_require_valid_base64_and_matching_signatures(self):
        for value in ('data:image/png;base64,@@@@', 'data:image/png;base64,YWJjZA==',
                      'data:image/svg+xml;base64,PHN2Zy8+'):
            self.rejected(SVG.format('<image width="10" height="10" href="' + value + '"/>'),
                          'NATIVE_SVG_RESOURCE_UNSUPPORTED')

    def test_active_xml_entities_and_instructions_fail(self):
        for body in ('<script>alert(1)</script>', '<foreignObject/>',
                     '<rect width="10" height="10" onclick="alert(1)"/>'):
            self.rejected(SVG.format(body), 'NATIVE_SVG_RESOURCE_UNSUPPORTED')
        self.rejected('<!DOCTYPE svg [<!ENTITY x "unsafe">]>' + SVG.format('<text>&x;</text>'))
        self.rejected('<!DOCTYPE svg SYSTEM "https://remote.invalid/svg.dtd">' + SVG.format('<text>safe</text>'))
        self.rejected('<?xml-stylesheet href="https://remote.invalid/style.css"?>' + SVG.format('<text>safe</text>'))

    def test_truncation_wrong_namespace_depth_and_cancellation_fail(self):
        self.rejected(SVG.format('<text>safe</text>')[:-5])
        self.rejected('<svg width="10" height="10"><text>safe</text></svg>')
        self.rejected(SVG.format('<g>' * 260 + '<text>safe</text>' + '</g>' * 260))
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(GuardError) as caught:
            self.inspect(SVG.format('<text>safe</text>'), cancel=cancel)
        self.assertEqual(caught.exception.code, 'CANCELLED')

    def test_native_doctype_is_removed_only_in_a_private_complete_copy(self):
        import hashlib
        declaration = '<!DOCTYPE  svg PUBLIC\n "-//W3C//DTD SVG 1.1//EN"\n "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd" >'
        original = ('<?xml version="1.0" encoding="UTF-8"?>\n' + declaration + '\n' +
                    SVG.format('<text>Native page</text>')).encode()
        source, destination = self.root / 'native.svg', self.root / 'packaged.svg'
        source.write_bytes(original)
        result = sanitize_native_svg_doctype(source, destination, cancel=threading.Event())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(destination.read_bytes(), original.replace(declaration.encode(), b''))
        self.assertTrue(result['removedNativeDoctype'])
        self.assertEqual(result['originalSha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(result['packagedSha256'], hashlib.sha256(destination.read_bytes()).hexdigest())
        self.assertGreater(inspect_svg_page(destination, cancel=threading.Event())['textCharacters'], 0)

    def test_sanitizer_rejects_other_declarations_and_never_overwrites(self):
        source, destination = self.root / 'native.svg', self.root / 'packaged.svg'
        source.write_text('<!DOCTYPE svg SYSTEM "https://remote.invalid/svg.dtd">' + SVG.format('<text>safe</text>'))
        with self.assertRaises(GuardError):
            sanitize_native_svg_doctype(source, destination, cancel=threading.Event())
        self.assertFalse(destination.exists())
        source.write_text(SVG.format('<text>safe</text>'))
        destination.write_bytes(b'original destination bytes')
        with self.assertRaises(GuardError):
            sanitize_native_svg_doctype(source, destination, cancel=threading.Event())
        self.assertEqual(destination.read_bytes(), b'original destination bytes')


if __name__ == '__main__':
    unittest.main()
