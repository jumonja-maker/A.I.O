"""EPUB spine/resources and hostile-package regressions, without another engine."""
import base64
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from app_backend.converter_guard.epub_import import prepare_epub, _anchor
from app_backend.converter_guard.errors import GuardError

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
MARKERS = ('AIO_EPUB_SPINE_FIRST_001', 'AIO_EPUB_SPINE_SECOND_002')


def create_epub(path, *, replacement=None, extra=None):
    """A real two-chapter OCF book whose spine reverses lexical entry order."""
    files = {
        'mimetype': b'application/epub+zip',
        'META-INF/container.xml': '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        'EPUB/book.opf': '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:aio:epub:17</dc:identifier><dc:title>Spine and image probe</dc:title><dc:language>ru</dc:language></metadata><manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/><item id="z" href="z.xhtml" media-type="application/xhtml+xml"/><item id="css" href="styles/main.css" media-type="text/css"/><item id="image" href="images/pixel.png" media-type="image/png"/></manifest><spine><itemref idref="z"/><itemref idref="a"/></spine></package>',
        'EPUB/z.xhtml': '<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head><title>First</title><link rel="stylesheet" href="styles/main.css"/></head><body><h1 id="part">' + MARKERS[0] + '</h1><p>Кириллица сохранена.</p><img src="images/pixel.png" alt="pixel" width="40" height="40"/><p><a href="a.xhtml#part">Next chapter</a></p></body></html>',
        'EPUB/a.xhtml': '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Second</title></head><body><h1 id="part">' + MARKERS[1] + '</h1><p><a href="z.xhtml#part">Previous chapter</a></p></body></html>',
        'EPUB/styles/main.css': 'h1 {color: #184088;} p {font-size: 12pt;}',
        'EPUB/images/pixel.png': PNG,
    }
    files.update(replacement or {})
    files.update(extra or {})
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return Path(path)


