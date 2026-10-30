"""M19: the PPT/XHTML recovery must never promote an unknown visual slide."""
import ast
from collections import Counter
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
             'aio_try_ppt_xhtml_textboxes'}
    selected = [node for node in source.body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert {node.name for node in selected} == names
    context = {'Counter': Counter, 'HTMLParser': HTMLParser, 'Path': Path,
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


if __name__ == '__main__':
    unittest.main()
