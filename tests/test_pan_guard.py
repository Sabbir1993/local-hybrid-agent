"""tests/test_pan_guard.py - built-in card-number (PAN) guard for chat/agent.

Run: python -m unittest tests.test_pan_guard -v
Test numbers are the public issuer test PANs, not real cards.
"""

import asyncio
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

    def test_attachment_file_name_is_not_scanned(self):
        name = "photo_6210835229476328575_y.jpg"      # Luhn-valid by chance
        self.assertTrue(pan.contains_pan(name))
        body = f"hi\n--- IMAGE: {name} ---\na plant\n--- END {name} ---"
        self.assertFalse(pan.contains_pan(pan.blank_attachment_names(body)))
        leak = f"--- FILE: a.txt ---\ncard {VISA_TEST}\n--- END a.txt ---"   # content still scanned
        self.assertTrue(pan.contains_pan(pan.blank_attachment_names(leak)))
        self.assertTrue(pan.contains_pan(pan.blank_attachment_names(f"pay {VISA_TEST}")))

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


class KbIngestPanTests(unittest.TestCase):
    """Knowledge-base ingestion used to carry reject_if_pan(), a `pass` stub, behind a
    docstring that claimed ingestion "fails closed". helpers.py caught the ValueError it
    could never raise, so the call site looked like a live control while masking nothing --
    card numbers went straight into the shared vector index, from where they could be
    retrieved into a prompt and sent to a cloud lane.

    It is masked now, before chunking, so a document that legitimately discusses card
    handling stays ingestible and no PAN ever reaches the index.
    """

    def _run(self, text, empty=False):
        from unittest import mock
        from routes.knowledge import helpers
        seen = {}

        async def fake_index(source_id, txt):
            seen["text"] = txt
            return 3

        with mock.patch.object(helpers, "index_knowledge_source", fake_index), \
             mock.patch.object(helpers.auth_db, "update_knowledge_source_status"), \
             mock.patch.object(helpers, "audit_log") as audit:
            out = asyncio.run(helpers._finish_ingest(7, text, mock.Mock()))
        return out, seen.get("text"), audit

    def test_card_numbers_are_masked_before_chunking(self):
        out, indexed, audit = self._run(f"Customer {VISA_TEST} holds an account.")
        self.assertTrue(out["ok"])
        self.assertEqual(indexed, "Customer [card ****1111] holds an account.")
        self.assertNotIn("4111 1111", indexed)          # never enters the vector index
        self.assertEqual(out["pans_masked"], 1)

    def test_masking_is_audited(self):
        out, _indexed, audit = self._run(f"card {MC_TEST} on file")
        detail = audit.call_args.kwargs["detail"]
        self.assertEqual(detail["pans_masked"], 1)
        self.assertEqual(detail["chunks"], 3)

    def test_a_clean_document_is_untouched(self):
        out, indexed, _audit = self._run("Refund policy: 30 days from delivery.")
        self.assertEqual(indexed, "Refund policy: 30 days from delivery.")
        self.assertEqual(out["pans_masked"], 0)

    def test_ingest_is_not_rejected(self):
        # rejecting would make a document *about* card handling impossible to upload,
        # which is why masking replaced rejection rather than implementing it
        out, indexed, _audit = self._run(f"PCI guidance: never store {VISA_TEST} unencrypted.")
        self.assertTrue(out["ok"], out)
        self.assertIn("[card ****1111]", indexed)

    def test_empty_text_is_still_reported_as_empty(self):
        out, _indexed, _audit = self._run("   \n  ")
        self.assertFalse(out["ok"])
        self.assertIn("no extractable text", out["error"])

    def test_ingest_module_has_no_dead_pan_code(self):
        # it used to keep a second, weaker PAN detector (_PAN_CANDIDATE/_luhn_ok/
        # find_pan_like) that nothing called; core/pan.py is now the only detector
        import core.knowledge_ingest as ki
        for name in ("reject_if_pan", "find_pan_like", "_PAN_CANDIDATE", "_luhn_ok"):
            self.assertFalse(hasattr(ki, name), f"{name} must not come back")

    def test_pan_module_docstring_matches_behaviour(self):
        import core.pan as p
        doc = p.__doc__
        self.assertIn("KB ingest", doc)
        self.assertNotIn("deliberately NOT scanned", doc)


if __name__ == "__main__":
    unittest.main()
