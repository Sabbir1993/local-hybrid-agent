"""A chat picks its own model: the choice lives in the request (cloud.request_main), not in the saved lane binding."""
import asyncio
import unittest
from unittest import mock

from core import cloud
from core.cloud import lanes

CFG = {
    "provider": {"p": {"options": {"baseURL": "http://x/v1"}, "models": {"a": {}, "b": {}}}},
    "cloud": {"main": "p/a", "routing_mode": "auto"},
}


class RequestMainOverride(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(lanes, "_merged", lambda uid=None: CFG)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_default_is_the_saved_binding(self):
        self.assertEqual(cloud.cloud_lane("main", 1).key, "p/a")

    def test_override_wins_and_executor_follows_in_auto(self):
        def run():
            cloud.request_main.set("p/b")
            return cloud.cloud_lane("main", 1).key, cloud.cloud_lane("executor", 1).key
        self.assertEqual(self._in_ctx(run), ("p/b", "p/b"))

    def test_local_forces_the_local_model(self):
        def run():
            cloud.request_main.set("local")
            return cloud.cloud_lane("main", 1), cloud.cloud_lane("executor", 1)
        self.assertEqual(self._in_ctx(run), (None, None))

    def test_two_concurrent_requests_do_not_see_each_other(self):
        async def one(key, pause):
            cloud.request_main.set(key)
            await asyncio.sleep(pause)
            return cloud.cloud_lane("main", 1).key

        async def both():
            return await asyncio.gather(one("p/a", 0.02), one("p/b", 0.0))
        self.assertEqual(asyncio.run(both()), ["p/a", "p/b"])

    def test_saved_binding_is_untouched(self):
        self._in_ctx(lambda: cloud.request_main.set("p/b"))
        self.assertEqual(CFG["cloud"]["main"], "p/a")
        self.assertEqual(cloud.cloud_lane("main", 1).key, "p/a")

    @staticmethod
    def _in_ctx(fn):
        import contextvars
        return contextvars.copy_context().run(fn)


if __name__ == "__main__":
    unittest.main()
