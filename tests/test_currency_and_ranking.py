"""BDT conversion of foreign amounts (core/currency.py + output_guard) and the weighted web-search merge."""
import unittest
from unittest import mock

from core import currency, output_guard
import importlib

rerank = importlib.import_module("core.web_search.rerank")

RATES = {"ts": 9e12, "date": "2026-11-03", "rates": {"USD": 1.0, "BDT": 122.0, "INR": 83.0, "EUR": 0.9}}


class CurrencyTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(currency._mem, RATES, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_real_rate_not_symbol_swap(self):
        out, _ = currency.convert_text("It costs $100 today.")
        self.assertIn("৳12,200 (≈ $100)", out)
        self.assertNotIn("৳100", out)

    def test_other_currencies_and_scales(self):
        out, _ = currency.convert_text("₹5,000 and USD 2.5 million and 15 EUR")
        self.assertIn("৳7,349 (≈ ₹5,000)", out)
        self.assertIn("৳30.5 crore (≈ USD 2.5 million)", out)
        self.assertIn("৳2,033", out)

    def test_taka_and_code_left_alone_and_idempotent(self):
        text = "Fee Tk 500 or ৳1,200.\n```\nprice = $5\n```\n`$7`"
        self.assertEqual(currency.convert_text(text)[0], text)
        once, _ = currency.convert_text("$100")
        self.assertEqual(currency.convert_text(once)[0], once)

    def test_unknown_rate_is_not_guessed(self):
        self.assertEqual(currency.convert_text("CAD 40")[0], "CAD 40")

    def test_override_beats_live_rate(self):
        with mock.patch.object(currency, "cfg", return_value={"overrides": {"USD": 125.0}}):
            self.assertIn("৳12,500", currency.convert_text("$100")[0])

    def test_lakh_crore_grouping(self):
        self.assertEqual(currency.format_bdt(1250000), "৳12,50,000")
        self.assertEqual(currency.format_bdt(610000000), "৳61 crore")

    def test_streaming_matches_one_shot_and_is_opt_in(self):
        text = "Total $100 or ₹5,000 for the plan, paid in Tk 500."
        red = output_guard.OutputRedactor(None, False, fx=True)
        got = "".join(red.feed(text[i:i + 3]) for i in range(0, len(text), 3)) + red.flush()
        self.assertEqual(got, currency.convert_text(text)[0])
        plain = output_guard.OutputRedactor(None, False)
        self.assertEqual(plain.feed("pay $100 now") + plain.flush(), "pay $100 now")


class RankingTests(unittest.TestCase):
    def test_engine_weight_and_agreement(self):
        a = {"url": "https://a.example/1", "title": "a", "snippet": "", "src": ["google", "brave", "bing"]}
        b = {"url": "https://b.example/1", "title": "b", "snippet": ""}
        ranked = rerank.merge({"duckduckgo#0": [b, a]})
        self.assertEqual(ranked[0]["url"], a["url"])         # rank 2 but three engines agree
        sx = rerank.merge({"searxng#0": [b], "duckduckgo#0": [dict(b)]})
        self.assertGreater(sx[0]["rrf"], 2 / 60)             # searxng weighted above 1.0

    def test_authority_bonus(self):
        items = [{"url": "https://spam.example/x", "title": "", "snippet": "", "score": 0.5},
                 {"url": "https://en.wikipedia.org/wiki/X", "title": "", "snippet": "", "score": 0.45}]
        out = rerank.apply_domain_prefs(items)
        self.assertTrue(out[0]["url"].startswith("https://en.wikipedia.org"))


if __name__ == "__main__":
    unittest.main()
