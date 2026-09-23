"""tests/test_kb_residency.py - knowledge base never reaches cloud lanes.

Run: python -m unittest tests.test_kb_residency -v
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import knowledge_access as ka
from core import agent_tools
from core.small_model import APP_CONFIG


class KBResidencyTests(unittest.TestCase):
    def setUp(self):
        self._cfg = APP_CONFIG.get("knowledge")

    def tearDown(self):
        if self._cfg is None:
            APP_CONFIG.pop("knowledge", None)
        else:
            APP_CONFIG["knowledge"] = self._cfg

    def test_tool_refuses_when_cloud_lane_active(self):
        APP_CONFIG["knowledge"] = {"cloud_policy": "local_only"}

        async def main():
            self.assertTrue(ka.set_kb_cloud_blocked(True))
            return await agent_tools.tool_search_knowledge_base({"query": "salary of employee"})

        self.assertEqual(asyncio.run(main()), ka.KB_CLOUD_BLOCKED_MSG)

    def test_local_lane_not_blocked(self):
        APP_CONFIG["knowledge"] = {"cloud_policy": "local_only"}

        async def main():
            return ka.set_kb_cloud_blocked(False), ka.kb_cloud_blocked()

        self.assertEqual(asyncio.run(main()), (False, False))

    def test_allow_policy_disables_block(self):
        APP_CONFIG["knowledge"] = {"cloud_policy": "allow"}

        async def main():
            return ka.set_kb_cloud_blocked(True)

        self.assertFalse(asyncio.run(main()))

    def test_default_policy_is_local_only(self):
        APP_CONFIG.pop("knowledge", None)
        self.assertTrue(ka.kb_local_only())

    def test_block_is_per_request(self):
        APP_CONFIG["knowledge"] = {"cloud_policy": "local_only"}

        async def req(cloud):
            ka.set_kb_cloud_blocked(cloud)
            await asyncio.sleep(0.01)
            return ka.kb_cloud_blocked()

        async def main():
            return await asyncio.gather(req(True), req(False))

        self.assertEqual(asyncio.run(main()), [True, False])


if __name__ == "__main__":
    unittest.main()
