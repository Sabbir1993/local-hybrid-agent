import unittest

from core.doc_ops.pptx_ops.outline import _FOOTER_META


class FooterMetaTest(unittest.TestCase):
    def test_footer_lines_match(self):
        for s in ["total slides 12", "Total Slides: 12", "estimated time 5 min",
                  "estimated presentation time 5 min", "end of the deck", "format: markdown", "(end)"]:
            with self.subTest(s=s):
                self.assertIsNotNone(_FOOTER_META.match(s))

    def test_content_lines_do_not_match(self):
        for s in ["Total slidesmanship", "Our total revenue"]:
            with self.subTest(s=s):
                self.assertIsNone(_FOOTER_META.match(s))


if __name__ == "__main__":
    unittest.main()
