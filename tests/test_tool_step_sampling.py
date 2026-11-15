"""A step that offers tools must not run with DRY or repeat penalties: it copies file names and text out of its context."""
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from routes.common.sampling_extra import SamplerFields, sampler_extra, tool_step_sampling

TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]


def req(**kw):
    """A request as the web UI's saved sampler panel sends it (DRY on, repeat penalties on)."""
    base = dict(repeat_last_n=64, frequency_penalty=0.5, seed=7, dry_multiplier=0.6, dry_base=1.75,
                dry_allowed_length=2, dry_penalty_last_n=4096, dynatemp_range=0.5, dynatemp_exponent=1.0,
                presence_penalty=0.4, repeat_penalty=1.1)
    base.update(kw)
    return SimpleNamespace(**base)


class Helper(unittest.TestCase):
    def test_a_step_with_tools_runs_with_every_repeat_sampler_off(self):
        rep, pres, extra = tool_step_sampling(req(), TOOLS, 1.1)
        self.assertEqual((rep, pres), (1.0, 0.0))
        self.assertEqual(extra["dry_multiplier"], 0.0)
        self.assertEqual(extra["dynatemp_range"], 0.0)
        self.assertEqual(extra["frequency_penalty"], 0.0)

    def test_the_dry_tuning_that_would_matter_is_not_sent_at_all(self):
        _, _, extra = tool_step_sampling(req(), TOOLS, 1.1)
        for k in ("dry_base", "dry_allowed_length", "dry_penalty_last_n", "dynatemp_exponent", "repeat_last_n"):
            self.assertNotIn(k, extra)

    def test_unrelated_settings_survive(self):
        _, _, extra = tool_step_sampling(req(), TOOLS, 1.1)
        self.assertEqual(extra["seed"], 7)

    def test_the_zeros_are_explicit_even_when_the_panel_sent_nothing(self):
        bare = SimpleNamespace(**{k: None for k in SamplerFields.model_fields}, presence_penalty=None, repeat_penalty=None)
        _, _, extra = tool_step_sampling(bare, TOOLS, 1.0)
        self.assertEqual(extra, {"dry_multiplier": 0.0, "dynatemp_range": 0.0, "frequency_penalty": 0.0})

    def test_a_step_without_tools_keeps_the_users_sampling(self):
        r = req()
        rep, pres, extra = tool_step_sampling(r, None, 1.15)
        self.assertEqual((rep, pres), (1.15, 0.4))
        self.assertEqual(extra, sampler_extra(r))
        self.assertEqual(extra["dry_multiplier"], 0.6)
        self.assertEqual(tool_step_sampling(r, [], 1.15)[2], sampler_extra(r))

    def test_the_helper_never_changes_the_request(self):
        r = req()
        tool_step_sampling(r, TOOLS, 1.1)
        self.assertEqual((r.dry_multiplier, r.repeat_penalty, r.presence_penalty), (0.6, 1.1, 0.4))


class Wiring(unittest.TestCase):
    src = (Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py").read_text(encoding="utf-8")

    def test_both_tool_calling_sites_use_it(self):
        self.assertIn("tool_step_sampling(req, tools_for_lane, agent_rep)", self.src)
        self.assertIn("tool_step_sampling(req, esc_tools, agent_rep)", self.src)
        self.assertEqual(self.src.count("extra=step_extra"), 2)        # cloud and local call of the main step
        self.assertEqual(self.src.count("extra=esc_extra"), 2)         # cloud and local call of the escalation

    def test_only_the_plain_answer_calls_still_use_the_panel_directly(self):
        self.assertEqual(self.src.count("extra=sampler_extra(req)"), 2)   # the no-tools "direct" answer, cloud and local


class OnTheWire(unittest.TestCase):
    """What the model server actually receives."""

    def _payload(self, extra, rep, pres):
        from routes.common import llm_stream

        captured = {}

        class _Resp:
            status_code = 400
            async def aread(self): return b'{"error": {"message": "stop here"}}'

        class _Cm:
            async def __aenter__(self): return _Resp()
            async def __aexit__(self, *a): return False

        class _Client:
            is_cloud = False
            def stream(self, method, url, json=None, **kw):
                captured.update(json)
                return _Cm()

        fake = _Client()
        orig = llm_stream.state.client
        llm_stream.state.client = fake
        try:
            async def go():
                gen = llm_stream._llm_chat_stream_raw_inner(
                    fake, [{"role": "user", "content": "x"}], TOOLS, 0.4, 100, repeat_penalty=rep, extra=extra,
                    presence_penalty=pres)
                try:
                    async for _ in gen:
                        pass
                except Exception:
                    pass
            asyncio.run(go())
        finally:
            llm_stream.state.client = orig
        return captured

    def test_dry_and_penalties_are_zero_in_the_request_body_for_a_tool_step(self):
        rep, pres, extra = tool_step_sampling(req(), TOOLS, 1.1)
        body = self._payload(extra, rep, pres)
        self.assertEqual(body.get("dry_multiplier"), 0.0)
        self.assertEqual(body.get("repeat_penalty"), 1.0)
        self.assertEqual(body.get("presence_penalty"), 0.0)
        self.assertEqual(body.get("frequency_penalty"), 0.0)
        self.assertNotIn("dry_allowed_length", body)

    def test_the_panel_still_reaches_the_server_for_a_plain_answer(self):
        rep, pres, extra = tool_step_sampling(req(), None, 1.1)
        body = self._payload(extra, rep, pres)
        self.assertEqual(body.get("dry_multiplier"), 0.6)
        self.assertEqual(body.get("repeat_penalty"), 1.1)


if __name__ == "__main__":
    unittest.main()
