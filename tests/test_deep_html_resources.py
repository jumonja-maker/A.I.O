"""Static HTML dependency gaps and invalid exported raster companions."""
import html
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from app_backend.converter_guard.core import _html_resource_warning
from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.postconditions import check_native_output
from test_technical_integrity_20261108 import png


class DeepHtmlResources(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.source = self.root / 'document.docx'
        with zipfile.ZipFile(self.source, 'w') as package:
            package.writestr('[Content_Types].xml',
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            package.writestr('_rels/.rels',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            package.writestr('word/document.xml',
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>Document text</w:t></w:r></w:p></w:body></w:document>')

    def output(self, body):
        path = self.root / 'document.html'
        path.write_text('<!doctype html><html><body>' + body + '</body></html>')
        return path

    def check(self, output):
        return check_native_output([output], 'html', self.source, cancel=self.cancel)

    def test_css_image_set_cannot_claim_dependencies_checked(self):
        for spelling in ('image-set', '-webkit-image-set', r'image\2d set'):
            for external in (False, True):
                with self.subTest(function=spelling, external=external):
                    css = '.photo {background-image:' + spelling + '("missing.png" 1x)}'
                    if external:
                        (self.root / 'style.css').write_text(css)
                    output = self.output(('<link rel="stylesheet" href="style.css">' if external
                        else '<style>' + css + '</style>') + '<div class="photo">Document text</div>')
                    with self.assertRaises(GuardError) as error:
                        self.check(output)
                    self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_css_literal_image_set_text_is_not_a_function(self):
        output = self.output('<style>.label::before{content:\'image-set("example")\'}</style>'
                             '<div class="label">Document text</div>')
        self.assertTrue(self.check(output)['localResourcesChecked'])

    def test_srcdoc_local_resources_are_checked_and_valid_companions_still_work(self):
        for markup in ('<img src="photo.png">', '<style>.p{background:url(photo.png)}</style><p>text</p>'):
            with self.subTest(markup=markup):
                (self.root / 'photo.png').unlink(missing_ok=True)
                output = self.output('<iframe srcdoc="' + html.escape(markup, quote=True) + '"></iframe>'
                                     '<p>Document text</p>')
                self.assertEqual(_html_resource_warning(output), 'HTML_LOCAL_RESOURCES_NOT_UPLOADED')
                with self.assertRaises(GuardError) as error:
                    self.check(output)
                self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_RESOURCE_MISSING')
                (self.root / 'photo.png').write_bytes(png())
                self.assertTrue(self.check(output)['localResourcesChecked'])

    def test_nested_srcdoc_budget_cannot_become_success(self):
        markup = '<img src="photo.png">'
        for _ in range(7):
            markup = '<iframe srcdoc="' + html.escape(markup, quote=True) + '"></iframe>'
        output = self.output(markup + '<p>Document text</p>')
        with self.assertRaises(GuardError) as error:
            self.check(output)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_truncated_srcdoc_suffix_stays_unverified(self):
        markup = '<p>' + 'x' * 6000 + '</p><img src="missing.png">'
        output = self.output('<iframe srcdoc="' + html.escape(markup, quote=True) + '"></iframe>'
                             '<p>Document text</p>')
        with self.assertRaises(GuardError) as error:
            self.check(output)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_iframe_only_is_not_certified_as_an_empty_document(self):
        output = self.output('<iframe srcdoc="&lt;p&gt;Document text&lt;/p&gt;"></iframe>')
        with self.assertRaises(GuardError) as error:
            self.check(output)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_empty_truncated_and_wrong_image_resources_are_not_ready(self):
        for payload in (b'', png()[:-7], b'not an image'):
            with self.subTest(payload=payload[-10:]):
                image = self.root / 'photo.png'
                image.write_bytes(payload)
                output = self.output('<img src="photo.png"><p>Document text</p>')
                # Resource existence alone cannot distinguish these exports.
                self.assertIsNone(_html_resource_warning(output))
                with self.assertRaises(GuardError) as error:
                    self.check(output)
                self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_RESOURCE_INVALID')

    def test_nested_css_image_resource_validation_and_unique_counts(self):
        (self.root / 'style.css').write_text('body{background:url(photo.png)}')
        image = self.root / 'photo.png'
        output = self.output('<link rel="stylesheet" href="style.css"><img src="photo.png">'
                             '<p>Document text</p>')
        image.write_bytes(png()[:-7])
        with self.assertRaises(GuardError):
            self.check(output)
        image.write_bytes(png())
        result = self.check(output)
        self.assertTrue(result['localResourcesChecked'])
        self.assertEqual(result['validatedRasterResources'], 1)

    def test_renamed_raster_is_checked_by_magic_without_extension_veto(self):
        image = self.root / 'photo.blob'
        output = self.output('<img src="photo.blob"><p>Document text</p>')
        image.write_bytes(png()[:-7])
        with self.assertRaises(GuardError) as error:
            self.check(output)
        self.assertEqual(error.exception.code, 'ENGINE_OUTPUT_RESOURCE_INVALID')
        image.write_bytes(png())
        self.assertEqual(self.check(output)['validatedRasterResources'], 1)

    def test_ordinary_css_svg_font_and_text_resources_stay_supported(self):
        (self.root / 'photo.png').write_bytes(png())
        (self.root / 'font.woff').write_bytes(b'font bytes are not an image container')
        (self.root / 'style.css').write_text('@font-face{font-family:p;src:url(font.woff)}'
                                            'body{background:url(photo.png)}')
        (self.root / 'figure.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg">'
                                             '<image href="photo.png"/></svg>')
        output = self.output('<link rel="stylesheet" href="style.css"><img src="figure.svg">'
                             '<p>Document text</p>')
        result = self.check(output)
        self.assertTrue(result['localResourcesChecked'])
        self.assertEqual(result['validatedRasterResources'], 1)

    def test_more_images_than_the_deduplication_cache_are_still_validated(self):
        names = ['picture-%d.png' % index for index in range(150)]
        for name in names:
            (self.root / name).write_bytes(png())
        output = self.output(''.join('<img src="' + name + '">' for name in names) +
                             '<p>Document text</p>')
        result = self.check(output)
        self.assertTrue(result['localResourcesChecked'])
        self.assertEqual(result['validatedRasterResources'], 150)

    def test_raster_resource_failure_retains_source_and_output_bytes(self):
        (self.root / 'photo.png').write_bytes(png()[:-7])
        output = self.output('<img src="photo.png"><p>Document text</p>')
        before = {path: path.read_bytes() for path in (self.source, output, self.root/'photo.png')}
        with self.assertRaises(GuardError):
            self.check(output)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_cancellation_during_resource_validation_keeps_its_cause(self):
        from unittest.mock import patch
        (self.root / 'photo.png').write_bytes(png())
        output = self.output('<img src="photo.png"><p>Document text</p>')
        with patch('app_backend.converter_guard.media_fidelity.inspect_media',
                   side_effect=GuardError('CANCELLED', 'cancelled in resource reader', stage='result')):
            with self.assertRaises(GuardError) as error:
                self.check(output)
        self.assertEqual(error.exception.code, 'CANCELLED')


if __name__ == '__main__':
    unittest.main()
