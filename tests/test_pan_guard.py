"""tests/test_pan_guard.py - built-in card-number (PAN) guard for chat/agent.

Run: python -m unittest tests.test_pan_guard -v
Test numbers are the public issuer test PANs, not real cards.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import pan, input_guard, output_guard
from core.small_model import APP_CONFIG

VISA_TEST = "4111 1111 1111 1111"
MC_TEST = "5555-5555-5555-4444"


class PanDetectTests(unittest.TestCase):
    def test_detects_grouped_and_plain(self):
        self.assertTrue(pan.contains_pan(f"my card is {VISA_TEST} ok"))
        self.assertTrue(pan.contains_pan(MC_TEST))
        self.assertTrue(pan.contains_pan("378282246310005"))   # Amex test

    def test_ignores_non_pans(self):
        self.assertFalse(pan.contains_pan("4111 1111 1111 1112"))       # bad Luhn
        self.assertFalse(pan.contains_pan("+8801712345678"))            # BD mobile
        self.assertFalse(pan.contains_pan("txn 9000000000000000008"))   # IIN 9
        self.assertFalse(pan.contains_pan("4444444444444444"))          # repeated digit
        self.assertFalse(pan.contains_pan("order 12345"))

    def test_mask_keeps_last_four(self):
        out, n = pan.mask_pans(f"a {VISA_TEST} b {MC_TEST}")
        self.assertEqual(n, 2)
        self.assertEqual(out, "a [card ****1111] b [card ****4444]")


class PanGuardTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: APP_CONFIG.get(k) for k in ("pci", "input_guard", "output_guard")}
        APP_CONFIG.pop("pci", None)
        APP_CONFIG["input_guard"] = {"enabled": False, "rules": []}
        APP_CONFIG["output_guard"] = {"enabled": False, "rules": []}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                APP_CONFIG.pop(k, None)
            else:
                APP_CONFIG[k] = v

    def test_input_blocked_even_with_guard_disabled(self):
        hit = input_guard.check([f"charge {VISA_TEST}"], None, any_cloud_lane=False)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["_matched_pattern"], "builtin:pan")
        self.assertNotIn("4111", str(hit))      # the PAN never lands in audit detail

    def test_input_off_switch(self):
        APP_CONFIG["pci"] = {"pan_input": "off"}
        self.assertIsNone(input_guard.check([VISA_TEST], None, any_cloud_lane=True))

    def test_output_stream_masked_across_chunks(self):
        red = output_guard.OutputRedactor(None, any_cloud_lane=False)
        text = f"Your card {VISA_TEST} is on file."
        out = "".join(red.feed(text[i:i + 3]) for i in range(0, len(text), 3)) + red.flush()
        self.assertEqual(out, "Your card [card ****1111] is on file.")

    def test_plain_text_not_held_back(self):
        red = output_guard.OutputRedactor(None, any_cloud_lane=False)
        self.assertEqual(red.feed("Hello, how can I help?"), "Hello, how can I help?")
        self.assertEqual(red.feed(" Order 12"), " Order")      # only the digit run waits

    def test_output_full_masked(self):
        out, _ = output_guard.redact_full(f"pan={MC_TEST}", None, False)
        self.assertEqual(out, "pan=[card ****4444]")

    def test_cloud_egress_masks_copy_only(self):
        from core.cloud import CloudClient, CloudModel
        cm = CloudModel("p", {"options": {"baseURL": "https://example.invalid/v1"}}, "m", {})
        msgs = [{"role": "tool", "content": f"row: {VISA_TEST}"},
                {"role": "user", "content": [{"type": "text", "text": MC_TEST}]}]
        sent = CloudClient(cm)._prepare({"messages": msgs})
        self.assertEqual(sent["messages"][0]["content"], "row: [card ****1111]")
        self.assertEqual(sent["messages"][1]["content"][0]["text"], "[card ****4444]")
        self.assertEqual(msgs[0]["content"], f"row: {VISA_TEST}")   # caller untouched


if __name__ == "__main__":
    unittest.main()
