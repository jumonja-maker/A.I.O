"""M19: the PPT/XHTML recovery must never promote an unknown visual slide."""
import ast
from collections import Counter
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import os
import re
import tempfile
from types import SimpleNamespace
import unicodedata
import unittest

from app_backend.converter_guard.uno_session import WORKER_SOURCE


def load_worker_contract():
    source = ast.parse(WORKER_SOURCE)
    names = {'AioPresentationTextLoss', 'AioPresentationUnverified',
             'aio_normal_text', '_AioExportText', 'aio_verify_presentation_xhtml',
             '_AioXhtmlVisualScope', 'aio_xhtml_visual_scope',
             'aio_try_ppt_xhtml_textboxes'}
    selected = [node for node in source.body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert {node.name for node in selected} == names
    context = {'Counter': Counter, 'HTMLParser': HTMLParser, 'Path': Path, 'hashlib': hashlib,
               'os': os, 're': re, 'unicodedata': unicodedata}
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'worker_contract', 'exec'), context)
    return context


WORKER = load_worker_contract()


class Shape:
    def __init__(self, kind='CustomShape', content='SLIDE_TEXT', geometry='mso-spt202'):
        self.kind, self.String = kind, content
        self.CustomShapeGeometry = [SimpleNamespace(Name='Type', Value=geometry)]
        self.Position, self.Size = (12, 34), (500, 200)
    def getShapeType(self):
        return 'com.sun.star.drawing.' + self.kind
    def getString(self):
        return self.String


class Page:
    def __init__(self, shapes):
        self.shapes = list(shapes)
    def getCount(self):
        return len(self.shapes)
    def getByIndex(self, index):
        return self.shapes[index]
    def add(self, item):
        self.shapes.append(item)


class NativeModel:
    def __init__(self, target, pages, *, deliberately_omit=False):
        self.target, self.pages = target, pages
        self.deliberately_omit = deliberately_omit
        self.store_calls = 0
    def getDrawPages(self):
        parent = self
        class DrawPages:
            def getCount(self):
                return len(parent.pages)
            def getByIndex(self, index):
                return parent.pages[index]
        return DrawPages()
    def createInstance(self, name):
        assert name == 'com.sun.star.drawing.TextShape'
        return Shape(kind='TextShape', content='')
    def storeToURL(self, url, properties):
        self.store_calls += 1
        assert url.endswith('__aio_textbox_recovery.xhtml')
        from html import escape
        content = '' if self.deliberately_omit else ''.join(
            '<p>' + escape(shape.String) + '</p>'
            for page in self.pages for shape in page.shapes if shape.kind == 'TextShape')
        temporary = self.target.with_name(self.target.stem + '__aio_textbox_recovery.xhtml')
        temporary.write_text('<html><body>' + content + '</body></html>', encoding='utf-8')


