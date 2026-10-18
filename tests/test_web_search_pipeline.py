import asyncio
import unittest
from unittest import mock

import core.web_search as ws
from core import web_tools
from core.web_search import health, service


def _r(url):
    return {"title": url, "url": url, "snippet": "s"}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        health.reset()
        self.calls = []

    def run_search(self, backends, cfg, *a, **kw):
        async def _rerank(q, items):
            return items

        async def _attach(q, items, n):
            return None

        patches = [mock.patch.object(ws, "BACKENDS", backends, create=True), mock.patch.object(ws, "cfg", lambda: cfg),
                   mock.patch.object(ws, "cache_get", lambda k: None),
                   mock.patch.object(ws, "cache_put", lambda k, v: None),
                   mock.patch.object(ws, "rerank", _rerank),
                   mock.patch.object(ws, "attach_passages", _attach)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(service.search(*a, **kw))

    def backend(self, name, urls=None, boom=False):
        def fn(q, recency):
            self.calls.append((name, q))
            if boom:
                raise RuntimeError("down")
            return [_r(u) for u in (urls or [])]
        return fn

    def test_searxng_enough_results_skips_scrapers(self):
        be = {"searxng": self.backend("searxng", [f"https://a.com/{i}" for i in range(6)]),
              "duckduckgo": self.backend("duckduckgo", ["https://b.com/x"])}
        out = self.run_search(be, {"searxng_url": "http://127.0.0.1:8888"}, "q")
        self.assertEqual(out["engines"], ["searxng"])
        self.assertNotIn("duckduckgo", {c[0] for c in self.calls})

    def test_searxng_down_falls_back_to_scrapers(self):
        be = {"searxng": self.backend("searxng", boom=True),
              "duckduckgo": self.backend("duckduckgo", ["https://b.com/x"])}
        out = self.run_search(be, {"searxng_url": "http://127.0.0.1:8888"}, "q")
        self.assertEqual(out["engines"], ["duckduckgo"])
        self.assertIn("searxng", out["errors"])

    def test_extra_queries_are_fused_and_capped(self):
        be = {"duckduckgo": self.backend("duckduckgo", ["https://b.com/x"])}
        out = self.run_search(be, {}, "q", extra_queries=["q2", "Q", "q3", "q4"])
        self.assertEqual([c[1] for c in self.calls], ["q", "q2", "q3"])
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(len(out["results"][0]["engines"]), 3)

    def test_breaker_opens_after_repeated_failures_and_resets(self):
        for _ in range(health.FAIL_LIMIT):
            health.record("bing", ok=False)
        self.assertTrue(health.is_open("bing"))
        health.record("bing", ok=True)
        self.assertFalse(health.is_open("bing"))

    def test_open_backend_is_skipped(self):
        for _ in range(health.FAIL_LIMIT):
            health.record("bing", ok=False)
        be = {"duckduckgo": self.backend("duckduckgo", ["https://b.com/x"]), "bing": self.backend("bing", ["https://c.com"])}
        self.run_search(be, {}, "q")
        self.assertEqual({c[0] for c in self.calls}, {"duckduckgo"})


class InboundPanTests(unittest.TestCase):
    def test_card_number_on_page_is_masked(self):
        with mock.patch("core.pan.enabled", lambda k: True):
            out = web_tools._mask_inbound("order card 4111 1111 1111 1111 expires soon")
        self.assertNotIn("4111 1111 1111 1111", out)
        self.assertIn("masked", out)

    def test_disabled_leaves_text(self):
        with mock.patch("core.pan.enabled", lambda k: False):
            self.assertEqual(web_tools._mask_inbound("plain"), "plain")


if __name__ == "__main__":
    unittest.main()
