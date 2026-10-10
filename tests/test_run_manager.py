"""tests/test_run_manager.py - runs that outlive their request (routes/agent/run_manager.py).

Run: python -m unittest tests.test_run_manager -v
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.agent import run_manager as rm  # noqa: E402


def frame(event, **data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def producer(n=3, delay=0.0, final=True, gate=None):
    """A stand-in for the loop: n tool frames, then done."""
    yield frame("run", run_id="r1")
    for i in range(n):
        if gate is not None:
            await gate.wait()
        if delay:
            await asyncio.sleep(delay)
        yield frame("tool_call", name=f"t{i}")
    if final:
        yield frame("done", state="completed")


async def collect(handle, after=-1, limit=None):
    out = []
    async for f in rm.subscribe(handle, after):
        out.append(f)
        if limit and len(out) >= limit:
            break
    return out


def seqs(frames):
    return [int(f.split("\n", 1)[0][4:]) for f in frames if f.startswith("id: ")]


class Base(unittest.TestCase):
    def setUp(self):
        rm.clear()

    def tearDown(self):
        rm.clear()

    def run_(self, coro):
        return asyncio.run(coro)


class ReplayAndFollow(Base):
    def test_a_late_subscriber_gets_everything_in_order_with_ids(self):
        async def go():
            h = rm.start(1, 5, producer(3))
            await h.task
            return await collect(h)
        frames = self.run_(go())
        self.assertEqual(seqs(frames), [0, 1, 2, 3, 4])
        self.assertIn("event: run", frames[0])
        self.assertIn("event: done", frames[-1])

    def test_resume_after_a_sequence_number(self):
        async def go():
            h = rm.start(1, 5, producer(3))
            await h.task
            return await collect(h, after=2)
        self.assertEqual(seqs(self.run_(go())), [3, 4])

    def test_a_subscriber_follows_a_live_run_and_ends_with_it(self):
        async def go():
            gate = asyncio.Event()
            h = rm.start(1, 5, producer(2, gate=gate))
            sub = asyncio.ensure_future(collect(h))
            await asyncio.sleep(0.05)
            self.assertFalse(sub.done(), "still waiting: the run is blocked")
            gate.set()
            return await asyncio.wait_for(sub, 2)
        self.assertEqual(seqs(self.run_(go())), [0, 1, 2, 3])


class DisconnectDoesNotCancel(Base):
    def test_a_subscriber_that_goes_away_leaves_the_run_running_to_completion(self):
        async def go():
            gate = asyncio.Event()
            h = rm.start(1, 5, producer(2, gate=gate))
            sub = asyncio.ensure_future(collect(h))
            await asyncio.sleep(0.05)
            sub.cancel()                                  # the tab was closed
            with self.assertRaises(asyncio.CancelledError):
                await sub
            self.assertFalse(h.done, "the run was not touched")
            gate.set()
            await asyncio.wait_for(h.task, 2)
            return h, await collect(h)                    # reopened later: everything is there
        h, frames = self.run_(go())
        self.assertEqual((h.state, h.cancel_requested), ("completed", False))
        self.assertEqual(seqs(frames), [0, 1, 2, 3])


class Cancel(Base):
    def test_cancel_stops_the_run_and_tells_subscribers_how_it_ended(self):
        closed = []

        async def stuck():
            try:
                yield frame("run", run_id="r1")
                await asyncio.sleep(60)
                yield frame("done", state="completed")
            finally:
                closed.append(True)

        async def go():
            h = rm.start(1, 5, stuck())
            sub = asyncio.ensure_future(collect(h))
            await asyncio.sleep(0.05)
            self.assertTrue(h.cancel())
            frames = await asyncio.wait_for(sub, 2)
            return h, frames
        h, frames = self.run_(go())
        self.assertEqual(h.state, "stopped")
        self.assertIn("event: done", frames[-1])
        self.assertIn('"reason": "cancelled"', frames[-1])
        self.assertTrue(closed, "the loop's own cleanup ran")
        self.assertFalse(h.cancel(), "cancelling a finished run is a no-op")

    def test_a_loop_that_reports_its_own_stop_is_not_overwritten(self):
        async def polite():
            try:
                yield frame("run", run_id="r1")
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                yield frame("done", state="stopped", reason="user_cancelled")
        async def go():
            h = rm.start(1, 5, polite())
            await asyncio.sleep(0.05)
            h.cancel()
            await asyncio.wait_for(h.task, 2)
            return h
        h = self.run_(go())
        self.assertEqual((h.state, h.reason), ("stopped", "user_cancelled"))
        self.assertEqual(sum(1 for f in h.frames if f.startswith("event: done")), 1)

    def test_a_crash_becomes_a_failed_done_frame(self):
        async def boom():
            yield frame("run", run_id="r1")
            raise RuntimeError("secret detail that must not leak")

        async def go():
            h = rm.start(1, 5, boom())
            await h.task
            return h
        h = self.run_(go())
        self.assertEqual(h.state, "failed")
        self.assertIn("run_crashed: RuntimeError", h.frames[-1])
        self.assertNotIn("secret detail", h.frames[-1])


class Bounds(Base):
    def test_pings_are_not_buffered_but_idle_subscribers_get_their_own(self):
        async def noisy():
            yield ": ping\n\n"
            yield frame("run", run_id="r1")
            yield ": ping\n\n"
            yield frame("done", state="completed")

        async def go():
            h = rm.start(1, 5, noisy())
            await h.task
            return h
        h = self.run_(go())
        self.assertEqual(len(h.frames), 2)

        async def idle():
            gate = asyncio.Event()
            h2 = rm.start(1, 5, producer(1, gate=gate))
            with mock.patch.object(rm, "PING_S", 0.05):
                frames = await collect(h2, limit=3)
            gate.set()
            return frames
        frames = self.run_(idle())
        self.assertTrue(any(f.startswith(": ping") for f in frames), frames)

    def test_the_buffer_drops_the_oldest_frames_and_says_so(self):
        with mock.patch.dict(rm.APP_CONFIG.setdefault("agent", {}), {"run_buffer_max_bytes": 600}):
            async def go():
                h = rm.start(1, 5, producer(40))
                await h.task
                return h, await collect(h)
            h, frames = self.run_(go())
        self.assertGreater(h.base_seq, 0, "old frames were dropped")
        self.assertLessEqual(h.bytes, 600 + 200)
        self.assertTrue(frames[0].startswith("event: gap"), "a subscriber is told it missed the beginning")
        self.assertIn("event: done", frames[-1], "the end is always kept")

    def test_a_user_cannot_start_more_than_the_limit(self):
        async def go():
            gate = asyncio.Event()
            with mock.patch.dict(rm.APP_CONFIG.setdefault("agent", {}), {"max_active_runs_per_user": 2}):
                rm.start(1, 5, producer(1, gate=gate))
                rm.start(1, 5, producer(1, gate=gate))
                with self.assertRaises(rm.TooManyRuns):
                    rm.start(1, 5, producer(1, gate=gate))
                rm.start(2, 5, producer(1, gate=gate))          # another user is unaffected
            gate.set()
        self.run_(go())

    def test_finished_runs_do_not_count_against_the_limit(self):
        async def go():
            with mock.patch.dict(rm.APP_CONFIG.setdefault("agent", {}), {"max_active_runs_per_user": 1}):
                h = rm.start(1, 5, producer(1))
                await h.task
                rm.start(1, 5, producer(1))
        self.run_(go())


class RetentionAndOwnership(Base):
    def test_ack_frees_a_finished_run_and_unfinished_ones_stay(self):
        async def go():
            gate = asyncio.Event()
            done = rm.start(1, 5, producer(1))
            await done.task
            live = rm.start(1, 5, producer(1, gate=gate))
            self.assertEqual(len(rm.list_for(1)), 2)
            done.acked = True
            live.acked = True                               # acking a run that is still going frees nothing
            ids = [r["id"] for r in rm.list_for(1)]
            gate.set()
            await live.task
            return done.id, live.id, ids
        done_id, live_id, ids = self.run_(go())
        self.assertNotIn(done_id, ids)
        self.assertIn(live_id, ids)

    def test_finished_runs_expire(self):
        async def go():
            h = rm.start(1, 5, producer(1))
            await h.task
            h.finished_at -= 7 * 3600
            return rm.list_for(1)
        self.assertEqual(self.run_(go()), [])

    def test_runs_are_only_visible_to_their_owner_and_listed_by_session(self):
        async def go():
            a = rm.start(1, 5, producer(1))
            b = rm.start(1, 6, producer(1))
            await asyncio.gather(a.task, b.task)
            return a, b
        a, b = self.run_(go())
        self.assertIsNone(rm.owned(a.id, 2))
        self.assertIs(rm.owned(a.id, 1), a)
        self.assertEqual([r["id"] for r in rm.list_for(1, 5)], [a.id])
        self.assertEqual(rm.list_for(2), [])

    def test_a_client_chosen_id_is_used_when_valid_and_never_trusted_blindly(self):
        async def go():
            h = rm.start(1, 5, producer(1), run_id="client-chosen-id-123")
            h2 = rm.start(1, 5, producer(1), run_id="client-chosen-id-123")      # duplicate: a fresh id instead
            h3 = rm.start(1, 5, producer(1), run_id="../../etc/passwd")
            await asyncio.gather(h.task, h2.task, h3.task)
            return h, h2, h3
        h, h2, h3 = self.run_(go())
        self.assertEqual(h.id, "client-chosen-id-123")
        self.assertNotEqual(h2.id, h.id)
        self.assertRegex(h3.id, r"^[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
