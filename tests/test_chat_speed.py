"""The reply badge's speed comes from llama-server's own timings, summed over the model calls of one reply."""
import unittest

from routes.chat.speed import SpeedTally


class SpeedTallyTests(unittest.TestCase):
    def test_generation_and_prompt_speed_over_two_calls(self):
        s = SpeedTally()
        s.add({"timings": {"predicted_n": 100, "predicted_ms": 4000, "prompt_n": 3000, "prompt_ms": 1500}})
        s.add({"timings": {"predicted_n": 50, "predicted_ms": 2000, "prompt_n": 500, "prompt_ms": 250}})
        self.assertEqual(s.gen_tps(), 25.0)          # 150 tokens in 6 s, not tokens / whole request time
        self.assertEqual(s.prompt_tps(), 2000.0)     # 3500 in 1.75 s
        # cache_tokens / read_tokens: the first call of the reply (how much of the prompt the server had cached)
        self.assertEqual(s.as_event(), {"gen_tps": 25.0, "prompt_tps": 2000.0, "cache_tokens": 0, "read_tokens": 3000})

    def test_cache_reuse_is_taken_from_the_first_call(self):
        s = SpeedTally()
        s.add({"timings": {"predicted_n": 10, "predicted_ms": 500, "prompt_n": 120, "prompt_ms": 60, "cache_n": 4200}})
        s.add({"timings": {"predicted_n": 10, "predicted_ms": 500, "prompt_n": 900, "prompt_ms": 400, "cache_n": 4330}})
        self.assertEqual((s.first_cached, s.first_read), (4200, 120))

    def test_draft_acceptance(self):
        s = SpeedTally()
        s.add({"timings": {"predicted_n": 40, "predicted_ms": 1000, "draft_n": 30, "draft_n_accepted": 21}})
        self.assertEqual(s.draft_accept(), 0.7)

    def test_no_timings_means_no_figures(self):
        s = SpeedTally()
        for res in (None, {}, {"usage": {"completion_tokens": 5}}, {"timings": {"predicted_n": "x"}}):
            s.add(res)
        self.assertIsNone(s.gen_tps())
        self.assertEqual(s.as_event(), {})


if __name__ == "__main__":
    unittest.main()
