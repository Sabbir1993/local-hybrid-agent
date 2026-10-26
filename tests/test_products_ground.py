"""tests/test_products_ground.py - product cards keep only links and pictures that a search really returned.

Run: python -m unittest tests.test_products_ground -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import products_ground as pg

REAL = "https://www.startech.com.bd/tefal-everyday-slim-ih-2200w-induction-cooker"
IMG = "https://www.startech.com.bd/image/cache/catalog/tefal-1200x630.webp"


class GroundTests(unittest.TestCase):
    def setUp(self):
        pg.reset()
        pg.note_result(1, REAL, "Tefal EVERYDAY SLIM IH 2200W Induction Cooker Price in BD",
                       {"image": IMG, "price": "11900.0000", "currency": "BDT"})
        pg.note_result(1, "https://rangsemart.com.bd/product-detail/tefal-induction-cooktop-everyday-slim-ih-2200w",
                       "Tefal Induction Cooktop Everyday Slim Ih 2200w")

    def block(self, items):
        return "intro\n```products\n" + json.dumps(items) + "\n```\nafter"

    def cards(self, text):
        return json.loads(text.split("```products\n")[1].split("\n```")[0])

    def test_invented_link_and_picture_are_replaced_by_the_real_ones(self):
        out, changed = pg.ground_text(1, self.block([{
            "title": "Tefal EVERYDAY SLIM AH250180 2200W", "store": "Star Tech", "price": "9,000",
            "url": "https://www.startech.com.bd/product/tefal-everyday-slim-i2000w",
            "image": "https://www.startech.com.bd/images/thumbnail/1200x1200/products/x.jpg"}]))
        c = self.cards(out)[0]
        self.assertTrue(changed)
        self.assertEqual((c["url"], c["image"], c["price"]), (REAL, IMG, "9,000"))

    def test_card_that_matches_nothing_loses_link_and_picture_but_keeps_text(self):
        out, _ = pg.ground_text(1, self.block([{"title": "Gazi Smiss Cooker", "url": "https://nowhere.example/x",
                                                "image": "https://nowhere.example/x.jpg", "price": "4,487"}]))
        c = self.cards(out)[0]
        self.assertEqual((c["url"], c["image"], c["title"], c["price"]), ("", "", "Gazi Smiss Cooker", "4,487"))

    def test_other_users_results_are_not_used(self):
        out, _ = pg.ground_text(2, self.block([{"title": "Tefal EVERYDAY SLIM IH 2200W", "url": REAL}]))
        self.assertEqual(self.cards(out)[0]["url"], "")

    def test_text_without_a_block_and_invalid_blocks_are_untouched(self):
        self.assertEqual(pg.ground_text(1, "plain"), ("plain", False))
        bad = "```products\nnot json at all\n```"
        self.assertEqual(pg.ground_text(1, bad), (bad, False))


if __name__ == "__main__":
    unittest.main()
