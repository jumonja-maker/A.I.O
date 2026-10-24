"""Integrity regressions: complete compressed streams and streamed HTML."""
import gzip
import io
from pathlib import Path
import tempfile
import threading
import tracemalloc
import unittest

from app_backend.converter_guard.errors import GuardError
from app_backend.converter_guard.postconditions import check_native_output


class OutputIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def check(self, data, target='emz', **kwargs):
        path = self.root / ('original.' + target)
        path.write_bytes(data)
        return check_native_output([path], target, self.root / 'original.bin',
                                   cancel=kwargs.pop('cancel', threading.Event()), **kwargs)

    def assert_failure(self, data, code, target='emz', **kwargs):
        with self.assertRaises(GuardError) as caught:
            self.check(data, target, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_complete_gzip_crc_and_size_are_checked_for_both_formats(self):
        original = gzip.compress(bytes(range(256)) * 500, mtime=0)
        for target in ('emz', 'wmz'):
            with self.subTest(target=target):
                result = self.check(original, target)
                self.assertEqual(result['status'], 'validated')
                self.assertEqual(result['uncompressedBytes'], 128000)
                for offset in (-8, -4):
                    corrupt = bytearray(original)
                    corrupt[offset] ^= 1
                    self.assert_failure(corrupt, 'ENGINE_OUTPUT_CONTAINER_INVALID', target)

    def test_truncated_member_and_trailing_garbage_are_rejected(self):
        original = gzip.compress(b'EMF content' * 20000, mtime=0)
        for corrupt in (original[:-1], original[:-8], original[:len(original)//2],
                        original + b'not another gzip member', b'not gzip'):
            with self.subTest(length=len(corrupt)):
                self.assert_failure(corrupt, 'ENGINE_OUTPUT_CONTAINER_INVALID')

    def test_filename_header_and_each_concatenated_member_are_checked(self):
        buffer = io.BytesIO()
        with gzip.GzipFile(filename='inner', fileobj=buffer, mode='wb', mtime=0) as stream:
            stream.write(b'first metafile')
        first = buffer.getvalue()
        second = gzip.compress(b'second metafile', mtime=0)
        self.assertEqual(self.check(first + second)['uncompressedBytes'], 29)
        corrupt = bytearray(second)
        corrupt[-8] ^= 1
        self.assert_failure(first + corrupt, 'ENGINE_OUTPUT_CONTAINER_INVALID')

    def test_empty_member_is_not_content(self):
        self.assert_failure(gzip.compress(b''), 'ENGINE_OUTPUT_CONTENT_EMPTY')

    def test_explicit_gzip_budget_fails_closed(self):
        error = self.assert_failure(gzip.compress(b'a' * 100000),
            'ENGINE_OUTPUT_VALIDATION_INCOMPLETE', budget=50000)
        self.assertEqual(error.diagnostic, 'gzip_inspection_budget')

    def test_cancel_is_polled_during_full_gzip_read(self):
        class CancelAfterReads:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls > 3
        self.assert_failure(gzip.compress(b'a' * (1024 * 1024)), 'CANCELLED',
                            cancel=CancelAfterReads())

    def test_large_inline_image_is_content_without_parser_budget_escape(self):
        data = b'<html><body><img src="data:image/png;base64,' + b'A' * (2*1024*1024)
        data += b'" title="a > b"></body></html>'
        result = self.check(data, 'html')
        self.assertEqual(result['status'], 'content_present')
        self.assertEqual(result['inspectedBytes'], len(data))
        self.assertFalse(result['qualityCompared'])

    def test_large_attribute_inspection_has_bounded_memory(self):
        path = self.root / 'original.html'
        with path.open('wb') as stream:
            stream.write(b'<body><img src="data:image/png;base64,')
            for _ in range(12):
                stream.write(b'A' * 1024 * 1024)
            stream.write(b'"></body>')
        tracemalloc.start()
        try:
            result = check_native_output([path], 'html', self.root / 'original.bmp',
                                         cancel=threading.Event())
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result['status'], 'content_present')
        self.assertLess(peak, 3 * 1024 * 1024)

    def test_content_after_former_one_mib_limit_is_checked(self):
        data = b'<html><body>' + b' ' * (2 * 1024 * 1024) + b'<p>preserved</p></body></html>'
        self.assertEqual(self.check(data, 'html')['status'], 'content_present')

    def test_empty_content_after_large_attribute_still_fails(self):
        data = b'<html><body><p title="' + b'A' * (2 * 1024 * 1024) + b'"></p></body></html>'
        self.assert_failure(data, 'ENGINE_OUTPUT_CONTENT_EMPTY', 'html')

    def test_unverified_dynamic_encoding_and_quotas_fail_closed(self):
        examples = (
            (b'<body><canvas></canvas><script>draw()</script></body>', {}, 'html_dynamic_content'),
            (b'<body>\xff</body>', {}, 'html_encoding_syntax_or_read:UnicodeDecodeError'),
            (b'<body>' + b' ' * 100 + b'text</body>', {'budget': 50}, 'html_inspection_budget'),
            (b'<body><img src="unfinished', {}, 'html_unterminated_markup'),
        )
        for data, kwargs, diagnostic in examples:
            with self.subTest(reason=diagnostic):
                error = self.assert_failure(data, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE', 'html', **kwargs)
                self.assertEqual(error.diagnostic, diagnostic)

    def test_quotes_in_comments_and_script_do_not_create_fake_content(self):
        data = b'<html><!-- unpaired " --><body><!-- <img src="fake.png"> -->'
        data += b'<script>var x="<img src=\'fake.png\'>";</script></body></html>'
        self.assert_failure(data, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE', 'html')
        self.assertEqual(self.check(b'<html><!-- unpaired " --><body>real</body></html>',
                                    'html')['status'], 'content_present')

    def test_rawtext_less_than_and_quotes_do_not_swallow_the_body(self):
        for head in (b'<script>var a = "<";</script>',
                     b'<style>p:before{content:"<"}</style>',
                     b'<script>' + b' ' * 65515 + b'var a="<";</script>'):
            with self.subTest(head=head[:40]):
                self.assertEqual(self.check(b'<html><head>' + head +
                    b'</head><body>Real content</body></html>', 'html')['status'], 'content_present')

    def test_missing_primary_fails_closed(self):
        with self.assertRaises(GuardError) as caught:
            check_native_output([], 'emz', self.root / 'original.bin', cancel=threading.Event())
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')

    def test_html_local_sidecars_must_exist_and_stay_in_output_directory(self):
        data = b'<html><body><img src="figure.png"></body></html>'
        self.assert_failure(data, 'ENGINE_OUTPUT_RESOURCE_MISSING', 'html')
        (self.root / 'figure.png').write_bytes(b'image bytes')
        self.assertTrue(self.check(data, 'html')['localResourcesChecked'])
        self.assert_failure(b'<body><img src="../outside.png"></body>',
                            'ENGINE_OUTPUT_RESOURCE_MISSING', 'html')

    def test_html_checks_css_dependencies_without_fetching_remote_urls(self):
        (self.root / 'style.css').write_text('body { background: url(missing.png); }')
        self.assert_failure(b'<html><head><link rel="stylesheet" href="style.css"></head>'
                            b'<body>content</body></html>', 'ENGINE_OUTPUT_RESOURCE_MISSING', 'html')
        result = self.check(b'<body><img src="https://example.invalid/not-fetched.png"></body>', 'html')
        self.assertTrue(result['localResourcesChecked'])
        with self.assertRaises(GuardError) as caught:
            check_native_output([], 'html', self.root / 'original.bin', cancel=threading.Event())
        self.assertEqual(caught.exception.code, 'ENGINE_OUTPUT_VALIDATION_INCOMPLETE')


if __name__ == '__main__':
    unittest.main()
