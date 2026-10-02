"""tests/test_image_tools.py - Unit tests for image optimization and multimodal analysis."""

import asyncio
import io
import unittest
from unittest import mock
from PIL import Image

from core.agent_tools.search_tools import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_DIM,
    _image_analysis_cache,
    optimize_image_bytes,
    tool_analyze_image,
)


class TestImageTools(unittest.TestCase):
    def setUp(self):
        _image_analysis_cache.clear()

    def _make_dummy_image(self, width: int, height: int, fmt: str = "PNG", mode: str = "RGB") -> bytes:
        img = Image.new(mode, (width, height), color=(100, 150, 200))
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        return buf.getvalue()

    def test_optimize_small_image_not_scaled(self):
        raw = self._make_dummy_image(400, 300, fmt="PNG")
        opt, meta = optimize_image_bytes(raw, max_dim=1536)
        self.assertFalse(meta["scaled"])
        self.assertEqual(meta["w"], 400)
        self.assertEqual(meta["h"], 300)
        self.assertEqual(opt, raw)

    def test_optimize_oversized_image_scaled_proportionally(self):
        raw = self._make_dummy_image(3000, 1500, fmt="PNG")
        opt, meta = optimize_image_bytes(raw, max_dim=1500)
        self.assertTrue(meta["scaled"])
        self.assertEqual(meta["orig_w"], 3000)
        self.assertEqual(meta["orig_h"], 1500)
        self.assertEqual(meta["w"], 1500)
        self.assertEqual(meta["h"], 750)
        self.assertLess(len(opt), len(raw))

    def test_optimize_rgba_jpeg_converted_safely(self):
        raw = self._make_dummy_image(2000, 2000, fmt="PNG", mode="RGBA")
        opt, meta = optimize_image_bytes(raw, max_dim=1000)
        self.assertTrue(meta["scaled"])
        self.assertEqual(meta["w"], 1000)
        self.assertEqual(meta["h"], 1000)

    def test_optimize_corrupt_bytes_graceful_fallback(self):
        corrupt = b"not_an_image_bytes_12345"
        opt, meta = optimize_image_bytes(corrupt, max_dim=1000)
        self.assertFalse(meta["scaled"])
        self.assertEqual(opt, corrupt)

    def test_tool_analyze_image_caching(self):
        raw = self._make_dummy_image(200, 200)
        import base64
        b64_data = base64.b64encode(raw).decode()

        with mock.patch("core.agent_tools.search_tools._ws_resolve", return_value="dummy/test.png"), \
             mock.patch("core.agent_tools.search_tools._remote_uid", return_value="user1"), \
             mock.patch("core.companion_bridge.call", return_value={"data": b64_data}), \
             mock.patch("core.agent_tools.search_tools.describe_image_bytes", return_value="A blue box") as mock_desc:

            res1 = asyncio.run(tool_analyze_image({"path": "test.png", "question": "What is this?"}))
            self.assertEqual(res1, "A blue box")
            self.assertEqual(mock_desc.call_count, 1)

            # Second call with same image and question should hit cache without invoking describe_image_bytes
            res2 = asyncio.run(tool_analyze_image({"path": "test.png", "question": "What is this?"}))
            self.assertEqual(res2, "A blue box")
            self.assertEqual(mock_desc.call_count, 1)

    def test_tool_analyze_image_too_large(self):
        large_bytes = b"0" * (MAX_IMAGE_BYTES + 10)
        import base64
        b64_data = base64.b64encode(large_bytes).decode()

        with mock.patch("core.agent_tools.search_tools._ws_resolve", return_value="dummy/large.png"), \
             mock.patch("core.agent_tools.search_tools._remote_uid", return_value="user1"), \
             mock.patch("core.companion_bridge.call", return_value={"data": b64_data}):
            res = asyncio.run(tool_analyze_image({"path": "large.png"}))
            self.assertIn("error: image too large", res)


if __name__ == "__main__":
    unittest.main()
