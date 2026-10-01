"""tests/test_llama_logging.py - the main llama-server's output is captured, not just printed.

Run: python -m unittest tests.test_llama_logging -v

The main model's stdout used to reach print() and nowhere else, so the OOM/allocator trace
from a failed load existed only in console scrollback and was gone on restart - while the
small-model lanes already kept a ring buffer and surfaced the error to the user
(core/small_model/instance.py::_exit_reason). This is that same capability for the main server.
"""

import collections
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import state as state_mod
from core.config import LOG_KEEP_DAYS, LOG_TAIL_LINES
from core.state import ProxyState


def a_state(tail=None):
    st = ProxyState.__new__(ProxyState)
    st._log_tasks = set()
    st._log_tail = collections.deque(maxlen=tail or LOG_TAIL_LINES)
    st._record_log = ProxyState._record_log.__get__(st)
    return st


class RingBuffer(unittest.TestCase):
    def test_lines_are_kept_in_order(self):
        st = a_state()
        for i in range(5):
            st._record_log(f"line {i}")
        self.assertEqual(st.log_tail(5), [f"line {i}" for i in range(5)])

    def test_it_is_bounded(self):
        st = a_state(tail=10)
        for i in range(100):
            st._record_log(f"line {i}")
        got = st.log_tail(1000)
        self.assertEqual(len(got), 10, "the ring must not grow without bound")
        self.assertEqual(got[-1], "line 99")

    def test_log_tail_returns_the_newest_n_oldest_first(self):
        st = a_state()
        for i in range(50):
            st._record_log(f"l{i}")
        self.assertEqual(st.log_tail(3), ["l47", "l48", "l49"])


class ExitReason(unittest.TestCase):
    """Same intent as the small-model helper, including the PAN mask."""

    OOM = "ggml_vram_reserve: failed to reserve 3.5 GiB of VRAM"

    def test_it_finds_the_last_error_line(self):
        st = a_state()
        st._log_tail.extend(["loading model", "ok", self.OOM, "shutting down"])
        self.assertEqual(st._exit_reason(), self.OOM)

    def test_no_errors_means_none(self):
        st = a_state()
        st._log_tail.extend(["loading model", "ok", "all slots ready"])
        self.assertIsNone(st._exit_reason())

    def test_it_matches_the_patterns_a_real_failure_produces(self):
        for line in ("error: failed to load model",
                     "mmap failed: cannot allocate memory",
                     "CUDA error: out of memory",
                     "ggml_backend_sched_eval: assert failed",
                     "llama_model_load: file not found"):
            st = a_state()
            st._log_tail.append(line)
            self.assertEqual(st._exit_reason(), line, f"not matched: {line}")

    def test_it_is_masked_for_card_numbers(self):
        """A log line is data. Anything that can reach a prompt or another user's view goes
        through the mask first -- same reason the small-model helper does it."""
        st = a_state()
        st._log_tail.append("error: card 4111 1111 1111 1111 declined")
        got = st._exit_reason()
        self.assertNotIn("4111 1111", got)
        self.assertIn("card", got)

    def test_it_is_bounded_in_length(self):
        st = a_state()
        st._log_tail.append("error: " + ("x" * 5000))
        self.assertLessEqual(len(st._exit_reason() or ""), 300)

    def test_whitespace_is_collapsed(self):
        st = a_state()
        st._log_tail.append("error:\n   failed   to   load\n  model")
        self.assertNotIn("\n", st._exit_reason())


class DailyLogFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dir = state_mod.LOG_DIR
        self._fh = state_mod._log_fh
        self._day = state_mod._log_day
        state_mod.LOG_DIR = self.tmp
        state_mod._log_fh = None
        state_mod._log_day = ""

    def tearDown(self):
        state_mod.close_log_file()
        state_mod.LOG_DIR = self._dir
        state_mod._log_fh = self._fh
        state_mod._log_day = self._day

    def test_lines_are_written_to_a_daily_file(self):
        state_mod._write_log_file("first")
        state_mod._write_log_file("second")
        files = list(self.tmp.glob("llama-server-*.log"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(encoding="utf-8").splitlines(),
                         ["first", "second"])

    def test_the_log_file_is_gitignored(self):
        ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/logs/", ignore, "machine-local diagnostics must not be committed")

    def test_old_days_are_pruned(self):
        import time as _t
        for d in range(LOG_KEEP_DAYS + 4):
            (self.tmp / f"llama-server-2026010{d % 9 + 1}-0{d}.log").write_text("old", encoding="utf-8")
        state_mod._write_log_file("today")
        kept = list(self.tmp.glob("llama-server-*.log"))
        self.assertLessEqual(len(kept), LOG_KEEP_DAYS + 1,
                             "old daily logs must be trimmed")

    def test_a_write_failure_never_raises(self):
        """A log that cannot be written must not take the model server down with it."""
        # a path far past Windows' MAX_PATH: the writer must swallow the failure.
        # NB parenthesised -- "/" and "*" share precedence and are left-associative, so
        # `tmp / "x" * 200` would parse as (tmp / "x") * 200.
        state_mod.LOG_DIR = self.tmp / ("a" * 300) / "deep" / ("x" * 200)
        state_mod._write_log_file("this should be swallowed")

    def test_writing_can_be_switched_off(self):
        with mock.patch.object(state_mod, "LOG_WRITE_FILE", False):
            state_mod._write_log_file("ignored")
        self.assertEqual(list(self.tmp.glob("*.log")), [])


class LoadFailureCarriesTheServersOwnWords(unittest.TestCase):
    """The whole point of keeping the buffer: "it didn't start" vs "mmap failed: no space"."""

    def _state(self):
        st = a_state()
        st.last_load_error = None
        return st

    def test_a_timeout_reports_what_llama_server_said(self):
        st = self._state()
        st._log_tail.append("ggml_vram_reserve: failed to reserve 3.5 GiB")
        err = st._load_failed("llama-server didn't become healthy within 120s")
        self.assertIn("failed to reserve", str(err))
        self.assertIn("didn't become healthy", str(err))
        self.assertIn("failed to reserve", st.last_load_error)

    def test_with_no_error_line_the_message_is_unchanged(self):
        st = self._state()
        st._log_tail.append("loading model, please wait")
        err = st._load_failed("llama-server didn't become healthy within 120s")
        self.assertNotIn("llama-server:", str(err))
        self.assertEqual(st.last_load_error, "llama-server didn't become healthy within 120s")

    def test_an_empty_buffer_does_not_append_a_dangling_separator(self):
        st = a_state()
        msg = str(st._load_failed("boom"))
        self.assertEqual(msg, "boom")


class StatusExposesTheTail(unittest.TestCase):
    def test_status_reports_the_tail(self):
        from pathlib import Path as P
        src = (P(__file__).resolve().parent.parent / "routes" / "control" / "status_endpoints.py"
               ).read_text(encoding="utf-8")
        self.assertIn("log_tail", src)
        self.assertIn("last_load_error", src)

    def test_shutdown_closes_the_log_file(self):
        from pathlib import Path as P
        src = (P(__file__).resolve().parent.parent / "core" / "startup.py").read_text(encoding="utf-8")
        self.assertIn("close_log_file", src)


if __name__ == "__main__":
    unittest.main()