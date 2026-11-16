import unittest
from unittest import mock

from core import input_guard, output_guard, pan


class PhoneOutputMask(unittest.TestCase):
    def _redact(self, text, cfg=None):
        with mock.patch.object(pan, "_cfg", return_value=cfg or {}), \
             mock.patch.object(input_guard, "rules_for", return_value=[]):
            return output_guard.redact_full(text, None, False)[0]

    def test_bd_mobile_forms_are_masked(self):
        for s in ("01712345678", "01712-345678", "+8801912345678", "8801512345678"):
            out = self._redact(f"call {s} now")
            self.assertNotIn(s, out, out)
            self.assertIn("[phone ****5678]" if s.endswith("5678") else "[phone ****", out)

    def test_card_still_masked_and_phone_does_not_eat_it(self):
        out = self._redact("card 4111 1111 1111 1111 and 01712345678")
        self.assertIn("[card ****1111]", out)
        self.assertIn("[phone ****5678]", out)

    def test_off_switch_leaves_phone_alone(self):
        out = self._redact("call 01712345678", {"phone_output": "off"})
        self.assertIn("01712345678", out)

    def test_non_phone_numbers_are_not_masked(self):
        for s in ("order 12345678901", "amount 1,200,000", "2026-11-16"):
            self.assertEqual(self._redact(s), s)


if __name__ == "__main__":
    unittest.main()
