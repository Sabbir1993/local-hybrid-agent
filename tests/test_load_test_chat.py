"""scripts/load_test_chat.py: SSE framing, per-request timing and report math. No network."""

import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "load_test_chat", Path(__file__).resolve().parents[1] / "scripts" / "load_test_chat.py")
lt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lt)


def _timed(events, end, user="u", rnd=1):
    t = lt.StreamTimer(lt.RequestResult(user=user, round=rnd))
    for at, ev, data in events:
        t.feed(at, ev, data)
    return t.finish(end)


class Sse(unittest.TestCase):
    def test_framing(self):
        lines = ["event: lane", 'data: {"lane": "main"}', "", ": keepalive", "",
                 "event: queued", 'data: {"position": 2}', "",
                 "event: delta", 'data: {"text": "hi"}', "",
                 'data: plain', "", "event: done", "data: {}"]
        evs = list(lt.iter_sse(lines))
        self.assertEqual([e for e, _ in evs], ["lane", "queued", "delta", "message", "done"])
        self.assertEqual(evs[1][1], {"position": 2})
        self.assertEqual(evs[3][1], "plain")


class Timing(unittest.TestCase):
    def test_queued_request(self):
        r = _timed([(0.01, "lane", {}), (0.02, "queued", {"position": 3}),
                    (1.0, "queued", {"position": 1}), (4.0, "delta", {"text": "a"}),
                    (6.0, "delta", {"text": "b"}),
                    (6.5, "done", {"prompt_tokens": 40, "completion_tokens": 50, "gen_tps": 25.0,
                                   "prompt_tps": 800.0})], end=6.6)
        self.assertTrue(r.ok)
        self.assertTrue(r.queued)
        self.assertEqual(r.queue_position, 3)
        self.assertAlmostEqual(r.queue_wait_s, 4.0 - 0.02)
        self.assertAlmostEqual(r.ttft_s, 4.0)
        self.assertEqual(r.gen_tps, 25.0)
        self.assertEqual((r.prompt_tokens, r.completion_tokens), (40, 50))
        self.assertAlmostEqual(r.total_s, 6.6)

    def test_unqueued_request_falls_back_to_token_rate(self):
        r = _timed([(0.5, "thought_delta", {"delta": "x"}), (1.0, "delta", {}),
                    (2.5, "done", {"completion_tokens": 40})], end=2.5)
        self.assertEqual(r.queue_wait_s, 0.0)
        self.assertAlmostEqual(r.ttft_s, 0.5)
        self.assertAlmostEqual(r.gen_tps, 20.0)       # 40 tokens over 2.0 s of streaming

    def test_errors(self):
        self.assertIn("without a done", _timed([(0.1, "delta", {})], end=1).error)
        self.assertIn("run failed", _timed([(0.1, "done", {"state": "failed", "note": "X"})], end=1).error)
        r = _timed([(0.1, "error", {"message": "nope"}), (0.2, "done", {})], end=1)
        self.assertIn("nope", r.error)
        self.assertFalse(r.ok)

    def test_still_queued_at_end_counts_whole_wait(self):
        r = _timed([(1.0, "queued", {"position": 1})], end=9.0)
        self.assertAlmostEqual(r.queue_wait_s, 8.0)


class Report(unittest.TestCase):
    def test_percentile(self):
        self.assertIsNone(lt.percentile([], 50))
        self.assertEqual(lt.percentile([5], 95), 5)
        self.assertAlmostEqual(lt.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(lt.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 9.55)
        self.assertAlmostEqual(lt.percentile([3, None, 1], 100), 3)

    def test_summary(self):
        R = lt.RequestResult
        results = [
            R("a", 1, 200, False, None, 0.0, 1.0, 5.0, 20.0, None, 10, 100),
            R("b", 1, 200, True, 2, 3.0, 4.0, 8.0, 10.0, None, 10, 50),
            R("c", 1, 429, error="HTTP 429: too_many_runs"),
            R("a", 2, 200, False, None, 0.0, 2.0, 6.0, 30.0, None, 10, 150),
        ]
        s = lt.summarize(results, round_walls=[8.0, 6.0])
        self.assertEqual((s["requests"], s["ok"], s["errors"], s["queued"]), (4, 3, 1, 1))
        self.assertEqual(s["completion_tokens"], 300)
        self.assertAlmostEqual(s["wall_s"], 14.0)
        self.assertAlmostEqual(s["aggregate_decode_tps"], round(300 / 14.0, 2))
        self.assertAlmostEqual(s["ttft_s"]["mean"], round(7 / 3, 3))
        self.assertEqual(s["ttft_s"]["max"], 4.0)
        self.assertEqual(s["gen_tps_per_request"]["min"], 10.0)
        self.assertEqual(s["queue_wait_s"]["max"], 3.0)
        self.assertEqual(s["error_kinds"], {"HTTP 429": 1})
        text = lt.format_report(results, s)
        self.assertIn("aggregate decode", text)
        self.assertIn("too_many_runs", text)

    def test_long_prompt_size_and_uniqueness(self):
        p1 = lt.build_long_prompt(2000, "u1")
        p2 = lt.build_long_prompt(2000, "u2")
        self.assertGreaterEqual(len(p1), 8000)
        self.assertLess(len(p1), 8300)
        self.assertNotEqual(p1[:40], p2[:40])


class Args(unittest.TestCase):
    def test_local_hosts(self):
        for u in ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000",
                  "http://app.localhost", "https://box.test"):
            self.assertTrue(lt.is_local_url(u), u)
        for u in ("http://10.0.0.5:8000", "https://example.com", "http://localhost.evil.com"):
            self.assertFalse(lt.is_local_url(u), u)

    def test_remote_needs_flag_and_never_creates_users(self):
        import contextlib, io
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            lt.parse_args(["--base-url", "http://10.0.0.5:8000"])
        self.assertTrue(lt.parse_args(["--base-url", "http://10.0.0.5:8000", "--allow-remote"]).allow_remote)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            lt.parse_args(["--base-url", "http://10.0.0.5:8000", "--allow-remote", "--create-users"])

    def test_chat_body(self):
        args = lt.parse_args(["--max-tokens", "64"])
        b = lt.chat_body("hi", args)
        self.assertEqual(b["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual((b["max_tokens"], b["kb"], b["web_search"], b["reasoning_effort"]),
                         (64, "off", False, "none"))


if __name__ == "__main__":
    unittest.main()
