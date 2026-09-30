"""Browser-test flow: the tool surface stays on across follow-up turns, and dev servers can be
started in the background instead of being killed by the synchronous shell timeout."""

import unittest

from core import tool_surface
from core.shell_tools import _background_args
from routes.agent.constants import EXECUTOR_TEST_TOOLS


def _tool(name):
    return {"type": "function", "function": {"name": name}}


class SurfaceQueryTests(unittest.TestCase):
    def test_follow_up_keeps_browser_family(self):
        msgs = [{"role": "user", "content": "can you test this app on my browser?"},
                {"role": "assistant", "content": "I found 2 issues."},
                {"role": "user", "content": "yes go ahead and fix them"}]
        q = tool_surface.surface_query(msgs)
        self.assertIn("browser", tool_surface.needed_families(q))
        # the old behaviour (last message only) dropped it
        self.assertNotIn("browser", tool_surface.needed_families("yes go ahead and fix them"))

    def test_used_tools_keep_their_family(self):
        msgs = [{"role": "user", "content": "one"}, {"role": "user", "content": "two"},
                {"role": "user", "content": "three"}, {"role": "user", "content": "four"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "browser_navigate", "arguments": "{}"}}]},
                {"role": "user", "content": "carry on"}]
        self.assertIn("browser", tool_surface.needed_families(tool_surface.surface_query(msgs)))

    def test_control_turns_are_ignored(self):
        msgs = [{"role": "user", "content": "check the page in the browser"},
                {"role": "user", "content": "[continue] call the tool"}]
        q = tool_surface.surface_query(msgs)
        self.assertNotIn("[continue]", q)
        self.assertIn("browser", tool_surface.needed_families(q))

    def test_unrelated_conversation_stays_core(self):
        msgs = [{"role": "user", "content": "rename this variable"}]
        self.assertEqual(tool_surface.needed_families(tool_surface.surface_query(msgs)), set())

    def test_multimodal_content(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "open my website"}]}]
        self.assertIn("browser", tool_surface.needed_families(tool_surface.surface_query(msgs)))

    def test_executor_gets_the_tools_the_skill_uses(self):
        for name in ("browser_wait", "browser_press", "browser_select", "browser_close"):
            self.assertIn(name, EXECUTOR_TEST_TOOLS)


class BackgroundShellTests(unittest.TestCase):
    def test_plain_call_is_not_background(self):
        self.assertEqual(_background_args({"command": "npm test"}), {})

    def test_background_with_port(self):
        self.assertEqual(_background_args({"command": "npm run dev", "background": True, "wait_for_port": "5173"}),
                         {"background": True, "wait_for_port": 5173, "wait": 30})

    def test_string_true_and_bad_values(self):
        self.assertEqual(_background_args({"background": "true", "wait_for_port": "abc", "wait": 999}),
                         {"background": True, "wait": 120})
        self.assertEqual(_background_args({"background": "false"}), {})
        self.assertEqual(_background_args({"background": True, "wait_for_port": 70000})["background"], True)
        self.assertNotIn("wait_for_port", _background_args({"background": True, "wait_for_port": 70000}))


if __name__ == "__main__":
    unittest.main()