class EPUBPreparationV17(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cancel = threading.Event()

    def prepare(self, replacement=None, extra=None):
        source = create_epub(self.root / 'book.epub', replacement=replacement, extra=extra)
        return source, prepare_epub(source, self.root / 'private', cancel=self.cancel)

    def rejected(self, replacement=None, extra=None, code=None):
        source = create_epub(self.root / 'book.epub', replacement=replacement, extra=extra)
        original = source.read_bytes()
        with self.assertRaises(GuardError) as error:
            prepare_epub(source, self.root / 'private', cancel=self.cancel)
        if code: self.assertEqual(error.exception.code, code)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.root / 'private').exists())
        return error.exception

    def test_spine_order_and_chapter_anchor_links_survive_combination(self):
        source, prepared = self.prepare()
        text = prepared.source.read_text()
        self.assertLess(text.index(MARKERS[0]), text.index(MARKERS[1]))
        self.assertIn('id="' + _anchor('EPUB/z.xhtml', 'part') + '"', text)
        self.assertIn('href="#' + _anchor('EPUB/a.xhtml', 'part') + '"', text)
        self.assertIn('href="#' + _anchor('EPUB/z.xhtml', 'part') + '"', text)
        self.assertEqual(prepared.summary['readingOrder'], 'opf_spine')
        self.assertEqual(prepared.summary['spineItems'], 2)

    def test_images_are_embedded_before_native_import_and_css_is_package_local(self):
        source, prepared = self.prepare()
        text = prepared.source.read_text()
        self.assertIn('src="data:image/png;base64,' + base64.b64encode(PNG).decode() + '"', text)
        self.assertIn('href="_epub-assets/EPUB/styles/main.css"', text)
        self.assertEqual((prepared.source.parent / '_epub-assets/EPUB/images/pixel.png').read_bytes(), PNG)
        self.assertEqual(prepared.summary['resources'], 2)

    def test_reading_order_depends_on_spine_not_zip_entry_or_manifest_sort(self):
        original = create_epub(self.root / 'original.epub')
        with zipfile.ZipFile(original) as archive:
            values = {n: archive.read(n) for n in reversed(archive.namelist())}
        with zipfile.ZipFile(self.root / 'reverse.epub', 'w') as archive:
            for name, value in values.items(): archive.writestr(name, value)
        result = prepare_epub(self.root / 'reverse.epub', self.root / 'prepared', cancel=self.cancel)
        content = result.source.read_text()
        self.assertLess(content.index(MARKERS[0]), content.index(MARKERS[1]))

    def test_script_and_event_handlers_are_not_silently_dropped(self):
        self.rejected({'EPUB/z.xhtml': '<html><head/><body><script>alert(1)</script></body></html>'},
                      code='EPUB_ACTIVE_CONTENT_UNSUPPORTED')

    def test_remote_resources_are_rejected_but_web_hyperlinks_remain_hyperlinks(self):
        self.rejected({'EPUB/z.xhtml': '<html><head/><body><img src="https://remote.invalid/pixel.png"/></body></html>'},
                      code='EPUB_EXTERNAL_RESOURCE_UNSUPPORTED')
        self.root.joinpath('private').mkdir()
        self.root.joinpath('private').rmdir()
        source = create_epub(self.root / 'safe.epub', replacement={'EPUB/z.xhtml':
            '<html><head/><body id="part"><a href="https://example.org/">Link</a></body></html>'})
        prepared = prepare_epub(source, self.root / 'safe', cancel=self.cancel)
        self.assertIn('href="https://example.org/"', prepared.source.read_text())

    def test_css_remote_imports_and_obfuscated_urls_are_rejected(self):
        for style in ('@import "https://remote.invalid/a.css";',
                      'p {background: url(https://remote.invalid/a.png)}',
                      'p {background: u\\72l(https://remote.invalid/a.png)}',
                      '@import/**/"https://remote.invalid/a.css";'):
            with self.subTest(style=style):
                self.rejected({'EPUB/styles/main.css': style})

    def test_css_images_are_embedded_and_transitive_imports_are_included(self):
        source = create_epub(self.root / 'seed.epub')
        with zipfile.ZipFile(source) as z: package = z.read('EPUB/book.opf').decode()
        package = package.replace('</manifest>', '<item id="extra" href="styles/extra.css" media-type="text/css"/></manifest>')
        source, prepared = self.prepare({'EPUB/book.opf': package,
            'EPUB/styles/main.css': '@import "extra.css"; p {background: url(../images/pixel.png)}'},
            {'EPUB/styles/extra.css': 'p {color: blue;}'})
        css = (prepared.source.parent / '_epub-assets/EPUB/styles/main.css').read_text()
        self.assertIn('data:image/png;base64,' + base64.b64encode(PNG).decode(), css)
        self.assertIn('@import "extra.css"', css)
        self.assertEqual((prepared.source.parent / '_epub-assets/EPUB/styles/extra.css').read_text(), 'p {color: blue;}')
        self.assertEqual(prepared.summary['resources'], 3)

    def test_missing_spine_entries_and_resources_are_rejected(self):
        error = self.rejected({'EPUB/z.xhtml': '<html><head/><body><img src="missing.png"/></body></html>'})
        self.assertEqual(error.diagnostic, 'missing_package_resource')

    def test_encryption_and_unsafe_zip_paths_are_rejected_without_extraction(self):
        self.rejected(extra={'META-INF/encryption.xml': '<encryption/>'}, code='EPUB_ENCRYPTION_UNSUPPORTED')
        self.rejected(extra={'../outside': b'escape'})
        self.assertFalse((self.root / 'outside').exists())

    def test_duplicate_casefold_paths_and_external_entities_are_rejected(self):
        self.rejected(extra={'epub/Z.xhtml': b'collision'})
        self.rejected({'EPUB/z.xhtml': '<!DOCTYPE html [<!ENTITY x "unsafe">]><html><head/><body>&x;</body></html>'})

    def test_fixed_layout_and_media_overlay_are_explicitly_unsupported(self):
        source = create_epub(self.root / 'source.epub')
        with zipfile.ZipFile(source) as z: package = z.read('EPUB/book.opf').decode()
        self.rejected({'EPUB/book.opf': package.replace('</metadata>',
            '<meta property="rendition:layout">pre-paginated</meta></metadata>')},
            code='EPUB_FIXED_LAYOUT_UNSUPPORTED')

    def test_invalid_and_mislabeled_image_resources_are_rejected(self):
        self.rejected({'EPUB/images/pixel.png': b'not a PNG'}, code='EPUB_IMAGE_INVALID')

    def test_foreign_elements_are_explicit_instead_of_losing_their_semantics(self):
        self.rejected({'EPUB/z.xhtml': '<html><head/><body xmlns:future="urn:future"><future:chart>Chart</future:chart></body></html>'},
                      code='EPUB_CHAPTER_FEATURE_UNSUPPORTED')

    def test_cancelled_preparation_does_not_create_private_tree(self):
        source = create_epub(self.root / 'source.epub')
        self.cancel.set()
        with self.assertRaises(GuardError) as error:
            prepare_epub(source, self.root / 'private', cancel=self.cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertFalse((self.root / 'private').exists())

    def test_source_prepared_chapter_and_resource_hashes_are_independent(self):
        source, prepared = self.prepare()
        summary = prepared.summary
        self.assertEqual(summary['sourceSha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(summary['preparedSha256'], hashlib.sha256(prepared.source.read_bytes()).hexdigest())
        self.assertFalse(summary['qualityCompared'])
        with zipfile.ZipFile(source) as archive:
            for chapter in summary['chapters']:
                self.assertEqual(chapter['sourceSha256'], hashlib.sha256(archive.read(chapter['archivePath'])).hexdigest())
            for resource in summary['resourceManifest']:
                self.assertEqual(resource['sourceSha256'], hashlib.sha256(archive.read(resource['archivePath'])).hexdigest())
                local = prepared.source.parent / '_epub-assets' / resource['archivePath']
                self.assertEqual(resource['preparedSha256'], hashlib.sha256(local.read_bytes()).hexdigest())

    def test_missing_fragment_is_rejected_before_native_import(self):
        source = create_epub(self.root / 'seed.epub')
        with zipfile.ZipFile(source) as archive: chapter = archive.read('EPUB/a.xhtml').decode()
        error = self.rejected({'EPUB/a.xhtml': chapter.replace('z.xhtml#part', 'z.xhtml#absent')})
        self.assertEqual(error.diagnostic, 'missing_chapter_fragment')

    def test_nonspine_xhtml_footnotes_are_appended_and_linked(self):
        source = create_epub(self.root / 'seed.epub')
        with zipfile.ZipFile(source) as archive:
            package = archive.read('EPUB/book.opf').decode()
            chapter = archive.read('EPUB/a.xhtml').decode()
        package = package.replace('</manifest>', '<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/></manifest>')
        chapter = chapter.replace('</body>', '<a href="notes.xhtml#n1">Footnote</a></body>')
        _, prepared = self.prepare({'EPUB/book.opf': package, 'EPUB/a.xhtml': chapter},
            {'EPUB/notes.xhtml': '<html><head/><body><p id="n1">AIO_NONSPINE_FOOTNOTE_017</p></body></html>'})
        content = prepared.source.read_text()
        self.assertLess(content.index(MARKERS[1]), content.index('AIO_NONSPINE_FOOTNOTE_017'))
        self.assertIn('href="#' + _anchor('EPUB/notes.xhtml', 'n1') + '"', content)
        self.assertEqual(prepared.summary['nonSpineItems'], 1)
        self.assertIn('EPUB_NONSPINE_XHTML_APPENDED', prepared.summary['warnings'])

    def test_simple_css_id_selectors_follow_renamed_anchors(self):
        _, prepared = self.prepare({'EPUB/styles/main.css':
            '#part {color: #184088;} @media print { h1#part { font-weight: bold; } }'})
        css = (prepared.source.parent / '_epub-assets/EPUB/styles/main.css').read_text()
        self.assertIn('#' + _anchor('EPUB/z.xhtml', 'part'), css)
        self.assertIn('#' + _anchor('EPUB/a.xhtml', 'part'), css)
        self.assertIn('color: #184088', css)
        self.assertNotIn('h1#part', css)

    def test_complex_css_id_scoping_fails_instead_of_changing_meaning(self):
        for css in ('p:not(#part) {color:red;}', 'a[href="#part"] {color:red;}'):
            with self.subTest(css=css):
                self.rejected({'EPUB/styles/main.css': css}, code='EPUB_CSS_UNSUPPORTED')

    def test_unmatched_complex_selector_does_not_block_other_content(self):
        _, prepared = self.prepare({'EPUB/styles/main.css':
            '#TOC a:not(:hover) {color: #184088;} #part {font-weight:bold;}'})
        css = (prepared.source.parent / '_epub-assets/EPUB/styles/main.css').read_text()
        self.assertIn('#TOC a:not(:hover)', css)
        self.assertIn('#' + _anchor('EPUB/a.xhtml', 'part'), css)

    def test_body_styles_and_controls_are_not_silently_ignored(self):
        for content in ('<style>p {background:url(https://remote.invalid/p.png)}</style>',
                        '<input value="important user value"/>'):
            with self.subTest(content=content):
                self.rejected({'EPUB/z.xhtml': '<html><head/><body>' + content + '</body></html>'})


if __name__ == '__main__': unittest.main()
