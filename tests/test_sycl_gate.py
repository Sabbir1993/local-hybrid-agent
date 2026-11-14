import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sycl_gate import launch_gate


class SyclGate(unittest.TestCase):
    def _run(self, backend):
        order = []

        async def job(n):
            async with launch_gate(backend):
                order.append(("in", n))
                await asyncio.sleep(0.03)
                order.append(("out", n))

        async def main():
            await asyncio.gather(job(1), job(2), job(3))
        asyncio.run(main())
        return order

    def test_sycl_starts_one_at_a_time(self):
        o = self._run("sycl")
        for i in range(0, 6, 2):
            self.assertEqual(o[i][0], "in")
            self.assertEqual(o[i + 1], ("out", o[i][1]))

    def test_other_backends_are_not_gated(self):
        o = self._run("vulkan")
        self.assertEqual([k for k, _ in o[:3]], ["in", "in", "in"])


if __name__ == "__main__":
    unittest.main()
