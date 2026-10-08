"""tests/test_voice_policy.py - what changes for a spoken turn, and that typed chat is left alone.

Run: python -m unittest tests.test_voice_policy -v
"""
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import output_guard, prompt_scope, router_policy, voice_policy


def req(**kw):
    base = dict(voice=True, reasoning_effort="low", max_tokens=-1, verify="gate")
    base.update(kw)
    return SimpleNamespace(**base)


class Apply(unittest.TestCase):
    def test_a_spoken_turn_is_narrowed(self):
        r = req()
        self.assertTrue(voice_policy.apply(r))
        self.assertEqual(r.reasoning_effort, "none")
        self.assertEqual(r.max_tokens, voice_policy.cfg()["max_tokens"])
        self.assertEqual(r.verify, "off")

    def test_typed_chat_is_untouched(self):
        r = req(voice=False)
        self.assertFalse(voice_policy.apply(r))
        self.assertEqual((r.reasoning_effort, r.max_tokens, r.verify), ("low", -1, "gate"))
        self.assertFalse(voice_policy.web_explicit_only(r))
        self.assertFalse(voice_policy.kb_company_only(r))
        self.assertFalse(voice_policy.sentence_release(r))

    def test_a_token_limit_the_user_set_is_kept_and_an_agent_is_never_capped(self):
        r = req(max_tokens=900)
        voice_policy.apply(r)
        self.assertEqual(r.max_tokens, 900)
        a = req()
        voice_policy.apply(a, cap_tokens=False)
        self.assertEqual(a.max_tokens, -1)           # a tool call may need long arguments
        self.assertEqual(a.reasoning_effort, "none")

    def test_admin_can_switch_voice_mode_off(self):
        with mock.patch.object(voice_policy, "cfg", return_value={**voice_policy.DEFAULTS, "enabled": False}):
            r = req()
            self.assertFalse(voice_policy.apply(r))
            self.assertFalse(r.voice)
            self.assertEqual(r.reasoning_effort, "low")

    def test_config_values_are_clamped(self):
        with mock.patch("core.small_model.APP_CONFIG", {"voice": {"max_tokens": 1, "end_silence_ms": 99999, "junk": 1}}):
            c = voice_policy.cfg()
        self.assertEqual(c["max_tokens"], 32)
        self.assertEqual(c["end_silence_ms"], 2500)
        self.assertNotIn("junk", c)

    def test_browser_gets_only_its_part(self):
        self.assertEqual(set(voice_policy.public()), {"enabled", "end_silence_ms", "filler"})


class WebIntent(unittest.TestCase):
    def test_everyday_words_do_not_start_a_search_by_voice(self):
        q = "what's the weather today and the latest news"
        self.assertTrue(prompt_scope.web_intent(q))
        self.assertFalse(prompt_scope.web_intent(q, explicit_only=True))

    def test_explicit_search_and_urls_still_work_by_voice(self):
        self.assertTrue(prompt_scope.web_intent("please search for python release notes", explicit_only=True))
        self.assertTrue(prompt_scope.web_intent("read this", url_matches=["https://example.com"], explicit_only=True))


class Greeting(unittest.TestCase):
    def test_punctuated_speech_is_a_greeting(self):
        for t in ("Hello.", "Hi, how are you?", "Good morning!", "hey there", "hello"):
            self.assertTrue(router_policy.is_greeting(t), t)

    def test_real_requests_are_not(self):
        for t in ("What is my balance?", "Help me build an app", "/init", "check the disk space"):
            self.assertFalse(router_policy.is_greeting(t), t)


class SentenceRelease(unittest.TestCase):
    def setUp(self):
        self.rule = {"name": "secret", "patterns": [(re.compile(r"SECRET-\d{6}"), "[hidden]")]}

    def make(self, release):
        with mock.patch.object(output_guard, "_active_rules", return_value=[self.rule]):
            return output_guard.OutputRedactor(None, False, sentence_release=release)

    def test_default_holds_a_short_reply_until_the_end(self):
        red = self.make(False)
        out = red.feed("Hello there. How can I help you today? ")
        self.assertEqual(out, "")                           # inside the holdback window
        self.assertEqual(red.flush(), "Hello there. How can I help you today? ")

    def test_sentence_release_lets_finished_sentences_through(self):
        red = self.make(True)
        self.assertEqual(red.feed("Hello there. How can"), "Hello there. ")
        self.assertEqual(red.feed(" I help you? Fine"), "How can I help you? ")
        self.assertEqual(red.flush(), "Fine")

    def test_a_pattern_inside_a_sentence_is_still_redacted(self):
        red = self.make(True)
        out = red.feed("Your code is SECRET-123456. Anything else? ")
        self.assertNotIn("SECRET", out)
        self.assertIn("[hidden]", out)

    def test_a_pattern_split_across_deltas_is_never_released_early(self):
        red = self.make(True)
        got = red.feed("It is fine. The code is SECRET-12")
        self.assertEqual(got, "It is fine. ")               # the growing match stays held
        got += red.feed("3456 and that is all. ")
        got += red.flush()
        self.assertNotIn("SECRET", got)
        self.assertIn("[hidden]", got)

    def test_a_decimal_is_not_a_sentence_end(self):
        red = self.make(True)
        self.assertEqual(red.feed("It costs 3.5 dollars"), "")


class Wiring(unittest.TestCase):
    def test_both_routes_apply_the_policy_and_the_guard_release(self):
        chat = Path("routes/chat/run.py").read_text(encoding="utf-8")
        agent = Path("routes/agent/setup.py").read_text(encoding="utf-8")
        stream = Path("routes/agent/stream.py").read_text(encoding="utf-8")
        self.assertIn("voice_policy.apply(req)", chat)
        self.assertIn("voice_policy.apply(req, cap_tokens=False)", agent)
        self.assertEqual(chat.count("sentence_release=voice_policy.sentence_release(req)"), 2)
        self.assertEqual(stream.count("StreamSpec(sentence_release=voice_policy.sentence_release(req)"), 3)

    def test_helper_lane_honours_an_explicit_none_effort(self):
        src = Path("routes/common/llm_stream.py").read_text(encoding="utf-8")
        self.assertIn('if effort == "none":\n            payload.update(reasoning.local_fields("none"))', src)


if __name__ == "__main__":
    unittest.main()
