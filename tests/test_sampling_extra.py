import unittest

from core.cloud.models import _LLAMA_ONLY_KEYS
from routes.agent.models import AgentRequest
from routes.chat.models import ChatRunRequest
from routes.common.sampling_extra import sampler_extra


class SamplerExtraTests(unittest.TestCase):
    def test_nothing_sent_means_no_extra(self):
        self.assertIsNone(sampler_extra(ChatRunRequest(messages=[])))

    def test_off_values_are_omitted(self):
        req = ChatRunRequest(messages=[], repeat_last_n=64, frequency_penalty=0.0, seed=-1,
                             dry_multiplier=0.0, dry_base=1.75, dry_allowed_length=2, dry_penalty_last_n=4096,
                             dynatemp_range=0.0, dynatemp_exponent=1.0)
        self.assertEqual(sampler_extra(req), {"repeat_last_n": 64, "frequency_penalty": 0.0})

    def test_enabled_values_are_sent_and_clamped(self):
        req = AgentRequest(messages=[], seed=7, dry_multiplier=99, dry_base=1.75, dry_allowed_length=2,
                           dry_penalty_last_n=4096, dynatemp_range=0.1, dynatemp_exponent=1.0)
        out = sampler_extra(req)
        self.assertEqual(out["seed"], 7)
        self.assertEqual(out["dry_multiplier"], 5.0)
        self.assertEqual(out["dry_penalty_last_n"], 4096)
        self.assertEqual(out["dynatemp_range"], 0.1)

    def test_llama_only_keys_are_stripped_for_cloud(self):
        for k in ("repeat_last_n", "dry_multiplier", "dry_base", "dry_allowed_length",
                  "dry_penalty_last_n", "dynatemp_range", "dynatemp_exponent"):
            self.assertIn(k, _LLAMA_ONLY_KEYS)


if __name__ == "__main__":
    unittest.main()
