"""/parallel <task> always fans out: the first main-lane step offers only spawn_parallel_agents with a call required,
for at most MAX_FORCED_STEPS steps, and never in the Personal Agent (which blocks the spawn tools)."""
import unittest
from pathlib import Path

from core.agent_loop import parallel_cmd


class Parse(unittest.TestCase):
    def test_command_is_recognised_and_stripped(self):
        self.assertEqual(parallel_cmd.parse("/parallel review core and routes"), (True, "review core and routes"))
        self.assertEqual(parallel_cmd.parse("  /PARALLEL\nreview it"), (True, "review it"))

    def test_other_text_is_left_alone(self):
        for t in ("review /parallel later", "/parallelize x", "hello", None, ["x"]):
            self.assertFalse(parallel_cmd.parse(t)[0], t)

    def test_instruction_names_the_tool_and_keeps_the_task(self):
        msg = parallel_cmd.instruction("audit the auth module")
        self.assertIn("spawn_parallel_agents", msg)
        self.assertTrue(msg.endswith("Task: audit the auth module"))


class ForcedStep(unittest.TestCase):
    def test_forced_until_the_fan_out_happens(self):
        self.assertTrue(parallel_cmd.wants_forced_step(True, [], 0))
        self.assertTrue(parallel_cmd.wants_forced_step(True, [{"name": "read_file"}], 1))
        self.assertFalse(parallel_cmd.wants_forced_step(True, [{"name": "spawn_parallel_agents"}], 1))

    def test_gives_up_after_the_cap_and_never_when_off(self):
        self.assertFalse(parallel_cmd.wants_forced_step(True, [], parallel_cmd.MAX_FORCED_STEPS))
        self.assertFalse(parallel_cmd.wants_forced_step(False, [], 0))


class Wiring(unittest.TestCase):
    """Source checks, like test_project_builder: the loop is too large to drive here."""
    SRC = (Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py").read_text(encoding="utf-8")
    SETUP = (Path(__file__).resolve().parents[1] / "routes" / "agent" / "setup.py").read_text(encoding="utf-8")

    def test_forced_step_offers_one_tool_and_requires_a_call(self):
        self.assertIn('tools_for_lane, step_tool_choice, step_grammar = _par_tools, "required", None', self.SRC)
        self.assertIn("parallel_cmd.TOOL not in (hidden_tools or ())", self.SRC)

    def test_personal_agent_never_fans_out(self):
        self.assertIn("ctx.parallel = not ctx.personal", self.SETUP)


if __name__ == "__main__":
    unittest.main()
