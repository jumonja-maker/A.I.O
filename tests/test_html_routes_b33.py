import ast
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard.routes import Registry
from app_backend.converter_guard.uno_session import WORKER_SOURCE
from test_converter_guard_v04 import component,node

P='com.sun.star.presentation.PresentationDocument';D='com.sun.star.drawing.DrawingDocument'

class HtmlRouteSafety(unittest.TestCase):
    def test_native_shape_profile_is_bounded_and_distinguishes_text_and_graphics(self):
        tree=ast.parse(WORKER_SOURCE)
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='inspect_html_content_profile')
        scope={};exec(compile(ast.Module(body=[function],type_ignores=[]),'native-html-profile','exec'),scope)
        def group(shapes):return SimpleNamespace(getCount=lambda:len(shapes),getByIndex=lambda i:shapes[i])
        def shape(kind):return SimpleNamespace(getShapeType=lambda:'com.sun.star.drawing.'+kind)
        text=shape('TextShape');image=shape('GraphicObjectShape')
        model=SimpleNamespace(getDrawPages=lambda:group([group([text]),group([image])]))
        profile=scope['inspect_html_content_profile'](model)
        self.assertEqual(profile,{'complete':True,'textShapes':1,'graphicShapes':1,'pages':2})
        self.assertFalse(scope['inspect_html_content_profile'](object())['complete'])

    def test_known_html_filter_restrictions_are_upfront_and_keep_good_text_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);exe=root/'soffice';exe.touch();reg=root/'registry.xcd'
            types=node('html',Extensions='html htm')+node('pres',Extensions='odp')+node('draw',Extensions='odg')
            filters=(node('impress8',Type='pres',DocumentService=P,Flags='IMPORT EXPORT')+
                node('draw8',Type='draw',DocumentService=D,Flags='IMPORT EXPORT')+
                node('impress_html_Export',Type='html',DocumentService=P,Flags='EXPORT')+
                node('draw_html_Export',Type='html',DocumentService=D,Flags='EXPORT'))
            reg.write_text('<oor:data xmlns:oor="http://openoffice.org/2001/registry">'+
                component('Types','Types',types)+component('Filter','Filters',filters)+'</oor:data>')
            native=Registry(((str(reg),0,reg.stat().st_size),),exe)
            proof={'state':'detected','basis':'native_preflight_uno_document','modelLoaded':True,
                'registryFingerprint':native.fingerprint,'documentServices':[P],
                'htmlContentProfile':{'complete':True,'textShapes':2,'graphicShapes':0,'pages':2}}
            self.assertEqual(native.observed_decision('htm',proof),'registered')
            for profile in (None,{'complete':False},{'complete':True,'graphicShapes':1}):
                bad={**proof,'htmlContentProfile':profile}
                self.assertEqual(native.observed_decision('htm',bad),'unavailable')
                self.assertTrue(native.route_reason('htm',bad).startswith('NATIVE_PRESENTATION_HTML_'))
            draw={**proof,'documentServices':[D]}
            self.assertEqual(native.observed_decision('html',draw),'unavailable')
            self.assertEqual(native.route_reason('html',draw),'NATIVE_DRAW_HTML_CONTENT_UNSUPPORTED')
            # A known native single-page XHTML alternative is preserved.
            native.filters['XHTML Draw File']=(D,{'xhtml'},{'EXPORT'})
            native.export_candidates['xhtml',D]={'XHTML Draw File'}
            single={**draw,'drawPages':1}
            self.assertIsNone(native.html_route_reason('html',single))
            # Ambiguous static overlays must not bypass the known automatic
            # Impress HTML fallback's graphical-content restriction.
            with patch.object(native,'export_filter',return_value=None):
                bad={**proof,'htmlContentProfile':{'complete':True,'graphicShapes':1}}
                self.assertEqual(native.observed_decision('html',bad),'unavailable')
