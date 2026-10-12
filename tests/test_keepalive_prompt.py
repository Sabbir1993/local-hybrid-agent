import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import state as st


class KeepalivePrompt(unittest.TestCase):
    def test_a_dense_model_gets_the_short_ping(self):
        with mock.patch("core.vram.gguf_parser.parse_gguf_info", return_value={"n_expert": 0, "n_layer": 32}):
            self.assertEqual(st.keepalive_prompt({"model_path": "m.gguf"}), "ping")

    def test_a_moe_model_gets_a_prompt_that_routes_through_many_experts(self):
        with mock.patch("core.vram.gguf_parser.parse_gguf_info", return_value={"n_expert": 256, "n_layer": 40}):
            self.assertGreater(len(st.keepalive_prompt({"model_path": "m.gguf"})), 300)

    def test_no_profile_falls_back_to_the_short_ping(self):
        self.assertEqual(st.keepalive_prompt(None), "ping")


if __name__ == "__main__":
    unittest.main()
