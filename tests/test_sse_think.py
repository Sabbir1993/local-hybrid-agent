"""_process_sse_stream: inline <think> handling, incl. forced-open templates
(reasoning streamed with no opening tag, then a bare </think>)."""
import asyncio
import json
import unittest

from routes.common import ThinkSplitter, _process_sse_stream


class _FakeResp:
    def __init__(self, chunks, reasoning=None):
        self.lines = []
        for r in reasoning or []:
            self.lines.append("data: " + json.dumps({"choices": [{"delta": {"reasoning_content": r}}]}))
        for c in chunks:
            self.lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
        self.lines.append("data: [DONE]")

    async def aiter_lines(self):
        for ln in self.lines:
            yield ln


def _run(chunks, reasoning=None):
    async def go():
        return [item async for item in _process_sse_stream(_FakeResp(chunks, reasoning))]
    events = asyncio.run(go())
    return events[:-1], events[-1][1]


class ThinkStreamTests(unittest.TestCase):
    def test_orphan_close_moves_streamed_text_to_reasoning(self):
        evs, res = _run(["Let me build ", "the PDF now.", "\n</think>\n\n", "Here is the deck."])
        self.assertIn(("content_to_thought", "Let me build the PDF now."), evs)
        self.assertEqual(res["content"], "Here is the deck.")
        self.assertEqual(res["reasoning"], "Let me build the PDF now.\n")

    def test_tag_split_across_chunks(self):
        evs, res = _run(["plan it</th", "ink>\n\nAnswer"])
        self.assertEqual(res["content"], "Answer")
        self.assertEqual(res["reasoning"], "plan it")
        self.assertNotIn("</th", "".join(v for e, v in evs if e == "content_delta"))

    def test_normal_think_block(self):
        evs, res = _run(["<think>hmm", " ok</think>", "Hi"])
        self.assertEqual(res["content"], "Hi")
        self.assertEqual(res["reasoning"], "hmm ok")
        self.assertFalse(any(e == "content_to_thought" for e, _ in evs))

    def test_split_open_tag(self):
        _evs, res = _run(["<thi", "nk>x</think>y"])
        self.assertEqual((res["content"], res["reasoning"]), ("y", "x"))

    def test_reasoning_content_path_unchanged(self):
        _evs, res = _run(["Answer"], reasoning=["thinking..."])
        self.assertEqual((res["content"], res["reasoning"]), ("Answer", "thinking..."))

    def test_literal_close_after_think_block_stays_content(self):
        _evs, res = _run(["<think>r</think>", "Use `</think>` to end it."])
        self.assertEqual(res["content"], "Use `</think>` to end it.")

    def test_trailing_partial_tag_flushed_as_content(self):
        _evs, res = _run(["a < b and c <"])
        self.assertEqual(res["content"], "a < b and c <")

    def test_splitter_flush_inside_think(self):
        t = ThinkSplitter()
        t.feed("<think>unfinished </thi")
        self.assertEqual(t.flush(), [("thought", "</thi")])


if __name__ == "__main__":
    unittest.main()
