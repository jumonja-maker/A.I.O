"""QA oracle must reject visually wrong pages even with plausible metadata."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'aio_calc_page_test_oracle', Path(__file__).resolve().parents[1] / 'tools/qa/calc_pages_native.py')
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


class CalcPageContentOracleTests(unittest.TestCase):
    def counts(self, *colours):
        return qa._marker_counts([colour for colour in colours for _ in range(20)])

    def test_exact_and_jpeg_perturbed_bands_pass(self):
        for red, blue, gold in (((255,64,64),(64,64,255),(255,200,64)),
                                ((240,80,76),(80,75,240),(240,185,77))):
            qa._verify_markers(self.counts(red,blue,gold), {'red','blue','gold'})

    def test_repeated_first_page_with_new_filename_is_rejected(self):
        with self.assertRaises(AssertionError):
            qa._verify_markers(self.counts((255,64,64)), {'blue'})

    def test_empty_page_cannot_pass(self):
        with self.assertRaises(AssertionError):
            qa._verify_markers(self.counts((255,255,255),(0,0,0)), {'red'})

    def test_missing_lower_content_at_half_scale_is_rejected(self):
        with self.assertRaises(AssertionError):
            qa._verify_markers(self.counts((255,64,64),(64,64,255)), {'red','blue','gold'})

    def test_hidden_and_outside_print_area_regions_are_rejected(self):
        for colour in ((255,64,255),(64,255,255)):
            with self.subTest(colour=colour), self.assertRaises(AssertionError):
                qa._verify_markers(self.counts((64,255,64),colour), {'green'})

    def test_small_colour_noise_does_not_stand_for_content(self):
        counts=qa._marker_counts([(255,64,64)]*9)
        with self.assertRaises(AssertionError):
            qa._verify_markers(counts, {'red'})


if __name__ == '__main__':
    unittest.main()
