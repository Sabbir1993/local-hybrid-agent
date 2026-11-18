import unittest

from core import image_proxy


class ImageProxyAgent(unittest.TestCase):
    def test_wikimedia_gets_a_descriptive_agent_others_the_generic_one(self):
        # Wikimedia answers 403 to generic agents: pictures from a search went blank in the chat gallery
        self.assertEqual(image_proxy._ua_for("https://upload.wikimedia.org/a.jpg"), image_proxy._WIKI_UA)
        self.assertEqual(image_proxy._ua_for("https://thumb.wikimedia.org/a.jpg"), image_proxy._WIKI_UA)
        self.assertEqual(image_proxy._ua_for("https://tse1.mm.bing.net/th?id=x"), image_proxy._UA)

    def test_a_big_original_is_shrunk_to_fit(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (3000, 2000), (10, 120, 200)).save(buf, "PNG")
        ctype, data = image_proxy._shrink(buf.getvalue(), "image/png")
        self.assertEqual(ctype, "image/jpeg")
        self.assertLess(len(data), len(buf.getvalue()))


if __name__ == "__main__":
    unittest.main()
