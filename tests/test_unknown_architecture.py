"""A model whose architecture the installed llama.cpp does not know is reported as that, not as a "bad path or VRAM" problem.

Run: python -m unittest tests.test_unknown_architecture -v
"""
import asyncio
import collections
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import ProxyState

LOG = ["0.00.222.802 E llama_model_load: error loading model: unknown model architecture: 'flint'",
       "0.00.304.082 E srv  llama_server: exiting due to model loading error"]


def _state(lines):
    st = ProxyState.__new__(ProxyState)
    st._log_tail = collections.deque(lines, maxlen=50)
    return st


class UnknownArchitectureTests(unittest.TestCase):
    def test_the_architecture_is_read_from_the_log(self):
        self.assertEqual(_state(LOG)._unknown_architecture(), "flint")

    def test_other_failures_are_not_mistaken_for_it(self):
        self.assertIsNone(_state(["E ggml_vulkan: out of memory", "E srv exiting due to model loading error"])._unknown_architecture())

    def test_the_load_error_names_the_architecture(self):
        st = _state(LOG)
        st.process = mock.Mock(); st.process.poll.return_value = 1; st.process.returncode = 1
        st.profile = None
        st._kill_loading_process = lambda: None
        st._load_failed = lambda msg: RuntimeError(msg)
        with mock.patch("core.state.health_timeout_s", return_value=5):
            with self.assertRaises(RuntimeError) as cm:
                asyncio.run(st._wait_healthy())
        text = str(cm.exception)
        self.assertIn("'flint' architecture", text)
        self.assertIn("not a path or VRAM problem", text)


if __name__ == "__main__":
    unittest.main()
