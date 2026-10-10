import json
import unittest

from routes.common.sse_stream import _oversize_write, _process_sse_stream


class _Resp:
    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


def _chunk(args, name=None):
    fn = {"arguments": args}
    if name:
        fn["name"] = name
    return "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": fn}]}}]})


class OversizeWriteTests(unittest.IsolatedAsyncioTestCase):
    def test_threshold(self):
        self.assertFalse(_oversize_write("write_file", "a.php", "x" * 1000))
        self.assertTrue(_oversize_write("write_file", "a.php", "x" * 30000))
        self.assertFalse(_oversize_write("write_file", "a.docx", "x" * 30000))
        self.assertFalse(_oversize_write("read_file", "a.php", "x" * 30000))

    async def test_stream_is_cut_and_args_are_valid_json(self):
        lines = [_chunk('{"path": "a.php", "content": "', "write_file")] + [_chunk("x" * 2000)] * 40 + [_chunk('"}')]
        result = None
        async for ev, val in _process_sse_stream(_Resp(lines)):
            if ev == "result":
                result = val
        args = json.loads(result["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["path"], "a.php")
        self.assertGreater(args["_oversize_tokens"], 4000)

    async def test_tool_returns_too_big_error(self):
        from core.agent_tools.file_ops import tool_write_file
        out = await tool_write_file({"path": "a.php", "content": "", "_oversize_tokens": 6000})
        self.assertIn("one call may carry at most", out)


if __name__ == "__main__":
    unittest.main()
