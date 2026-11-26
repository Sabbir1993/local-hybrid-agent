"""tests/test_prompt_trim.py - a plain greeting must not carry the whole agent prompt (core/prompt_trim.py).

Run: python -m unittest tests.test_prompt_trim -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import prompt_trim as pt  # noqa: E402
from core import router_policy  # noqa: E402

RPOL = router_policy.rcfg()


def msgs(text, *earlier):
    return [*earlier, {"role": "user", "content": text}]


class Switches(unittest.TestCase):
    def test_defaults_are_on_and_each_switch_turns_it_off(self):
        self.assertTrue(pt.enabled("greeting", {}))
        self.assertFalse(pt.enabled("greeting", {"greeting": False}))
        self.assertFalse(pt.enabled("greeting", {"enabled": False}))
        self.assertTrue(pt.enabled("greeting", None) in (True, False))      # reads app.json without failing


class Decision(unittest.TestCase):
    def test_plain_greetings_use_the_stub(self):
        for q in ("Hi", "hello", "Hey there", "good morning", "hi how are you"):
            with self.subTest(q=q):
                self.assertTrue(pt.greeting_turn(msgs(q), RPOL, cfg={}))

    def test_real_requests_keep_the_full_prompt(self):
        for q in ("Hi, build me an Excel report", "make a workbook", "what is 2+2", "/plan do it", "fix the login bug"):
            with self.subTest(q=q):
                self.assertFalse(pt.greeting_turn(msgs(q), RPOL, cfg={}))

    def test_runs_with_their_own_instructions_are_never_trimmed(self):
        base = msgs("Hi")
        self.assertFalse(pt.greeting_turn(base, RPOL, plan=True, cfg={}))
        self.assertFalse(pt.greeting_turn(base, RPOL, custom_agent={"name": "x"}, cfg={}))
        self.assertFalse(pt.greeting_turn(base, RPOL, voice=True, cfg={}))
        self.assertFalse(pt.greeting_turn(base, RPOL, request_system="Always answer in French.", cfg={}))

    def test_switch_off_restores_the_full_prompt(self):
        self.assertFalse(pt.greeting_turn(msgs("Hi"), RPOL, cfg={"greeting": False}))
        self.assertFalse(pt.greeting_turn([], RPOL, cfg={}))

    def test_the_stub_is_small_and_keeps_identity_language_and_style(self):
        self.assertLess(len(pt.GREETING_PROMPT) // 4, 100, "a greeting stub must stay around 100 tokens")
        low = pt.GREETING_PROMPT.lower()
        for need in ("assistant", "language", "briefly"):
            self.assertIn(need, low)


class LoopSize(unittest.TestCase):
    """Through the real /agent/run route with the mock model: what the model is sent for "Hi"."""

    def request_bytes(self, prompt, cfg):
        import eval_mock as em
        from test_doc_redirect import LoopBehaviour
        from core.small_model import APP_CONFIG
        with mock.patch.dict(APP_CONFIG, {"prompt_trim": cfg}):
            em._current["prompt_chars"] = []
            events, _ = LoopBehaviour.run_script(self, prompt, [em.T(content="Hello! How can I help?")])
        sizes = list(em._current.get("prompt_chars", []))
        return sizes, events

    def test_a_greeting_request_is_much_smaller_when_trimmed_and_still_answers(self):
        full, _ = self.request_bytes("Hi", {"greeting": False})
        small, events = self.request_bytes("Hi", {"greeting": True})
        self.assertTrue(full and small)
        self.assertLess(small[0], full[0] * 0.5, f"trimmed {small[0]} bytes vs full {full[0]}")
        self.assertTrue(any(e == "done" for e, _ in events), "the greeting still completes")

    def test_a_real_task_is_not_trimmed(self):
        full, _ = self.request_bytes("Create a file notes.txt containing hello", {"greeting": False})
        trim, _ = self.request_bytes("Create a file notes.txt containing hello", {"greeting": True})
        self.assertEqual(full[0], trim[0], "only greetings change")


if __name__ == "__main__":
    unittest.main()
