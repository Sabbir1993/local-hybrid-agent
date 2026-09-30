"""A model that writes its tool call as prose must still act; prose that merely looks like code must not."""
import json
import unittest

from core.agent_loop import _extract_text_tool_calls
from core.agent_loop.narration import _is_narration

KNOWN = {"read_skill", "read_file", "list_files", "run_shell"}


def names(text, known=KNOWN):
    return [(c["function"]["name"], json.loads(c["function"]["arguments"]))
            for c in _extract_text_tool_calls(text, known)]


class ProseToolCallTests(unittest.TestCase):
    def test_the_reported_form(self):
        self.assertEqual(names('Tool Call: read_skill({"name": "webapp-testing"})'),
                         [("read_skill", {"name": "webapp-testing"})])

    def test_variants(self):
        self.assertEqual(names('tool_call: list_files({})'), [("list_files", {})])
        self.assertEqual(names('Calling read_file({"path": "a.py"})'), [("read_file", {"path": "a.py"})])
        self.assertEqual(names('**Tool Call:** `read_file`({"path": "a.py"})'), [("read_file", {"path": "a.py"})])
        self.assertEqual(names('Action: list_files()'), [("list_files", {})])
        self.assertEqual(names('read_file({"path": "x"})'), [("read_file", {"path": "x"})])   # a line of its own

    def test_multiline_arguments_and_a_lead_in_line(self):
        text = 'I will read it.\nTool Call: run_shell({\n  "cmd": "ls -la"\n})'
        self.assertEqual(names(text), [("run_shell", {"cmd": "ls -la"})])

    def test_unknown_tool_names_are_never_executed(self):
        self.assertEqual(names('Tool Call: format_disk({"drive": "C"})'), [])
        self.assertEqual(names('Calling delete_everything()'), [])

    def test_ordinary_prose_with_parentheses_is_not_a_call(self):
        for text in ("In Python you can use read_file(path) to load it, as shown in the docs.",
                     "The function list_files() returns names; call it when needed.",
                     "Use print(x) to debug."):
            self.assertEqual(names(text), [], text)

    def test_existing_formats_still_work(self):
        self.assertEqual(names('<tool_call>{"name": "read_file", "arguments": {"path": "a"}}</tool_call>'),
                         [("read_file", {"path": "a"})])

    def test_an_unparseable_tool_call_reply_is_retried_not_accepted_as_an_answer(self):
        self.assertTrue(_is_narration('Tool Call: some_unknown_tool({"x": 1})'))
        self.assertTrue(_is_narration("**Tool call:** read_skill(...)"))
        self.assertFalse(_is_narration("The tool call failed because the file was missing, so I used a default."))


if __name__ == "__main__":
    unittest.main()


class KeyValueFormatTests(unittest.TestCase):
    """The exact reply that ended a run: prose, then spawn_agent in <arg_key>/<arg_value> tags."""
    REPLY = ("The webapp-testing skill isn't available as a direct tool, but I can delegate this.\n\n"
             "Tool Call: spawn_agent<tool_call>spawn_agent<arg_key>task</arg_key><arg_value>You are testing "
             "a web app.\n1. Start the dev server: npm run dev\n2. Open http://localhost:5173</arg_value></tool_call>")

    def test_the_reported_reply_becomes_a_spawn_agent_call(self):
        calls = _extract_text_tool_calls(self.REPLY, {"spawn_agent", "read_file"})
        self.assertEqual([c["function"]["name"] for c in calls], ["spawn_agent"])
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertIn("npm run dev", args["task"])
        self.assertIn("localhost:5173", args["task"])

    def test_typed_values_and_a_missing_closing_tag(self):
        text = "<tool_call>read_file<arg_key>path</arg_key><arg_value>a.py</arg_value>" \
               "<arg_key>limit</arg_key><arg_value>50</arg_value><arg_key>opts</arg_key><arg_value>{\"x\": 1}</arg_value>"
        args = json.loads(_extract_text_tool_calls(text, {"read_file"})[0]["function"]["arguments"])
        self.assertEqual(args, {"path": "a.py", "limit": 50, "opts": {"x": 1}})

    def test_unknown_tool_in_this_format_is_ignored(self):
        text = "<tool_call>wipe<arg_key>x</arg_key><arg_value>1</arg_value></tool_call>"
        self.assertEqual(_extract_text_tool_calls(text, {"read_file"}), [])


class UnknownToolTests(unittest.TestCase):
    def _run(self, name):
        import asyncio
        from core.agent_loop.execution import _unknown_tool
        return _unknown_tool(name)

    def test_a_skill_called_as_a_tool_serves_the_skill(self):
        out = self._run("webapp-testing")
        self.assertTrue(out.startswith("note: 'webapp-testing' is a skill"))
        self.assertIn("# Skill: webapp-testing", out)

    def test_other_names_get_a_hint_not_a_bare_error(self):
        from core.registry import bootstrap_builtin_tools
        bootstrap_builtin_tools()
        out = self._run("read_fil")
        self.assertTrue(out.startswith("error: unknown tool read_fil."))
        self.assertIn("read_file", out)
