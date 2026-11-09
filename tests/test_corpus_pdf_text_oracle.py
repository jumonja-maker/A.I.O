"""Independent positioned text checks must detect loss and font-run ordering."""
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                          NameObject, NumberObject)

from tools.qa.corpus_pdf_text_oracle import inspect_pdf_literals


class PositionedLiteralReadback(unittest.TestCase):
    def make_pdf(self, folder, *, rotated=False, interleaved_widths=False):
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=100)
        def font(mapping, *, variable=False):
            widths = [NumberObject(500) for _ in range(256)]
            if variable:
                widths[65], widths[66], widths[67] = map(NumberObject, (1000, 200, 1000))
            value = DictionaryObject({NameObject('/Type'):NameObject('/Font'),
                NameObject('/Subtype'):NameObject('/TrueType'),
                NameObject('/BaseFont'):NameObject('/SyntheticQAFont'),
                NameObject('/FirstChar'):NumberObject(0), NameObject('/LastChar'):NumberObject(255),
                NameObject('/Widths'):ArrayObject(widths),
                NameObject('/Encoding'):NameObject('/WinAnsiEncoding')})
            cmap = DecodedStreamObject()
            entries = '\n'.join('<%02X> <%04X>' % (code, ord(text)) for code, text in mapping.items())
            cmap.set_data(('begincmap\n1 begincodespacerange\n<00> <FF>\nendcodespacerange\n'
                           +str(len(mapping))+' beginbfchar\n'+entries+'\nendbfchar\nendcmap').encode())
            value[NameObject('/ToUnicode')] = writer._add_object(cmap)
            return writer._add_object(value)
        fonts = DictionaryObject({NameObject('/Latin'):font({i:chr(i) for i in range(32,127)}),
                                  NameObject('/Russian'):font({65:'М',66:'и',67:'р'}, variable=interleaved_widths)})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):fonts})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /Latin 10 Tf 1 0 0 1 20 80 Tm [(ALPHA) -4000 (OMEGA)] TJ ET\n'
                        +(b'BT /Russian 10 Tf 0 1 -1 0 50 20 Tm (ABC) Tj ET' if rotated else
                          b'BT /Russian 10 Tf 1 0 0 1 50 80 Tm (ABC) Tj ET')
                        +(b'BT /Latin 10 Tf 1 0 0 1 61 80 Tm (X) Tj ET' if interleaved_widths else b''))
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

    def test_nonuniform_raw_font_widths_control_cross_font_order(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_pdf(Path(temp), interleaved_widths=True)
            # Actual positions: М=50, и=60, X=61, р=62 points. Substituting
            # average widths for decoded Cyrillic would put X after all three.
            result = inspect_pdf_literals(path, ['ALPHA МиXр OMEGA'])
            self.assertEqual(result['status'], 'PASS', result)
            self.assertEqual(result['ordinaryOrderFallbackLiteralCount'], 0)

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

    def marked_pdf(self, folder, content, *, named=False):
        seed=self.make_pdf(folder)
        writer=PdfWriter();page=writer.add_page(PdfReader(seed).pages[0])
        if named:
            from pypdf.generic import TextStringObject
            props=DictionaryObject({NameObject('/P1'):writer._add_object(DictionaryObject({
                NameObject('/ActualText'):TextStringObject('NAMED')}))})
            page['/Resources'][NameObject('/Properties')]=writer._add_object(props)
        stream=DecodedStreamObject()
        stream.set_data(b'BT /Latin 10 Tf 1 0 0 1 20 80 Tm '+content+b' ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
        target=folder/'marked.pdf'
        with target.open('wb') as file:writer.write(file)
        return target

    def test_utf16_actualtext_replaces_all_glyphs_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.marked_pdf(Path(temp),
                b'(LEFT) Tj /Span << /ActualText <FEFF0438> >> BDC (wrong) Tj EMC (RIGHT) Tj')
            result=inspect_pdf_literals(path,['LEFTиRIGHT'])
            self.assertEqual(result['status'],'PASS',result)
            self.assertEqual(result['positionedPages'][0]['actualTextSpans'],1)
            self.assertTrue(result['ordinaryFallbackSuppressedForActualText'])
            lost=inspect_pdf_literals(path,['LEFTwrongRIGHT'])
            self.assertEqual(lost['status'],'MISSING',lost)

    def test_empty_actualtext_suppresses_hyphen_without_ordinary_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.marked_pdf(Path(temp),b'(foo) Tj /Span << /ActualText () >> BDC (-) Tj EMC (bar) Tj')
            result=inspect_pdf_literals(path,['foobar'])
            self.assertEqual(result['status'],'PASS',result)
            lost=inspect_pdf_literals(path,['foo-bar'])
            self.assertEqual(lost['status'],'MISSING',lost)

    def test_pdfdocencoding_actualtext_is_not_a_utf16_only_shortcut(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.marked_pdf(Path(temp),b'/Span << /ActualText (RIGHT) >> BDC (wrong) Tj EMC')
            self.assertEqual(inspect_pdf_literals(path,['RIGHT'])['status'],'PASS')
        with tempfile.TemporaryDirectory() as temp:
            path=self.marked_pdf(Path(temp),b'/Span << /ActualText <80> >> BDC (wrong) Tj EMC')
            self.assertEqual(inspect_pdf_literals(path,['\u2022'])['status'],'PASS')

    def test_actualtext_aware_logical_order_keeps_overlapping_draw_literals(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.marked_pdf(Path(temp),b'[(ALPHA) -4000 (OMEGA)] TJ '
                b'/Russian 10 Tf 1 0 0 1 50 80 Tm (ABC) Tj '
                b'/Span << /ActualText (REPLACED) >> BDC '
                b'/Latin 10 Tf 1 0 0 1 20 60 Tm (wrong) Tj EMC')
            result=inspect_pdf_literals(path,['ALPHAOMEGA','Мир','REPLACED'])
            self.assertEqual(result['status'],'PASS',result)
            self.assertTrue(result['ordinaryFallbackSuppressedForActualText'])
            self.assertEqual(result['actualTextAwareLogicalOrderLiteralCount'],1)

    def test_named_property_resources_resolve_actualtext(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.marked_pdf(Path(temp),b'/Span /P1 BDC (wrong) Tj EMC',named=True)
            self.assertEqual(inspect_pdf_literals(path,['NAMED'])['status'],'PASS')

    def test_nested_marked_content_uses_outer_replacement_without_double_count(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.marked_pdf(Path(temp),b'/Span << /ActualText (OUTER) >> BDC '
                b'/Span << /ActualText (INNER) >> BDC (wrong) Tj EMC /Span BMC (tail) Tj EMC EMC')
            result=inspect_pdf_literals(path,['OUTER'])
            self.assertEqual(result['status'],'PASS',result)
            self.assertEqual(result['positionedPages'][0]['actualTextSpans'],2)
            self.assertEqual(inspect_pdf_literals(path,['OUTERINNER'])['status'],'MISSING')

    def test_malformed_actualtext_never_claims_success_from_matching_raw_text(self):
        variants=(b'/Span << /ActualText <FEFF04> >> BDC (RIGHT) Tj EMC',
                  b'/Span << /ActualText 42 >> BDC (RIGHT) Tj EMC',
                  b'/Span /Unknown BDC (RIGHT) Tj EMC')
        for index,content in enumerate(variants):
            with self.subTest(index=index),tempfile.TemporaryDirectory() as temp:
                result=inspect_pdf_literals(self.marked_pdf(Path(temp),content),['RIGHT'])
                self.assertEqual(result['status'],'UNCERTAIN',result)

    def test_unbalanced_marked_content_is_explicit_uncertainty(self):
        for content in (b'(RIGHT) Tj EMC',b'/Span << /ActualText (RIGHT) >> BDC (RIGHT) Tj'):
            with self.subTest(content=content),tempfile.TemporaryDirectory() as temp:
                result=inspect_pdf_literals(self.marked_pdf(Path(temp),content),['RIGHT'])
                self.assertEqual(result['status'],'UNCERTAIN',result)
                self.assertIn('marked_content_unbalanced',result['issues'])

    def test_actualtext_cannot_certify_an_invisible_run(self):
        with tempfile.TemporaryDirectory() as temp:
            path=self.marked_pdf(Path(temp),b'/Span << /ActualText (VISIBLE) >> BDC 3 Tr (wrong) Tj EMC')
            result=inspect_pdf_literals(path,['VISIBLE'])
            self.assertEqual(result['status'],'UNCERTAIN',result)
            self.assertIn('actualtext_visibility_unconfirmed',result['issues'])

    def test_actualtext_budget_and_nested_depth_are_bounded(self):
        variants=(b'/Span << /ActualText ('+b'A'*65537+b') >> BDC (RIGHT) Tj EMC',
                  b'/Span BMC '*33+b'(RIGHT) Tj'+b' EMC'*33)
        for content in variants:
            with self.subTest(size=len(content)),tempfile.TemporaryDirectory() as temp:
                result=inspect_pdf_literals(self.marked_pdf(Path(temp),content),['RIGHT'])
                self.assertEqual(result['status'],'UNCERTAIN',result)


if __name__ == '__main__':
    unittest.main()
