"""tests/test_proxy_guard.py - output/input guard wiring in routes/proxy.py.

Run: python -m unittest tests.test_proxy_guard -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import input_guard
from routes import proxy


class FakePrincipal:
    def __init__(self):
        self.id = 1
        self.username = "alice"
        self.role_names = ["user"]


RULE = {"id": "r1", "name": "No card numbers", "patterns": [r"\b\d{13,19}\b"],
        "scope": "block_all", "replacement": "[REDACTED]", "enabled": True}


def set_output_rules(rules, enabled=True):
    from core import small_model
    small_model.APP_CONFIG["output_guard"] = {"enabled": enabled, "rules": rules}
    # these tests exercise admin rules; the built-in PAN rule has its own tests
    small_model.APP_CONFIG["pci"] = {"pan_output": "off"}
    input_guard._compile_cache.clear()
    input_guard._cache_ts = 0.0


def sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def delta(text):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


def collect_text(stream: bytes) -> str:
    out = []
    for line in stream.split(b"\n"):
        if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
            obj = json.loads(line[5:])
            for ch in obj.get("choices") or []:
                out.append((ch.get("delta") or {}).get("content") or "")
    return "".join(out)


class SSERedactorTests(unittest.TestCase):
    def setUp(self):
        set_output_rules([RULE])

    def tearDown(self):
        set_output_rules([], enabled=False)

    def test_number_split_across_chunks_is_redacted(self):
        # [PLACEHOLDER] test digits only -- not a real card number
        parts = [sse(delta("card ")), sse(delta("41111111")), sse(delta("11111111 ok")),
                 b"data: [DONE]\n\n"]
        raw = b"".join(parts)
        r = proxy._SSERedactor(FakePrincipal(), False)
        self.assertTrue(r.active)
        # feed with chunk boundaries that cut through SSE lines
        out = b"".join(r.feed(raw[i:i + 7]) for i in range(0, len(raw), 7)) + r.close()
        text = collect_text(out)
        self.assertNotIn("4111111111111111", text)
        self.assertIn("[REDACTED]", text)
        self.assertTrue(text.startswith("card "))
        self.assertTrue(text.endswith(" ok"))
        self.assertIn(b"[DONE]", out)
        self.assertIsNotNone(r.red.matched)

    def test_passthrough_when_no_rules(self):
        set_output_rules([], enabled=False)
        r = proxy._SSERedactor(FakePrincipal(), False)
        self.assertFalse(r.active)

    def test_non_streaming_json_redacted(self):
        data = {"choices": [{"message": {"role": "assistant", "content": "n=4111111111111111"}}]}
        red, matched = proxy._redact_json(data, FakePrincipal(), False)
        self.assertEqual(red["choices"][0]["message"]["content"], "n=[REDACTED]")
        self.assertIsNotNone(matched)


class ProxyHelpersTests(unittest.TestCase):
    def test_prompt_texts(self):
        payload = {"messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "user", "content": [{"type": "text", "text": "part"},
                                         {"type": "image_url", "image_url": {"url": "x"}}]},
            # client-supplied assistant turns are scanned too: role is not a trust boundary
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"arguments": "{\"a\":1}"}}]},
        ], "prompt": "raw"}
        self.assertEqual(proxy._prompt_texts(payload), ["sys", "hello", "part", "", '{"a":1}', "raw"])

    def test_credentials_not_forwarded(self):
        class Req:
            headers = {"Host": "x", "Cookie": "session=[PLACEHOLDER]",
                       "Authorization": "Bearer [PLACEHOLDER]", "Content-Type": "application/json"}
        h = proxy._upstream_headers(Req())
        self.assertEqual(h, {"Content-Type": "application/json"})


if __name__ == "__main__":
    unittest.main()
