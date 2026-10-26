"""tests/test_product_meta.py - the picture / price a search-result page declares about itself.

Run: python -m unittest tests.test_product_meta -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.web_search.extraction import extract_product_meta

OG = """<html><head><title>x</title>
<meta property="og:image" content="/img/a.jpg">
<meta property="product:price:amount" content="9870">
<meta property="product:price:currency" content="BDT">
<meta property="og:title" content="Tefal Everyday Slim"></head><body></body></html>"""

LD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Philips HD4938","image":["https://cdn.example/p.png"],
 "offers":{"@type":"Offer","price":"10200","priceCurrency":"BDT"}}</script></head><body></body></html>"""


class ProductMetaTests(unittest.TestCase):
    def test_open_graph_image_is_made_absolute_and_price_read(self):
        m = extract_product_meta(OG, "https://shop.example/p/1")
        self.assertEqual(m["image"], "https://shop.example/img/a.jpg")
        self.assertEqual((m["price"], m["currency"]), ("9870", "BDT"))

    def test_json_ld_product(self):
        m = extract_product_meta(LD, "https://shop.example/p/2")
        self.assertEqual((m["image"], m["price"], m["name"]), ("https://cdn.example/p.png", "10200", "Philips HD4938"))

    def test_http_images_are_dropped(self):
        m = extract_product_meta('<meta property="og:image" content="http://shop.example/a.jpg">', "https://shop.example/")
        self.assertNotIn("image", m)

    def test_plain_page_gives_nothing(self):
        self.assertEqual(extract_product_meta("<html><body><p>hi</p></body></html>", "https://a.example/"), {})


if __name__ == "__main__":
    unittest.main()


class ProductImageResolveTests(unittest.TestCase):
    """core/product_image.py: only addresses a web tool returned for this user are fetched."""

    def setUp(self):
        from core import image_proxy, product_image
        from core.web_search import citations
        self.ip, self.pi, self.cit = image_proxy, product_image, citations
        image_proxy._allowed.clear()
        image_proxy._cache.clear()
        product_image._page_image.clear()
        citations.reset()

    def test_model_image_that_was_never_returned_is_refused(self):
        with self.assertRaises(self.ip.ProxyRefused):
            self.pi.resolve(1, "https://shop.example/p", "https://evil.example/a.png")

    def test_page_picture_is_used_when_the_page_was_a_search_result(self):
        from unittest import mock
        self.cit.remember(1, ["https://shop.example/p"])
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        page = mock.Mock(status_code=200, content=OG.encode(), encoding="utf-8", url="https://shop.example/p")
        img = mock.Mock(status_code=200, content=png, truncated=False)
        with mock.patch("core.web_search.extraction.fetch_page", return_value=page), \
             mock.patch("core.net_guard.guarded_get", return_value=img):
            ctype, data = self.pi.resolve(1, "https://shop.example/p", "")
        self.assertEqual((ctype, data), ("image/png", png))

    def test_unknown_page_is_not_fetched(self):
        from unittest import mock
        with mock.patch("core.web_search.extraction.fetch_page", side_effect=AssertionError("must not fetch")):
            with self.assertRaises(self.ip.ProxyRefused):
                self.pi.resolve(1, "https://shop.example/other", "")
