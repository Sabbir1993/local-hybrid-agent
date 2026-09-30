import json
import unittest

from core import router_policy as rp
from core import step_outcome as so
from core import lane_health as lh
from core.agent_loop.finish import apply_finish, without_finish


def call(name, **args):
    return {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ApplyFinishTests(unittest.TestCase):
    def test_finish_alone_becomes_the_final_reply(self):
        content, calls, done = apply_finish("", [call("finish", answer="All 3 checks passed.")])
        self.assertEqual((content, calls, done), ("All 3 checks passed.", [], True))

    def test_finish_with_other_calls_is_dropped_so_the_work_happens_first(self):
        content, calls, done = apply_finish("text", [call("run_shell", cmd="ls"), call("finish", answer="x")])
        self.assertFalse(done)
        self.assertEqual([c["function"]["name"] for c in calls], ["run_shell"])
        self.assertEqual(content, "text")

    def test_no_finish_is_untouched(self):
        calls = [call("read_file", path="a")]
        self.assertEqual(apply_finish("hi", calls), ("hi", calls, False))
        self.assertEqual(apply_finish("hi", None), ("hi", [], False))

    def test_dict_arguments_and_bad_json(self):
        c = {"function": {"name": "finish", "arguments": {"answer": "ok"}}}
        self.assertEqual(apply_finish("", [c])[0], "ok")
        bad = {"function": {"name": "finish", "arguments": "not json"}}
        self.assertEqual(apply_finish("", [bad])[0], "not json")
        self.assertEqual(apply_finish("", [call("finish")])[0], "")   # empty answer -> empty reply

    def test_without_finish(self):
        tools = [{"function": {"name": "finish"}}, {"function": {"name": "read_file"}}]
        self.assertEqual([t["function"]["name"] for t in without_finish(tools)], ["read_file"])
        self.assertEqual(without_finish(None), None)


class FinishInThePolicyTests(unittest.TestCase):
    Q = "can you test this app on my browser ? and check is there any issues"

    def test_outcome_codes(self):
        self.assertEqual(so.classify_step("done", [], finished=True), so.FINISH)
        self.assertEqual(so.classify_step("", [], finished=True), so.EMPTY)

    def test_finish_before_any_work_on_an_action_request_is_still_a_failed_turn_and_escalates(self):
        self.assertFalse(lh.turn_ok(so.FINISH, "action", 0, False))
        self.assertTrue(lh.turn_ok(so.FINISH, "action", 5, False))         # after real work
        self.assertTrue(lh.turn_ok(so.FINISH, "question", 0, False))
        reason = rp.escalate_reason(step=0, content="Everything looks fine.", tool_calls=[],
                                    query=self.Q, is_loop=False)
        self.assertEqual(reason, "no_tool_call")

    def test_registered_as_a_builtin_tool(self):
        from core.agent_tools import AGENT_TOOLS, TOOL_IMPLS
        self.assertIn("finish", TOOL_IMPLS)
        self.assertEqual(TOOL_IMPLS["finish"]({"answer": " done "}), "done")
        self.assertIn("finish", [t["function"]["name"] for t in AGENT_TOOLS])


if __name__ == "__main__":
    unittest.main()
