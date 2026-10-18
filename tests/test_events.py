"""tests/test_events.py - the operator lifecycle event log.

Background: the supervisor (core/state.py) and the autotune sweep printed
their decisions to console only, so "why did llama-server restart 7 times
at 3am" answered to process exit. log_event appends bounded JSON lines to
logs/events-YYYYMMDD.jsonl: queryable, day-rotated, pruned, never raises,
and fields are truncated so nothing can smuggle a log dump or user text
into retained storage."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.events as events_mod
from core.events import log_event


class BoundedJsonl(unittest.TestCase):
    def setUp(self):
        # module-level handle state must not leak between tests: each test's
        # LOG_DIR is a different tempdir, and a live handle would write into
        # the previous test's directory
        events_mod._fh = None
        events_mod._day = ""

    def tearDown(self):
        events_mod.close()

    def _tmp(self):
        d = tempfile.mkdtemp(prefix="events-test-")
        return mock.patch.object(events_mod, "LOG_DIR", Path(d))

    def _ctx(self):
        d = tempfile.mkdtemp(prefix="events-test-")
        return d

    def _read_lines(self, tmpdir: Path):
        f = next(iter(sorted(tmpdir.glob("events-*.jsonl"))))
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]

    def test_record_shape(self):
        d = tempfile.mkdtemp(prefix="events-test-")
        with mock.patch.object(events_mod, "LOG_DIR", Path(d)):
            log_event("server.restart", restart=2, code=1, reason="out of memory")
            lines = self._read_lines(Path(d))
        self.assertEqual(len(lines), 1)
        r = lines[0]
        self.assertEqual(r["kind"], "server.restart")
        self.assertEqual(r["restart"], 2)
        self.assertEqual(r["code"], 1)
        self.assertIn("ts", r)
        self.assertEqual(r["reason"], "out of memory")

    def test_long_strings_are_truncated(self):
        d = self._ctx()
        with mock.patch.object(events_mod, "LOG_DIR", Path(d)):
            log_event("server.load_failed", reason="x" * 5000)
            r = self._read_lines(Path(d))[0]
        self.assertEqual(len(r["reason"]), 200)

    def test_odd_types_are_stringified_not_skipped(self):
        d = self._ctx()
        with mock.patch.object(events_mod, "LOG_DIR", Path(d)):
            log_event("autotune.winner", profile={"a": 1})
            r = self._read_lines(Path(d))[0]
        self.assertIsInstance(r["profile"], str)
        self.assertIn('"a": 1', r["profile"])

    def test_bad_kind_is_dropped(self):
        d = self._ctx()
        with mock.patch.object(events_mod, "LOG_DIR", Path(d)):
            log_event("", restart=1)
            log_event("bad kind!", restart=1)
            log_event("../escape", restart=1)
            log_event("server.restart", restart=1)
            lines = self._read_lines(Path(d))
        self.assertEqual([r["kind"] for r in lines], ["server.restart"])

    def test_never_raises_even_if_the_file_fails(self):
        d = self._ctx()
        with mock.patch.object(events_mod, "LOG_DIR", Path(d)), \
             mock.patch("builtins.open", side_effect=OSError("disk full")):
            log_event("server.restart", restart=1)   # must not raise
            log_event("server.healthy")              # must not raise

    def test_write_file_false_writes_nothing(self):
        d = self._ctx()
        with mock.patch.object(events_mod, "LOG_DIR", Path(d)), \
             mock.patch.object(events_mod, "LOG_WRITE_FILE", False):
            log_event("server.restart", restart=1)
        self.assertEqual(list(Path(d).glob("events-*.jsonl")), [])

    def test_day_rotation_uses_one_file_per_day(self):
        d = self._ctx()
        with mock.patch.object(events_mod, "LOG_DIR", Path(d)):
            log_event("server.restart")
            # mock the time module the module sees, not the shared real one
            fake = type("FakeTime", (), {"strftime": staticmethod(
                lambda f: "20990102" if f == "%Y%m%d" else "2099-01-02T00:00:00")})
            with mock.patch.object(events_mod, "time", fake):
                # new day: the handle reopens into the rotated file
                log_event("server.healthy")
            files = sorted(Path(d).glob("events-*.jsonl"))
        self.assertEqual(len(files), 2)


if __name__ == "__main__":
    unittest.main()
