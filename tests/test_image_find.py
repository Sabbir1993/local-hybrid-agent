import unittest

from core import prompt_scope as ps


class ImageFindVsMake(unittest.TestCase):
    def test_find(self):
        for q in ("I need image of cricket bat can you share", "Pokagulur photo dao",
                  "show me a picture of Dhaka", "image of Messi"):
            self.assertTrue(ps.image_find_wanted(q), q)

    def test_make_or_other(self):
        for q in ("generate an image of a cricket bat", "draw a cricket bat", "make it darker", "create a logo",
                  "write a prompt for an image of a cat", "photo of the attached file"):
            self.assertFalse(ps.image_find_wanted(q), q)


if __name__ == "__main__":
    unittest.main()
