import unittest

from core.agent_loop import executor_view as ev

SYS = {"role": "system", "content": "SYSTEM PROMPT"}


def u(t):
    return {"role": "user", "content": t}


def a(t):
    return {"role": "assistant", "content": t}


def call(name="read_file"):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": "{}"}}]}


def tool(t):
    return {"role": "tool", "tool_call_id": "c1", "content": t}


class ExecutorViewTests(unittest.TestCase):
    def test_single_request_is_unchanged(self):
        msgs = [SYS, u("test my app")]
        self.assertEqual(ev.build_executor_view(msgs), msgs)

    def test_old_tool_output_and_long_history_are_dropped(self):
        msgs = [SYS, u("first task"), call(), tool("X" * 5000), a("done with first"),
                u("second task"), a("did second"), u("third task"), a("did third"), u("now check the app")]
        view = ev.build_executor_view(msgs)
        self.assertEqual(view[0], SYS)
        self.assertEqual(view[-1], u("now check the app"))
        self.assertFalse(any(m.get("role") == "tool" for m in view))
        self.assertFalse(any(m.get("tool_calls") for m in view))
        prior = view[1:-1]
        self.assertLessEqual(len(prior), ev.PRIOR_TURNS)
        self.assertEqual(prior[0]["role"], "user")          # roles keep alternating

    def test_prior_turns_are_clipped(self):
        msgs = [SYS, u("A" * 3000), a("B" * 3000), u("go")]
        view = ev.build_executor_view(msgs)
        for m in view[1:-1]:
            self.assertLessEqual(len(m["content"]), ev.PRIOR_CHARS + 3)

    def test_current_run_steps_are_kept_and_big_tool_results_cut(self):
        msgs = [SYS, u("old"), a("old answer"), u("check the app"), call(), tool("Y" * 9000),
                call("run_shell"), tool("short")]
        view = ev.build_executor_view(msgs)
        tools = [m for m in view if m.get("role") == "tool"]
        self.assertEqual(len(tools), 2)
        self.assertLess(len(tools[0]["content"]), ev.TOOL_RESULT_CHARS + 600)
        self.assertIn("not a display problem", tools[0]["content"])   # a small model must not blame the screen
        self.assertEqual(tools[1]["content"], "short")
        self.assertEqual(sum(1 for m in view if m.get("tool_calls")), 2)   # call/reply pairs intact

    def test_cut_tool_result_keeps_its_end(self):
        msgs = [SYS, u("go"), call(), tool("HEAD-" + "m" * 30000 + "-TAIL")]
        body = [m for m in ev.build_executor_view(msgs) if m.get("role") == "tool"][0]["content"]
        self.assertTrue(body.startswith("HEAD-"))
        self.assertTrue(body.endswith("-TAIL"))

    def test_loop_injected_control_turns_do_not_start_a_new_request(self):
        msgs = [SYS, u("old"), a("ok"), u("check the app"), a("Let me look."),
                u("[continue] You described the next step but did not call a tool.")]
        view = ev.build_executor_view(msgs)
        self.assertIn(u("check the app"), view)
        self.assertEqual(view[-1]["content"][:10], "[continue]")

    def test_multimodal_prior_message_becomes_text(self):
        msgs = [SYS, {"role": "user", "content": [{"type": "text", "text": "look"},
                                                  {"type": "image_url", "image_url": {"url": "data:..."}}]},
                a("a cat"), u("what next")]
        view = ev.build_executor_view(msgs)
        self.assertEqual(view[1]["content"], "look [image]")

    def test_full_history_is_not_modified(self):
        msgs = [SYS, u("x"), call(), tool("Z" * 5000), a("y"), u("go")]
        snapshot = [dict(m) for m in msgs]
        ev.build_executor_view(msgs)
        self.assertEqual(msgs, snapshot)


if __name__ == "__main__":
    unittest.main()
