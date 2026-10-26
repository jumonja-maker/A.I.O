"""Presentation HTML loss regressions: no network, OCR or mock render proof."""
import base64
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.postconditions import SourceEvidence, check_native_output
from app_backend.converter_guard.presentation_evidence import check_presentation_html

P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
DRAW = 'urn:oasis:names:tc:opendocument:xmlns:drawing:1.0'
TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
XLINK = 'http://www.w3.org/1999/xlink'


class PresentationHtmlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()
        self.asset = b'known-original-image-content'
        self.source = self.root / 'slides.pptx'
        self.make_source(self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def make_source(self, path, paragraphs=('Caption Alpha', 'Caption Alpha'), image=True):
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('ppt/presentation.xml',
                f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
                '<p:sldId id="1" r:id="s1"/></p:sldIdLst></p:presentation>')
            archive.writestr('ppt/_rels/presentation.xml.rels',
                f'<Relationships xmlns="{REL}"><Relationship Id="s1" Type="{R}/slide" '
                'Target="slides/slide1.xml"/></Relationships>')
            archive.writestr('ppt/slides/slide1.xml',
                f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:cSld><p:spTree>' +
                ''.join('<a:p><a:r><a:t>' + value + '</a:t></a:r></a:p>' for value in paragraphs) +
                ('<p:pic><a:blip r:embed="im1"/></p:pic>' if image else '') +
                '</p:spTree></p:cSld></p:sld>')
            archive.writestr('ppt/slides/_rels/slide1.xml.rels',
                f'<Relationships xmlns="{REL}"><Relationship Id="im1" Type="{R}/image" '
                'Target="../media/image1.png"/></Relationships>')
            archive.writestr('ppt/media/image1.png', self.asset)
            # This detached slide must not require content in the result.
            archive.writestr('ppt/slides/slide99.xml', f'<p:sld xmlns:p="{P}" xmlns:a="{A}">'
                '<a:p><a:r><a:t>DETACHED</a:t></a:r></a:p></p:sld>')

    def write_html(self, body, suffix='html', extra=''):
        path = self.root / ('slides.' + suffix)
        path.write_text('<html><head>' + extra + '</head><body>' + body + '</body></html>', encoding='utf-8')
        return path

    def inline(self, data=None):
        return '<img src="data:image/png;base64,' + base64.b64encode(
            self.asset if data is None else data).decode() + '">'

    def check(self, path, evidence=None, paths=None):
        return check_native_output(paths or [path], path.suffix[1:], self.source,
            cancel=self.cancel, source_evidence=evidence)

    def test_missing_caption_rejected_for_all_html_aliases(self):
        for suffix in ('html', 'htm', 'xhtml'):
            with self.subTest(suffix=suffix):
                with self.assertRaises(GuardError) as caught:
                    self.check(self.write_html(self.inline(), suffix))
                self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')
                self.assertIn('missing_paragraphs:2', caught.exception.diagnostic)

    def test_text_only_output_does_not_silently_discard_images(self):
        with self.assertRaises(GuardError) as caught:
            self.check(self.write_html('<p>Caption Alpha</p>' * 2))
        self.assertEqual(caught.exception.diagnostic, 'presentation_html_missing_graphics')

    def test_good_text_and_original_images_are_only_a_checked_subset(self):
        result = self.check(self.write_html('<p>Caption Alpha</p>' * 2 + self.inline()))
        check = next(c for c in result['additionalChecks'] if c['check'] == 'presentation_html_content')
        self.assertEqual(check['status'], 'checked_subset')
        self.assertFalse(check['qualityCompared'])
        self.assertFalse(check['visualLayoutCompared'])

    def test_duplicate_source_captions_require_duplicate_output_text(self):
        with self.assertRaises(GuardError) as caught:
            self.check(self.write_html('<p>Caption Alpha</p>' + self.inline()))
        self.assertIn('missing_paragraphs:1', caught.exception.diagnostic)

    def test_full_slide_raster_is_unknown_not_a_claim_of_missing_text(self):
        result = self.check(self.write_html(self.inline(b'full-rendered-slide')))
        check = result['additionalChecks'][0]
        self.assertEqual(check['status'], 'not_checked')
        self.assertEqual(check['reason'], 'alternative_graphic_representation')

    def test_svg_and_css_graphic_cannot_be_mistaken_for_absent_images(self):
        for body, head in (('<svg><text>Caption Alpha</text></svg>', ''),
                           ('<p>Caption Alpha</p>', '<style>body {background: url(data:x)}</style>'),
                           ('<p>Caption Alpha</p>', '<link rel="stylesheet" href="https://invalid.test/a.css">')):
            with self.subTest(body=body, head=head):
                path = self.write_html(body, extra=head)
                result = check_presentation_html([path], 'html', self.source, cancel=self.cancel)
                self.assertEqual(result['status'], 'not_checked')

    def test_script_generated_content_is_unknown_and_not_executed(self):
        path = self.write_html(self.inline() + '<script>createCaptions()</script>')
        result = check_presentation_html([path], 'html', self.source, cancel=self.cancel)
        self.assertEqual(result['status'], 'not_checked')

    def test_text_from_head_and_alt_is_not_slide_body_text(self):
        path = self.write_html(self.inline(), extra='<title>Caption Alpha</title>')
        with self.assertRaises(GuardError):
            self.check(path)

    def test_bundle_sidecar_pages_are_included_in_text_evidence(self):
        main = self.write_html(self.inline())
        child = self.root / 'slide1.html'
        child.write_text('<html><body><p>Caption Alpha</p><p>Caption Alpha</p></body></html>')
        result = self.check(main, paths=[main, child])
        self.assertEqual(result['additionalChecks'][0]['status'], 'checked_subset')

    def test_local_original_asset_is_hashed_without_leaving_bundle(self):
        image = self.root / 'image.png'
        image.write_bytes(self.asset)
        path = self.write_html('<p>Caption Alpha</p>' * 2 + '<img src="image.png">')
        result = self.check(path, paths=[path, image])
        self.assertEqual(result['additionalChecks'][0]['status'], 'checked_subset')

    def test_content_identifies_renamed_presentation_and_active_slides_only(self):
        self.source = self.source.rename(self.root / 'slides.data')
        self.assertEqual(self.check(self.write_html('<p>Caption Alpha</p>' * 2 + self.inline()))
                         ['additionalChecks'][0]['sourceTextParagraphs'], 2)

    def test_source_cache_reused_but_every_output_independently_checked(self):
        from app_backend.converter_guard.presentation_evidence import _source_presentation
        evidence = SourceEvidence(self.source)
        with patch('app_backend.converter_guard.presentation_evidence._source_presentation',
                   wraps=_source_presentation) as inspect:
            self.check(self.write_html('<p>Caption Alpha</p>' * 2 + self.inline()), evidence)
            with self.assertRaises(GuardError):
                self.check(self.write_html(self.inline()), evidence)
            self.assertEqual(inspect.call_count, 1)
            self.make_source(self.source, paragraphs=('New Caption',))
            with self.assertRaises(GuardError):
                self.check(self.write_html(self.inline()), evidence)
            self.assertEqual(inspect.call_count, 2)

    def test_flat_odf_embedded_asset_proves_caption_loss(self):
        self.source = self.root / 'slides.fodp'
        self.source.write_text(f'<office:document xmlns:office="{OFFICE}" xmlns:draw="{DRAW}" '
            f'xmlns:text="{TEXT}"><office:body><office:presentation><draw:page>'
            '<text:p>Caption Alpha</text:p><draw:image><office:binary-data>' +
            base64.b64encode(self.asset).decode() + '</office:binary-data></draw:image>'
            '</draw:page></office:presentation></office:body></office:document>')
        with self.assertRaises(GuardError) as caught:
            self.check(self.write_html(self.inline()))
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_odp_package_checks_the_actual_body(self):
        self.source = self.root / 'slides.odp'
        with zipfile.ZipFile(self.source, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.presentation')
            archive.writestr('content.xml', f'<office:document-content xmlns:office="{OFFICE}" '
                f'xmlns:draw="{DRAW}" xmlns:text="{TEXT}" xmlns:xlink="{XLINK}">'
                '<office:body><office:presentation><draw:page><text:p>Caption Alpha</text:p>'
                '<draw:image xlink:href="Pictures/a.png"/></draw:page></office:presentation>'
                '</office:body></office:document-content>')
            archive.writestr('Pictures/a.png', self.asset)
        with self.assertRaises(GuardError) as caught:
            self.check(self.write_html(self.inline()))
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_CONTENT_LOSS')

    def test_odf_explicit_spaces_and_tabs_follow_text_semantics(self):
        self.source = self.root / 'slides.fodp'
        self.source.write_text(f'<office:document xmlns:office="{OFFICE}" xmlns:draw="{DRAW}" '
            f'xmlns:text="{TEXT}"><office:body><office:presentation><draw:page>'
            '<text:p>Alpha<text:s text:c="5"/>Beta<text:tab/>Gamma</text:p>'
            '</draw:page></office:presentation></office:body></office:document>')
        result = self.check(self.write_html('<p>Alpha Beta Gamma</p>'))
        self.assertEqual(result['additionalChecks'][0]['status'], 'checked_subset')

    def test_cancellation_is_not_downgraded_to_not_checked(self):
        self.cancel.set()
        with self.assertRaises(GuardError) as caught:
            self.check(self.write_html(self.inline()))
        self.assertEqual(caught.exception.code, 'CANCELLED')

    def test_source_budget_is_explicit_unknown_not_a_quality_certificate(self):
        with patch('app_backend.converter_guard.presentation_evidence.MAX_XML_BYTES', 32):
            result = check_presentation_html([self.write_html(self.inline())], 'html',
                self.source, cancel=self.cancel)
        self.assertEqual(result['status'], 'not_checked')
        self.assertFalse(result['qualityCompared'])

    def test_large_uninspectable_inline_graphic_is_explicit_unknown(self):
        with patch('app_backend.converter_guard.presentation_evidence.MAX_INLINE_ATTRIBUTE', 32):
            result = check_presentation_html([self.write_html(self.inline(b'x' * 1000))], 'html',
                self.source, cancel=self.cancel)
        self.assertEqual(result['status'], 'not_checked')

    def test_text_only_presentation_preserves_caption_without_images(self):
        self.make_source(self.source, paragraphs=('A unique caption',), image=False)
        result = self.check(self.write_html('<p>A unique caption</p>'))
        self.assertEqual(result['additionalChecks'][0]['status'], 'checked_subset')

    def test_unrelated_document_has_no_presentation_claim(self):
        self.source.write_bytes(b'unrelated')
        result = self.check(self.write_html('<p>Hello</p>'))
        self.assertNotIn('additionalChecks', result)


if __name__ == '__main__':
    unittest.main()
