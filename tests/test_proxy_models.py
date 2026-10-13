"""The raw /v1/models listing shows plain model names, not the server's file paths."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes.proxy.constants import short_model_name, short_models_listing  # noqa: E402

WIN = "E:" + chr(92) + "AI" + chr(92) + "Models" + chr(92) + "Qwen3.6-35B-A3B" + chr(92) + "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"


class ShortModelNameTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(short_model_name(WIN), "Qwen3.6-35B-A3B-UD-Q4_K_M")
        self.assertEqual(short_model_name("/home/u/models/x/Llama-3.1-8B.Q4.gguf"), "Llama-3.1-8B.Q4")
        self.assertEqual(short_model_name("already-plain"), "already-plain")
        self.assertEqual(short_model_name(None), "")

    def test_listing_is_rewritten_in_both_arrays(self):
        data = {"models": [{"name": WIN, "model": WIN, "details": {"format": "gguf"}}], "object": "list",
                "data": [{"id": WIN, "aliases": [WIN], "object": "model", "meta": {"n_ctx": 4096}}]}
        out = short_models_listing(data)
        self.assertEqual(out["models"][0]["name"], "Qwen3.6-35B-A3B-UD-Q4_K_M")
        self.assertEqual(out["models"][0]["model"], "Qwen3.6-35B-A3B-UD-Q4_K_M")
        self.assertEqual(out["data"][0]["id"], "Qwen3.6-35B-A3B-UD-Q4_K_M")
        self.assertEqual(out["data"][0]["aliases"], ["Qwen3.6-35B-A3B-UD-Q4_K_M"])
        self.assertEqual(out["data"][0]["meta"], {"n_ctx": 4096})
        self.assertNotIn("Models", str(out), "no server folder names leak")

    def test_odd_input_passes_through(self):
        self.assertEqual(short_models_listing({"error": "x"}), {"error": "x"})
        self.assertEqual(short_models_listing([1]), [1])


if __name__ == "__main__":
    unittest.main()
