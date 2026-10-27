"""Real QtPdf page renderer behind the authenticated, leased HTTP preview."""
import importlib.util
import io
import struct
import zlib
import unittest
from urllib.parse import urlencode
from pypdf import PdfWriter
import test_comparison_service_v19 as fixtures


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Existing app Qt dependency required')
class PagePreview(unittest.TestCase):
    setUp=fixtures.ComparisonServiceContracts.setUp
    tearDown=fixtures.ComparisonServiceContracts.tearDown
    request=fixtures.ComparisonServiceContracts.request
    json_request=fixtures.ComparisonServiceContracts.json_request
    converted=fixtures.ComparisonServiceContracts.converted
    assert_http_code=fixtures.ComparisonServiceContracts.assert_http_code

    def test_pdf_pages_are_bounded_png_not_a_sandboxed_browser_plugin(self):
        from PySide6.QtGui import QImage
        out=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=400,height=300);writer.add_blank_page(width=300,height=400);writer.write(out)
        state=self.converted(name='pages.pdf',formats=['pdf'],data=out.getvalue())
        identity=state['jobId'];task=state['files'][0]['targets'][0]['taskId'];images=[]
        for page in range(2):
            query=urlencode({'job':identity,'task':task,'side':'result','page':page})
            with self.request('compare/preview?'+query) as response:
                self.assertEqual(response.headers['Content-Type'],'image/png')
                self.assertEqual(response.headers['X-AIO-Preview-Pages'],'2')
                self.assertEqual(response.headers['X-AIO-Preview-Page'],str(page))
                raw=response.read();image=QImage.fromData(raw)
                self.assertFalse(image.isNull());self.assertLessEqual(image.width(),1400);self.assertLessEqual(image.height(),1800)
                self.assertEqual(image.pixelColor(0,0).getRgb(),(255,255,255,255))
                images.append((image.width(),image.height()))
        self.assertNotEqual(images[0],images[1])
        self.assert_http_code(403,'compare/preview?'+query,auth=False)
        bad=urlencode({'job':identity,'task':task,'side':'result','page':-1})
        self.assert_http_code(400,'compare/preview?'+bad)
        unknown=urlencode({'job':identity,'task':task,'side':'result','page':999})
        self.assertEqual(self.json_request('compare/preview?'+unknown)['kind'],'unavailable')
        self.assertTrue(self.guard._jobs[identity].files[0].source.is_file())

    def test_bad_pdf_cannot_crash_the_host_or_be_shown_as_working_preview(self):
        state=self.converted(name='invalid.pdf',formats=['pdf'],data=b'NOT A PDF')
        query=urlencode({'job':state['jobId'],'task':state['files'][0]['targets'][0]['taskId'],'side':'source'})
        result=self.json_request('compare/preview?'+query)
        self.assertEqual(result['kind'],'unavailable')
        self.assertIn('note',result)
        self.assertTrue(self.guard.status(state['jobId'])['finished'])

    def test_raster_preview_is_bounded_png_with_alpha_and_original_untouched(self):
        from PySide6.QtGui import QImage
        from PySide6.QtCore import QBuffer, QIODevice
        picture=QImage(2400,1200,QImage.Format.Format_ARGB32);picture.fill(0x7fff0000)
        stream=QBuffer();stream.open(QIODevice.OpenModeFlag.WriteOnly);self.assertTrue(picture.save(stream,'PNG'));raw=bytes(stream.data())
        state=self.converted(name='photo.png',formats=['png'],data=raw)
        meta=self.server.api.comparison._preview_meta(state['jobId'],state['files'][0]['targets'][0]['taskId'],'result',self.guard._jobs[state['jobId']].files[0].source)
        self.assertIn('страницы и кадры',meta['note'])
        query=urlencode({'job':state['jobId'],'task':state['files'][0]['targets'][0]['taskId'],'side':'result'})
        with self.request('compare/preview?'+query) as response:
            preview=response.read();image=QImage.fromData(preview)
            self.assertEqual(response.headers['Content-Type'],'image/png')
            self.assertEqual((image.width(),image.height()),(2400,1200))
            self.assertEqual(response.headers['X-AIO-Preview-Width'],'2400')
            self.assertEqual(response.headers['X-AIO-Preview-Rendered-Width'],'2400')
            self.assertLess(image.pixelColor(0,0).alpha(),255)
        with self.guard.comparison_inputs(state['jobId']) as records:
            self.assertEqual(records[0]['paths'][0].read_bytes(),raw)
        self.assertTrue(preview)

    def test_huge_raster_header_is_not_sent_to_the_browser_decoder(self):
        def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
        raw=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',70000,70000,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0'))+chunk(b'IEND',b'')
        state=self.converted(name='huge.png',formats=['png'],data=raw)
        query=urlencode({'job':state['jobId'],'task':state['files'][0]['targets'][0]['taskId'],'side':'result'})
        result=self.json_request('compare/preview?'+query)
        self.assertEqual(result['kind'],'unavailable')
        self.assertTrue(self.guard.status(state['jobId'])['finished'])

    def test_selected_apng_frame_has_correct_pixels_and_bounds(self):
        from PySide6.QtGui import QImage,QColor
        from app_backend.converter_guard.qt_media import _write_apng
        frames=[]
        for colour in ((255,0,0,255),(0,0,255,255)):
            image=QImage(80,40,QImage.Format.Format_RGBA8888);image.fill(QColor(*colour))
            self.assertEqual(image.pixelColor(10,10).getRgb(),colour,'fixture must contain the intended source colour')
            frames.append(image)
        stream=io.BytesIO();_write_apng(stream,iter(frames),{'width':80,'height':40,'frames':2,'pages':1,'durationsMs':[100,100]},None)
        raw=stream.getvalue();state=self.converted(name='frames.apng',formats=['apng'],data=raw)
        for page,expected in ((0,(255,0,0,255)),(1,(0,0,255,255))):
            query=urlencode({'job':state['jobId'],'task':state['files'][0]['targets'][0]['taskId'],'side':'result','page':page})
            with self.request('compare/preview?'+query) as response:
                image=QImage.fromData(response.read());self.assertFalse(image.isNull())
                self.assertEqual(image.pixelColor(10,10).getRgb(),expected)
                self.assertEqual(response.headers['X-AIO-Preview-Pages'],'2')
                self.assertEqual(response.headers['X-AIO-Preview-Page'],str(page))
        self.assertEqual(self.guard._jobs[state['jobId']].files[0].source.read_bytes(),raw)
        query=urlencode({'job':state['jobId'],'task':state['files'][0]['targets'][0]['taskId'],'side':'result','page':2})
        self.assertEqual(self.json_request('compare/preview?'+query)['kind'],'unavailable')
