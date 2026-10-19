"""Runaway guard: a looping generation is cut while it streams, a looping tool call never runs."""
import asyncio
import json
import unittest

from core.agent_loop.degeneration import RUNAWAY_MSG, find_runaway
from core.agent_loop.execution import run_tool
from routes.common.sse_stream import _process_sse_stream


class FakeResponse:
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0

    async def aiter_lines(self):
        for c in self.chunks:
            self.consumed += 1
            yield "data: " + json.dumps(c)
        yield "data: [DONE]"


def content(text):
    return {"choices": [{"delta": {"content": text}}]}


def reasoning(text):
    return {"choices": [{"delta": {"reasoning_content": text}}]}


def tool_args(name, text, first=False):
    fn = {"arguments": text}
    if first:
        fn["name"] = name
    return {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": fn}]}}]}


def run(resp):
    async def go():
        out = []
        async for item in _process_sse_stream(resp):
            out.append(item)
        return out
    return asyncio.run(go())


LOOP = "The capital city continues to grow rapidly every single year. "


class Detector(unittest.TestCase):
    def test_a_repeating_phrase_is_cut_after_one_copy(self):
        text = "Intro paragraph about the topic. " * 3 + LOOP * 10
        cut = find_runaway(text)
        self.assertGreater(cut, 0)
        self.assertEqual(text[:cut].count(LOOP.strip()), 1)

    def test_identical_lines_are_a_loop(self):
        text = "Some real content here.\n" * 1 + ("- same bullet point repeated\n" * 8) + "x" * 400
        self.assertGreaterEqual(find_runaway(text), 0)

    def test_real_outlines_and_tables_are_left_alone(self):
        deck = "".join(f"## Slide {i}\n- point number {i} about growth in sector {i * 7}\nsource: Wikipedia\n---\n"
                       for i in range(40))
        table = "| a | b |\n|---|---|\n" + "".join(f"| row {i} | value {i * 3} |\n" for i in range(60))
        self.assertEqual(find_runaway(deck), -1)
        self.assertEqual(find_runaway(table), -1)
        self.assertEqual(find_runaway("---\n" * 100 + "x" * 400), -1)

    def test_cards_with_the_same_image_link_tail_are_markup_not_a_loop(self):
        cards = "".join(f'<div class="card"><h3>Place {i} of the old city</h3><p>Fact number {i * 13} about it.</p>'
                        f'<img src="https://images.unsplash.com/photo-{i}?auto=format&fit=crop&w=800&q=80" alt="photo" loading="lazy"></div>\n'
                        for i in range(14))
        self.assertEqual(find_runaway(cards), -1)

    def test_short_text_is_never_flagged(self):
        self.assertEqual(find_runaway(LOOP * 3), -1)


class Tables(unittest.TestCase):
    def test_table_rows_that_share_long_cells_are_not_a_loop(self):
        rows = "".join(f"| Feature {i} | Core application development team assigned | High | 4 weeks |\n" for i in range(40))
        self.assertEqual(find_runaway("| Name | Owner | Priority | Effort |\n|---|---|---|---|\n" + rows), -1)
        csv_rows = "".join(f'"Feature {i}","Core application development team assigned","High","4 weeks"\n' for i in range(40))
        self.assertEqual(find_runaway(csv_rows), -1)

    def test_a_table_that_really_loops_is_still_cut(self):
        line = "| Same row repeated again and again | High | 4 weeks |\n"
        self.assertGreaterEqual(find_runaway("| a | b | c |\n|---|---|---|\n" + line * 12), 0)

    def test_prose_loops_are_still_cut(self):
        self.assertGreaterEqual(find_runaway("Intro paragraph about the topic. " * 3 + LOOP * 12), 0)


class Stream(unittest.TestCase):
    def test_looping_content_stops_early(self):
        chunks = [content("Here is the answer. ")] + [content(LOOP) for _ in range(400)]
        resp = FakeResponse(chunks)
        res = run(resp)[-1][1]
        self.assertEqual(res["finish_reason"], "runaway")
        self.assertEqual(res["aborted"], "content")
        self.assertLess(resp.consumed, len(chunks))
        self.assertLess(len(res["content"]), 2500)

    def test_looping_reasoning_stops_early(self):
        resp = FakeResponse([reasoning(LOOP) for _ in range(400)])
        res = run(resp)[-1][1]
        self.assertEqual(res["aborted"], "reasoning")
        self.assertLess(resp.consumed, 400)

    def test_looping_doc_append_never_executes(self):
        body = json.dumps({"path": "a.pptx", "text": ""})[:-3]
        chunks = [tool_args("doc_append", body, first=True)] + [tool_args("doc_append", LOOP) for _ in range(400)]
        resp = FakeResponse(chunks)
        res = run(resp)[-1][1]
        self.assertEqual(res["aborted"], "tool")
        self.assertLess(resp.consumed, len(chunks))
        args = json.loads(res["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args.get("_runaway"), True)
        self.assertEqual(args.get("path"), "a.pptx")
        self.assertEqual(asyncio.run(run_tool("doc_append", args)), RUNAWAY_MSG)

    def test_normal_stream_is_untouched(self):
        chunks = [content(f"Sentence number {i} says something different. ") for i in range(120)]
        res = run(FakeResponse(chunks))[-1][1]
        self.assertIsNone(res["aborted"])
        self.assertIsNone(res["finish_reason"])

    def test_generation_timeout(self):
        import routes.common.sse_stream as sse
        old = sse._gen_timeout
        sse._gen_timeout = lambda: 0.0001
        try:
            async def slow():
                for i in range(50):
                    await asyncio.sleep(0.001)
                    yield "data: " + json.dumps(content(f"word{i} "))
            class R:
                def aiter_lines(self):
                    return slow()
            res = run(R())[-1][1]
        finally:
            sse._gen_timeout = old
        self.assertEqual(res["finish_reason"], "timeout")


if __name__ == "__main__":
    unittest.main()
