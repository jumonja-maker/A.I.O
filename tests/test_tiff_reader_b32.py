from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.qt_media import _frames


class TiffAbsolutePages(unittest.TestCase):
    def test_each_validated_ifd_has_its_own_reader_and_keeps_thumbnail_mapping(self):
        created=[]
        class Reader:
            def __init__(self,path):self.index=0;self.reads=0;created.append(self)
            def supportsAnimation(self):return False
            def jumpToImage(self,index):
                if self.reads:return False  # observed Windows Qt TIFF state
                self.index=index;return True
            def size(self):return SimpleNamespace(width=lambda:4,height=lambda:3)
            def read(self):
                self.reads+=1
                return SimpleNamespace(isNull=lambda:False,width=lambda:4,height=lambda:3,index=self.index)
        profile={'format':'tiff','frames':1,'pages':6,'pageImageIndexes':[0,2,3,5,6,7]}
        with patch('app_backend.converter_guard.qt_media._qt',return_value=(None,)*5+(Reader,)), \
             patch('app_backend.converter_guard.qt_media._orient_image',side_effect=lambda image,_:image):
            images=list(_frames(Path('input.tiff'),Path('.'),profile,threading.Event()))
        self.assertEqual([image.index for image in images],[0,2,3,5,6,7])
        self.assertTrue(all(reader.reads<=1 for reader in created))

    def test_missing_page_still_fails_and_cancel_does_not_decode(self):
        class Reader:
            def __init__(self,path):pass
            def supportsAnimation(self):return False
            def jumpToImage(self,index):return False
            def size(self):return SimpleNamespace(width=lambda:4,height=lambda:3)
            def read(self):return SimpleNamespace(isNull=lambda:False,width=lambda:4,height=lambda:3)
        profile={'format':'tiff','frames':1,'pages':2}
        with patch('app_backend.converter_guard.qt_media._qt',return_value=(None,)*5+(Reader,)), \
             patch('app_backend.converter_guard.qt_media._orient_image',side_effect=lambda image,_:image):
            with self.assertRaises(GuardError) as error:list(_frames(Path('input.tif'),Path('.'),profile,threading.Event()))
            self.assertEqual(error.exception.code,'MEDIA_DECODE_FAILED')
            cancel=threading.Event();cancel.set()
            with self.assertRaises(GuardError) as error:list(_frames(Path('input.tif'),Path('.'),profile,cancel))
            self.assertEqual(error.exception.code,'CANCELLED')
