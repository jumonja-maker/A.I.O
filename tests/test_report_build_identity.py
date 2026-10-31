"""M30: exact build SHA is reported only when grounded in trusted metadata."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import sys
import unittest

from app_backend.converter_guard.build_info import report_build_identity


class BuildIdentityTests(unittest.TestCase):
    def test_explicit_revision(self):
        revision='1'*40
        with patch.dict(os.environ, {'AIO_BUILD_REVISION': revision}):
            value=report_build_identity()
        self.assertEqual(value['sourceRevision'],revision)
        self.assertEqual(value['basis'],'configured')

    def test_frozen_payload_revision(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'build-info'
            path.mkdir()
            sha='a'*40
            (path/'aio-source-revision.txt').write_text(sha+'\n',encoding='ascii')
            with patch.dict(os.environ, {'AIO_BUILD_REVISION': ''}):
                with patch.object(sys,'frozen',True,create=True), patch.object(sys,'_MEIPASS',folder,create=True):
                    value=report_build_identity()
            self.assertEqual(value['sourceRevision'],sha)
            self.assertEqual(value['basis'],'bundled')

    def test_invalid_claim_is_unknown(self):
        with patch.dict(os.environ, {'AIO_BUILD_REVISION':'not-a-sha'}):
            with patch.object(sys,'frozen',False,create=True):
                value=report_build_identity()
        self.assertIsNone(value['sourceRevision'])
        self.assertEqual(value['basis'],'unknown')


if __name__=='__main__':
    unittest.main()
