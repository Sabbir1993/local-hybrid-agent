"""tests/test_pan_v2.py - Bangladesh-first card number detector (core/pan.py detector v2).

Run: python -m unittest tests.test_pan_v2 -v
Every card number here is generated from a public issuer prefix plus a computed Luhn check
digit; none is a real card.
"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import pan, output_guard
from core.small_model import APP_CONFIG

BN = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


def mk(prefix: str, length: int, seed: int = 7) -> str:
    """Luhn-valid number: prefix + deterministic filler + check digit."""
    rnd = random.Random(seed)
    body = prefix + "".join(str(rnd.randint(0, 9)) for _ in range(length - len(prefix) - 1))
    for c in "0123456789":
        if pan._luhn_ok(body + c):
            return body + c
    raise AssertionError


def grouped(n: str, sep=" ", size=4) -> str:
    return sep.join(n[i:i + size] for i in range(0, len(n), size))


VISA = mk("4", 16)
MC = mk("5", 16, 3)
AMEX = mk("37", 15)
UNION = mk("62", 16)
UNION19 = mk("62", 19)
RUPAY = mk("81", 16)
DINERS = mk("36", 14)
JCB = mk("35", 16)


class PanV2Detect(unittest.TestCase):
    def hit(self, text):
        self.assertTrue(pan.contains_pan(text), text)
        masked, n = pan.mask_pans(text)
        self.assertGreaterEqual(n, 1, text)
        self.assertFalse(pan.contains_pan(masked), masked)

    def miss(self, text):
        self.assertFalse(pan.contains_pan(text), text)
        self.assertEqual(pan.mask_pans(text), (text, 0))

    def test_schemes(self):
        for n in (VISA, MC, AMEX, UNION, UNION19, RUPAY, DINERS, JCB):
            self.hit(f"my card {n} thanks")

    def test_separators(self):
        for sep in (" ", "-", ".", "\n", "—", "−", " ", " ", " ", " - ", "  ", "/", "_", ", ", "**"):
            self.hit(grouped(VISA, sep))
        self.hit(f"{VISA[:4]}​{VISA[4:8]}​{VISA[8:12]}​{VISA[12:]}")
        self.hit(f"{AMEX[:4]} {AMEX[4:10]} {AMEX[10:]}")                       # 4-6-5

    def test_cvv_or_quantity_next_to_pan(self):
        self.hit(f"{VISA} 123")
        self.hit(f"{grouped(VISA)} 123")
        self.hit(f"5 {VISA}")
        self.hit(f"{VISA}\n123")

    def test_bangla_digits(self):
        self.hit(f"কার্ড নম্বর {VISA.translate(BN)}")
        self.hit(grouped(VISA).translate(BN))
        masked, _ = pan.mask_pans(f"{VISA.translate(BN)}")
        self.assertIn("****" + VISA[-4:], masked)
        self.hit("".join(c for c in VISA).replace("4", "４"))              # fullwidth

    def test_superscript_and_circled(self):
        sup = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
        self.hit(grouped(VISA).translate(sup))

    def test_masks_only_the_number(self):
        masked, n = pan.mask_pans(f"order 5 {grouped(VISA)} 123 end")
        self.assertEqual(n, 1)
        self.assertIn("[card ****" + VISA[-4:] + "]", masked)
        self.assertTrue(masked.startswith("order 5 "))
        self.assertTrue(masked.endswith(" 123 end"))

    def test_two_cards(self):
        masked, n = pan.mask_pans(f"{VISA} and {MC}")
        self.assertEqual(n, 2)

    def test_context_mode_catches_mistyped(self):
        bad = VISA[:-1] + str((int(VISA[-1]) + 1) % 10)               # fails Luhn
        self.miss(f"reference {bad}")
        self.hit(f"my credit card number is {bad}")
        self.hit(f"কার্ড নং {bad}")

    def test_nid_card_is_not_a_payment_card(self):
        nid13 = "4512345678901"
        self.miss(f"NID card {nid13}")
        self.miss(f"SIM card 8801712345678")


class PanV2NegativeCorpus(unittest.TestCase):
    """Bangladesh identifiers and other numbers that must NOT be flagged."""

    CASES = [
        "+8801712345678", "01712345678", "+880 1712-345678", "০১৭১২৩৪৫৬৭৮",       # mobiles / wallets
        "bKash TrxID 8N7A6D5EE7", "Nagad txn 70KJ8X2P91",                         # alphanumeric TrxIDs
        "NID 1990123456789", "NID 19901234567890123", "2002123456789", "জাতীয় পরিচয়পত্র ১৯৯০১২৩৪৫৬৭৮৯",
        "routing 090261725", "A/C 1234567890123", "account 20501234567890",
        "timestamp 20261018123045", "epoch 1760000000000", "1760000000000000",
        "amount 4,123,456,789,012,345 BDT", "BDT 12,345,678,901,234,567",
        "invoice 2026-10-18 10:30:45 id 123456", "tel 02-9876543 02-1234567",
        "ISBN 978-3-16-148410-0", "version 1.2.3.4.5.6.7.8.9.0.1.2.3",
        "1111111111111111", "0000000000000000",
    ]

    def test_none_flagged(self):
        for t in self.CASES:
            with self.subTest(t=t):
                self.assertFalse(pan.contains_pan(t), t)
                self.assertEqual(pan.mask_pans(t), (t, 0))

    def test_random_sixteen_digit_ids_low_false_positive_rate(self):
        rnd = random.Random(1)
        fp = 0
        n = 5000
        for _ in range(n):
            d = "".join(rnd.choice("0123456789") for _ in range(16))
            if d[0] in "7" or d[:2] in ("00", "10", "11", "12"):
                pass
            if pan.contains_pan(f"id {d}"):
                fp += 1
        # IIN-valid + Luhn-valid is ~6% of random 16-digit strings (prefix 4/5/6 ~ 30% * Luhn 10% + others);
        # the bound only guards against the detector regressing to "everything looks like a card"
        self.assertLess(fp / n, 0.10)


class PanV2Cvv(unittest.TestCase):
    def test_cvv_masked(self):
        for t in ("cvv 123", "CVV: 123", "cvc2=4567", "CVV is 123", "সিভিভি ১২৩", "cvv number 123"):
            with self.subTest(t=t):
                self.assertTrue(pan.contains_pan(t), t)
                masked, n = pan.mask_pans(t)
                self.assertEqual(n, 1)
                self.assertIn("***", masked)

    def test_not_cvv(self):
        for t in ("cvv", "the cvv is on the back", "cvv 12", "cvv 12345"):
            self.assertFalse(pan.contains_pan(t), t)


class PanV2Rollback(unittest.TestCase):
    def test_v1_switch(self):
        old = APP_CONFIG.get("pci")
        try:
            APP_CONFIG["pci"] = {"detector": "v1"}
            self.assertTrue(pan.contains_pan(VISA))
            self.assertFalse(pan.contains_pan(f"{VISA} 123"))   # the documented v1 gap
        finally:
            if old is None:
                APP_CONFIG.pop("pci", None)
            else:
                APP_CONFIG["pci"] = old


class PanV2Streaming(unittest.TestCase):
    def _stream(self, text, size):
        r = output_guard.OutputRedactor(None, False)
        out = []
        for i in range(0, len(text), size):
            out.append(r.feed(text[i:i + size]))
        out.append(r.flush())
        return "".join(out)

    def test_split_pan_never_leaks(self):
        samples = [
            f"card: {grouped(VISA)} thanks",
            f"card: {grouped(VISA, '.')} thanks",
            f"card: {grouped(VISA, chr(0xa0))} thanks",
            f"card: {grouped(VISA, chr(0x2013))} thanks",
            f"card: {grouped(VISA, ' - ')} thanks",
            f"card: {grouped(VISA)} 123 thanks",
            f"কার্ড: {grouped(VISA).translate(BN)} ধন্যবাদ",
        ]
        for s in samples:
            for size in (1, 2, 3, 5, 7):
                with self.subTest(s=s, size=size):
                    out = self._stream(s, size)
                    self.assertFalse(pan.contains_pan(out), out)
                    self.assertNotIn(VISA[:12], out.replace(" ", "").replace("-", ""))

    def test_streamed_cvv_masked(self):
        for size in (1, 2, 3):
            out = self._stream("the cvv is 123 ok", size)
            self.assertNotIn("123", out)

    def test_plain_text_passes_through(self):
        out = self._stream("Hello, the total is 1,234 BDT. Call 01712345678.", 3)
        self.assertEqual(out, "Hello, the total is 1,234 BDT. Call 01712345678.")


class PanV2Property(unittest.TestCase):
    def test_random_luhn_numbers_with_noise_always_masked(self):
        rnd = random.Random(42)
        seps = [" ", "-", ".", "\n", "—", " - ", " ", "  ", "_", "/"]
        for k in range(400):
            prefix = rnd.choice(["4", "51", "55", "37", "62", "81", "35", "36", "2221", "2500"])
            length = {"37": 15, "36": 14}.get(prefix, rnd.choice([16, 16, 16, 19] if prefix in ("4", "62") else [16]))
            n = mk(prefix, length, k)
            sep = rnd.choice(seps)
            if prefix == "37":
                body = f"{n[:4]}{sep}{n[4:10]}{sep}{n[10:]}"
            elif prefix == "36":                                     # Diners prints 4-6-4
                body = f"{n[:4]}{sep}{n[4:10]}{sep}{n[10:]}"
            else:
                body = grouped(n, sep)
            pre = rnd.choice(["", "card ", "pay with ", "কার্ড ", "x" if False else ""])
            post = rnd.choice(["", " 123", " exp 12/28", "."])
            text = f"{pre}{body}{post}"
            if rnd.random() < 0.3:
                text = text.translate(BN)
            with self.subTest(text=text):
                self.assertTrue(pan.contains_pan(text), text)
                self.assertFalse(pan.contains_pan(pan.mask_pans(text)[0]), text)


if __name__ == "__main__":
    unittest.main()
