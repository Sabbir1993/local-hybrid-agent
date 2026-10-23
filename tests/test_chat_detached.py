"""A chat answer runs as a detached server task: it survives its connection, and re-attaching replays it.

Run: python -m unittest tests.test_chat_detached -v
"""
import asyncio
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import runs
from routes.agent import run_manager

SRC = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")


class DetachedChatTests(unittest.TestCase):
    def tearDown(self):
        run_manager._RUNS.clear()

    def test_the_answer_finishes_without_any_subscriber_and_replays_after(self):
        uid = 8_000_000 + uuid.uuid4().int % 1_000_000
        rid = "c" + uuid.uuid4().hex[:12]

        async def answer():
            for word in ("Hel", "lo"):
                await asyncio.sleep(0.01)
                yield f"event: delta\ndata: {{\"text\": \"{word}\"}}\n\n"
            yield "event: done\ndata: {}\n\n"

        async def scenario():
            h = run_manager.start(uid, None, answer(), rid, kind="chat", journal=False)
            first = run_manager.subscribe(h)
            await first.__anext__()                  # one frame, then the "page" goes away (refresh)
            await first.aclose()
            await h.task                             # the run keeps going and finishes on its own
            seen = [f async for f in run_manager.subscribe(run_manager.owned(rid, uid), after=-1)]
            return h, seen

        h, seen = asyncio.run(scenario())
        self.assertEqual((h.kind, h.state, h.done), ("chat", "completed", True))
        self.assertEqual(sum(f.count("event: delta") for f in seen), 2)       # the whole answer replays from the start
        self.assertIn("event: done", seen[-1])
        self.assertIsNone(runs.get(rid))                                       # no second journal row: the chat route has its own
        self.assertEqual(h.summary()["kind"], "chat")

    def test_the_chat_route_hands_its_stream_to_the_run_manager(self):
        self.assertIn('run_manager.start(user.id, req.session_id, resp.body_iterator, req.client_run_id, kind="chat", journal=False)', SRC)
        self.assertIn('"X-Run-Id": handle.id', SRC)


if __name__ == "__main__":
    unittest.main()
