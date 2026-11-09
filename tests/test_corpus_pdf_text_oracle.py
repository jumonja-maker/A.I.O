"""Independent positioned text checks must detect loss and font-run ordering."""
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                          NameObject, NumberObject)

from tools.qa.corpus_pdf_text_oracle import inspect_pdf_literals


class PositionedLiteralReadback(unittest.TestCase):
    def make_pdf(self, folder, *, rotated=False):
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=100)
        def font(mapping):
            value = DictionaryObject({NameObject('/Type'):NameObject('/Font'),
                NameObject('/Subtype'):NameObject('/TrueType'),
                NameObject('/BaseFont'):NameObject('/SyntheticQAFont'),
                NameObject('/FirstChar'):NumberObject(0), NameObject('/LastChar'):NumberObject(255),
                NameObject('/Widths'):ArrayObject([NumberObject(500) for _ in range(256)]),
                NameObject('/Encoding'):NameObject('/WinAnsiEncoding')})
            cmap = DecodedStreamObject()
            entries = '\n'.join('<%02X> <%04X>' % (code, ord(text)) for code, text in mapping.items())
            cmap.set_data(('begincmap\n1 begincodespacerange\n<00> <FF>\nendcodespacerange\n'
                           +str(len(mapping))+' beginbfchar\n'+entries+'\nendbfchar\nendcmap').encode())
            value[NameObject('/ToUnicode')] = writer._add_object(cmap)
            return writer._add_object(value)
        fonts = DictionaryObject({NameObject('/Latin'):font({i:chr(i) for i in range(32,127)}),
                                  NameObject('/Russian'):font({65:'М',66:'и',67:'р'})})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):fonts})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /Latin 10 Tf 1 0 0 1 20 80 Tm [(ALPHA) -4000 (OMEGA)] TJ ET\n'
                        +(b'BT /Russian 10 Tf 0 1 -1 0 50 20 Tm (ABC) Tj ET' if rotated else
                          b'BT /Russian 10 Tf 1 0 0 1 50 80 Tm (ABC) Tj ET'))
        page[NameObject('/Contents')] = writer._add_object(stream)
        path = folder/'positioned.pdf'
        with path.open('wb') as file:
            writer.write(file)
        return path

    def test_interleaved_font_runs_are_reconstructed_by_glyph_position(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_pdf(Path(temp))
            result = inspect_pdf_literals(path, ['ALPHA Мир OMEGA'])
            self.assertEqual(result['status'], 'PASS', result)
            self.assertIn('ALPHAМирOMEGA', result['positionedPages'][0]['text'])
            self.assertFalse(result['visualEquivalenceChecked'])

    def test_missing_literal_is_not_reclassified_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_pdf(Path(temp))
            result = inspect_pdf_literals(path, ['ALPHA Мир OMEGA', 'ACTUALLY_LOST'])
            self.assertEqual(result['status'], 'MISSING', result)
            self.assertEqual(result['missingLiterals'], ['ACTUALLY_LOST'])

    def test_unsupported_rotation_is_uncertain_when_literal_not_reconstructed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_pdf(Path(temp), rotated=True)
            result = inspect_pdf_literals(path, ['ALPHA Мир OMEGA'])
            self.assertEqual(result['status'], 'UNCERTAIN', result)
            self.assertIn('rotated_sheared_or_reversed_text', result['issues'])

    def test_empty_expected_text_does_not_claim_text_quality(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_pdf(Path(temp))
            result = inspect_pdf_literals(path, [])
            self.assertEqual(result['status'], 'NOT_CHECKED', result)


if __name__ == '__main__':
    unittest.main()
