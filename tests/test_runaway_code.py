"""Runaway guard on code: a script that legitimately repeats a pipeline is not a loop; a cut call tells the model what
repeated and how to split a shell job; the loop guard reacts on the second identical cut."""
import unittest

from core.agent_loop import loop_guard
from core.agent_loop.degeneration import find_runaway, is_code_call, runaway_message


def _audit_script(n: int) -> str:
    # a system audit in PowerShell: every query ends with the same error handling and JSON conversion
    classes = ["OperatingSystem", "ComputerSystem", "Processor", "PhysicalMemory", "DiskDrive", "LogicalDisk",
               "NetworkAdapter", "BIOS", "VideoController", "Service", "Process", "StartupCommand"][:n]
    return "\n".join(f'$r["{c}"] = Get-CimInstance -ClassName Win32_{c} -ErrorAction SilentlyContinue '
                     f'| ConvertTo-Json -Depth 3 -Compress' for c in classes)


class RunawayOnCode(unittest.TestCase):
    def test_repeated_pipeline_in_a_script_is_not_cut(self):
        text = _audit_script(12)
        self.assertGreaterEqual(find_runaway(text), 0, "the prose rule alone treats this script as a loop")
        self.assertEqual(find_runaway(text, code=True), -1)

    def test_real_loop_in_code_still_cut(self):
        line = "Get-CimInstance Win32_Process | Select-Object Name, Id | Format-Table -AutoSize"
        self.assertGreaterEqual(find_runaway("\n".join([line] * 30), code=True), 0)      # identical lines
        block = "foo = compute_value(alpha, beta) + adjust(gamma)\nbar = other_value(delta) * 2\n"
        self.assertGreaterEqual(find_runaway(block * 40, code=True), 0)                  # a repeated block

    def test_code_call_detection(self):
        self.assertTrue(is_code_call("run_shell"))
        self.assertTrue(is_code_call("write_file", "scripts/audit.ps1"))
        self.assertFalse(is_code_call("write_file", "report.md"))

    def test_message_fits_the_tool_and_names_the_repeat(self):
        msg = runaway_message("run_shell", {"_runaway": True, "_repeated": "| Format-Table -AutoSize"})
        self.assertIn("short commands", msg)
        self.assertIn("Format-Table -AutoSize", msg)
        self.assertNotIn("project_append", msg)
        self.assertIn("project_append", runaway_message("write_file", {"_runaway": True}))

    def test_loop_guard_nudges_on_the_second_identical_cut(self):
        g = loop_guard.LoopGuard()
        args = {"_runaway": True, "_repeated": "x"}
        g.record("run_shell", args, runaway_message("run_shell", args), ok=False)
        self.assertEqual(g.check().level, loop_guard.OK)
        g.record("run_shell", args, runaway_message("run_shell", args), ok=False)
        self.assertEqual(g.check().level, loop_guard.NUDGE)


if __name__ == "__main__":
    unittest.main()
