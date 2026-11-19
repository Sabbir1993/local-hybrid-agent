"""A custom agent bound to a device runs only from that device (core/request_context.agent_device_refusal)."""
import unittest

from core.request_context import agent_device_refusal, normalize_device_id


class AgentDeviceRefusalTest(unittest.TestCase):
    def test_unbound_agent_runs_anywhere(self):
        self.assertIsNone(agent_device_refusal({"device_id": ""}, None))
        self.assertIsNone(agent_device_refusal({}, "dev_abc"))
        self.assertIsNone(agent_device_refusal(None, None))

    def test_same_device_runs(self):
        self.assertIsNone(agent_device_refusal({"device_id": "dev_abc"}, "dev_abc"))

    def test_hardware_id_matches_its_hash(self):
        raw = "dev_win_1a2b3c"
        self.assertIsNone(agent_device_refusal({"device_id": normalize_device_id(raw)}, raw))

    def test_other_device_refused(self):
        err = agent_device_refusal({"device_id": "dev_abc", "device_name": "Windows PC-AAA"}, "dev_xyz")
        self.assertEqual(err["error"], "custom_agent_other_device")
        self.assertIn("Windows PC-AAA", err["message"])

    def test_missing_header_refused(self):
        # before: a request without X-Device-Id skipped the check and could run any bound agent
        for header in (None, "", "   "):
            err = agent_device_refusal({"device_id": "dev_abc"}, header)
            self.assertEqual(err["error"], "custom_agent_device_unknown")


if __name__ == "__main__":
    unittest.main()
