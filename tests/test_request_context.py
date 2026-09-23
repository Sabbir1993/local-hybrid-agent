"""tests/test_request_context.py - per-request user/device isolation.

Run: python -m unittest tests.test_request_context -v
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import request_context as rc


class RequestContextIsolation(unittest.TestCase):
    def test_concurrent_tasks_do_not_share_user(self):
        seen = {}

        async def request(uid, dev):
            rc.set_current_user(uid)
            rc.set_current_device(dev)
            await asyncio.sleep(0.01)          # let the other "request" run
            # a sync tool dispatched to the thread pool must see the same user
            seen[uid] = await rc.run_in_executor_ctx(
                lambda: (rc.get_current_user_id(), rc.get_current_device_id()))

        async def main():
            await asyncio.gather(request(1, "dev-a"), request(2, "dev-b"))

        asyncio.run(main())
        self.assertEqual(seen[1], (1, "dev-a"))
        self.assertEqual(seen[2], (2, "dev-b"))

    def test_default_device(self):
        async def main():
            return rc.get_current_user_id(), rc.get_current_device_id()

        self.assertEqual(asyncio.run(main()), (None, "default"))

    def test_plan_context_isolated(self):
        from core import agent_tools

        async def request(sid):
            agent_tools.set_plan_context(sid)
            await asyncio.sleep(0.01)
            return agent_tools.get_plan_context()

        async def main():
            return await asyncio.gather(request(10), request(20))

        self.assertEqual(asyncio.run(main()), [10, 20])


if __name__ == "__main__":
    unittest.main()