class TextboxOnlyNativeXhtml(unittest.TestCase):
    def call(self, model, expected):
        return WORKER['aio_try_ppt_xhtml_textboxes'](model, model.target, [], Counter(expected))

    def test_three_legacy_textboxes_restore_all_text_and_promote_only_verified_output(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / 'slides.xhtml'
            target.write_bytes(b'old lost text')
            model = NativeModel(target, [
                Page([Shape(content='AIO_SLIDE_1')]),
                Page([Shape(content='AIO_SLIDE_2')]),
                Page([Shape(content='AIO_SLIDE_3')])])
            result = self.call(model, {'AIO_SLIDE_1':1, 'AIO_SLIDE_2':1, 'AIO_SLIDE_3':1})
            self.assertEqual(result['status'], 'checked_subset')
            self.assertEqual(result['nativeTextboxCount'], 3)
            self.assertTrue(result['textPresent'])
            self.assertFalse(result['qualityCompared'])
            self.assertIn('visual_equivalence', result['notChecked'])
            self.assertEqual(model.store_calls, 1)
            for index in range(1,4):
                self.assertIn('AIO_SLIDE_'+str(index), target.read_text())
            self.assertFalse(target.with_name('slides__aio_textbox_recovery.xhtml').exists())

    def test_real_image_shape_blocks_bridge_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / 'slides.xhtml'
            page = Page([Shape(), Shape(kind='GraphicObjectShape', content='')])
            model = NativeModel(target, [page])
            self.assertIsNone(self.call(model, {'SLIDE_TEXT':1}))
            self.assertEqual(len(page.shapes), 2)
            self.assertEqual(model.store_calls, 0)

    def test_non_textbox_custom_shape_never_becomes_text(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / 'slides.xhtml'
            model = NativeModel(target, [Page([Shape(geometry='chevron')])])
            self.assertIsNone(self.call(model, {'SLIDE_TEXT':1}))
            self.assertEqual(model.store_calls, 0)

    def test_missing_text_is_not_treated_as_success(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / 'slides.xhtml'
            model = NativeModel(target, [Page([Shape(content='')])])
            self.assertIsNone(self.call(model, {'MISSING':1}))
            self.assertEqual(model.store_calls, 0)

    def test_failed_retry_keeps_original_and_cleans_partial(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / 'slides.xhtml'
            target.write_bytes(b'old-primary')
            model = NativeModel(target, [Page([Shape()])], deliberately_omit=True)
            with self.assertRaises(WORKER['AioPresentationTextLoss']):
                self.call(model, {'SLIDE_TEXT':1})
            self.assertEqual(target.read_bytes(), b'old-primary')
            self.assertFalse(target.with_name('slides__aio_textbox_recovery.xhtml').exists())

    def test_bounded_shapes_and_pages(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / 'slides.xhtml'
            too_many = NativeModel(target, [Page([Shape() for _ in range(257)])])
            self.assertIsNone(self.call(too_many, {'SLIDE_TEXT':1}))
            self.assertEqual(too_many.store_calls, 0)
            too_many_pages = NativeModel(target, [Page([Shape()]) for _ in range(33)])
            self.assertIsNone(self.call(too_many_pages, {'SLIDE_TEXT':1}))
            self.assertEqual(too_many_pages.store_calls, 0)


class MixedNativeModel(NativeModel):
    """A controlled native XHTML encoder that retains embedded image data."""
    def __init__(self, target, pages, *, image_src='data:image/png;base64,QUJD',
                 drop_image=False, change_container=False):
        super().__init__(target, pages)
        self.image_src, self.drop_image = image_src, drop_image
        self.change_container = change_container

    def storeToURL(self, url, properties):
        self.store_calls += 1
        from html import escape
        sections = []
        for page in self.pages:
            sections.extend('<p>' + escape(shape.String) + '</p>'
                            for shape in page.shapes
                            if shape.kind == 'TextShape' and shape.String)
        image = '' if self.drop_image else '<img src="' + self.image_src + '"/>'
        fixed = ('<div class="OTHER">' if self.change_container
                 else '<div class="original">')
        self.target.with_name(self.target.stem + '__aio_textbox_recovery.xhtml').write_text(
            '<html><body>' + fixed + image + '</div>' +
            ''.join(sections) + '</body></html>', encoding='utf-8')


class MixedPptXhtmlFidelity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'slides.xhtml'
        self.background = ('<html><body><div class="original">'
                           '<img src="data:image/png;base64,QUJD"/></div></body></html>')
        self.target.write_text(self.background, encoding='utf-8')

    def model(self, **kwargs):
        return MixedNativeModel(self.target, [
            Page([Shape(content='AIO_TEXT', geometry='rectangle'),
                  Shape(content='', geometry='round-rectangle'),
                  Shape(kind='GraphicObjectShape', content='')])], **kwargs)

    def call(self, model, target=None):
        return WORKER['aio_try_ppt_xhtml_textboxes'](
            model, target or self.target, [], Counter({'AIO_TEXT': 1}))

    def test_embedded_image_and_original_container_retained_during_recovery(self):
        model = self.model()
        receipt = self.call(model)
        self.assertEqual(receipt['status'], 'checked_subset')
        self.assertEqual(receipt['nativeImageCount'], 1)
        self.assertTrue(receipt['visualSubsetCompared'])
        self.assertTrue(receipt['textPresent'])
        self.assertFalse(receipt['qualityCompared'])
        self.assertIn('AIO_TEXT', self.target.read_text())
        self.assertIn('<img src="data:image/png;base64,QUJD"/>',
                      self.target.read_text())
        self.assertEqual(model.store_calls, 1)
        self.assertFalse(self.root.joinpath('slides__aio_textbox_recovery.xhtml').exists())

    def test_changed_image_payload_refuses_commit_and_preserves_primary(self):
        model = self.model(image_src='data:image/png;base64,REVG')
        with self.assertRaisesRegex(WORKER['AioPresentationUnverified'],
                                    'visual_subset_changed'):
            self.call(model)
        self.assertEqual(self.target.read_text(), self.background)
        self.assertFalse((self.root / 'slides__aio_textbox_recovery.xhtml').exists())

    def test_missing_image_refuses_commit(self):
        model = self.model(drop_image=True)
        with self.assertRaises(WORKER['AioPresentationUnverified']):
            self.call(model)
        self.assertEqual(self.target.read_text(), self.background)

    def test_changed_visual_container_refuses_commit(self):
        model = self.model(change_container=True)
        with self.assertRaises(WORKER['AioPresentationUnverified']):
            self.call(model)
        self.assertEqual(self.target.read_text(), self.background)

    def test_missing_source_image_refuses_before_mutation(self):
        self.target.write_text('<html><body><div class="original"/></body></html>')
        model = self.model()
        self.assertIsNone(self.call(model))
        self.assertEqual(model.store_calls, 0)
        self.assertEqual(len(model.pages[0].shapes), 3)

    def test_external_image_refuses_before_mutation(self):
        self.target.write_text(
            '<html><body><div class="original"><img src="file:///tmp/private.png"/></div></body></html>')
        model = self.model()
        self.assertIsNone(self.call(model))
        self.assertEqual(model.store_calls, 0)

    def test_unknown_geometry_refuses_before_mutation(self):
        model = MixedNativeModel(self.target, [
            Page([Shape(content='AIO_TEXT', geometry='chevron'),
                  Shape(kind='GraphicObjectShape', content='')])])
        self.assertIsNone(self.call(model))
        self.assertEqual(model.store_calls, 0)

    def test_extra_graphic_reference_refuses_before_mutation(self):
        self.target.write_text(
            '<html><body><div class="original"><img src="data:image/png;base64,QUJD"/>'
            '<img src="data:image/png;base64,QUJD"/></div></body></html>')
        model = self.model()
        self.assertIsNone(self.call(model))
        self.assertEqual(model.store_calls, 0)

    def test_svg_graphic_requires_a_different_quality_contract(self):
        self.target.write_text('<html><body><svg xmlns="http://www.w3.org/2000/svg"/>'
                               '<div class="original"><img src="data:image/png;base64,QUJD"/>'
                               '</div></body></html>')
        model = self.model()
        self.assertIsNone(self.call(model))
        self.assertEqual(model.store_calls, 0)


if __name__ == '__main__':
    unittest.main()
