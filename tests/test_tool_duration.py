"""tests/test_tool_duration.py - per-tool wall duration in route telemetry.

RED for P4 (remote/MCP read-only batching): the plan's reopen condition is
"only when step latency becomes dominated by tool time" -- but route_events
reason='tool' rows never recorded duration_s, so that condition is permanently
unmeasurable. The population census on this machine found this today:
3621 tool calls, ZERO mcp__ calls, and all 240 multi-call read-only steps
already served by the existing PARALLEL_READ_TOOLS gather fast path
(web_search/web_fetch included) in routes/agent/stream.py. Batch-parallel
dispatch for a population of zero is speculative infrastructure -- not built.
What IS missing, and measured-first, is the instrument that would ever prove
or disprove the reopen condition.

Scope: pure telemetry. A tool's route_events row gains its wall duration,
measured around the actual tool run (not approval-card user wait time), for
both dispatch paths -- the sequential loop and the pre-existing all-read
gather fast path (per-call timers there, since calls complete out of order).
"""
import re
import unittest
from pathlib import Path

STREAM = Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py"

_NL = "\r?\n"


def _call_extent(src: str, start: int) -> str:
    """The complete (balanced-paren) route_log.event(...) call starting at `start`."""
    depth = 0
    i = src.index("(", start)
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    return src[start:]


class ToolDurationWiringTests(unittest.TestCase):
    def setUp(self):
        self.src = STREAM.read_text(encoding="utf-8")

    def _full_calls(self, signature: str):
        """Every complete route_log.event call whose header contains `signature`."""
        out = []
        for m in re.finditer(r"route_log\.event\(", self.src):
            call = _call_extent(self.src, m.start())
            if signature in call.split("\n")[0] + call:
                out.append(call)
        return out

    def test_tool_event_records_duration(self):
        calls = [c for c in self._full_calls('lane_name, "tool"')
                 if "tool_err=None if ok else" in c]
        self.assertEqual(
            len(calls), 1,
            f"expected exactly one sequential-gauntlet tool event, found {len(calls)}")
        self.assertIn(
            "duration_s", calls[0],
            '"tool" telemetry rows are emitted without duration_s - the '
            '"tool time dominates step latency" reopen condition is unmeasurable')

    def test_sequential_run_is_timed(self):
        # a timer starts immediately before the run branch and feeds duration_s
        self.assertRegex(
            self.src,
            rf"_t0\s*=\s*time\.perf_counter\(\)(?:[ \t]*(?:#[^\n\r]*)?)?{_NL}\s+if tc_id in parallel_results:",
            "no perf_counter timer around the sequential run_tool branch")

    def test_fast_path_gather_times_each_call(self):
        # gather completes out of order: per-call timers, not one shared start
        self.assertIn("async def _timed", self.src,
                      "no per-call timing wrapper for the all-read gather fast path")
        self.assertRegex(
            self.src,
            r"valid_coros\.append\(_timed\(",
            "the gather fast path does not wrap its coros with the timing wrapper")
        self.assertIn("parallel_dur", self.src,
                      "fast-path durations are collected nowhere")
        # the result AND its duration are stored together, keyed by call id
        self.assertRegex(
            self.src,
            rf"parallel_results\[tid\]\s*=\s*\w+{_NL}\s*parallel_dur\[tid\]\s*=\s*\w+",
            "fast-path timing results are not stored per call id")

    def test_duration_feeds_the_event_from_the_right_place(self):
        calls = [c for c in self._full_calls('lane_name, "tool"')
                 if "tool_err=None if ok else" in c]
        self.assertEqual(len(calls), 1)
        m = re.search(r"duration_s=(.+)", calls[0], re.S)
        self.assertIsNotNone(m)
        self.assertIn("tc_id", m.group(1),
                      "duration expression ignores the fast path (parallel_dur)")


if __name__ == "__main__":
    unittest.main()
