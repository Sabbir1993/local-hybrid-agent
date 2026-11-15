"""An edit whose old_string was not copied from the file must not be retried blind: the read has to be real."""
import asyncio
import unittest
from unittest import mock

from core.agent_tools import file_ops, file_state, verify_loop
from tests.test_file_not_found_hint import FOLDER, FakeCompanion

REAL_LINE = '<div class="sub">Last boot: 2026-10-15 → 2026-12-01</div>'
INVENTED = '<div class="sub">Last boot: 2026-59-38 → 2026-lorem</div>'     # what the model typed from memory
PAGE = ('<html><body>\n<div class="sub">37.72 / 43.26 GB committed · paging active</div>\n'
        f'{REAL_LINE}\n</body></html>\n')


class StaleEdit(unittest.TestCase):
    def setUp(self):
        file_state.reset_session()
        self.addCleanup(file_state.reset_session)
        self.fake = FakeCompanion({"system_report_2.html": PAGE})
        file_state.mark_written(str(FOLDER / "system_report_2.html"), created=True)
        file_state.mark_read(str(FOLDER / "system_report_2.html"))          # what write_file leaves behind

    def run_calls(self, *calls):
        async def go():
            return [await getattr(file_ops, f"tool_{name}")(args) for name, args in calls]

        with mock.patch("core.agent_tools.require_device_workspace", lambda: (7, FOLDER)), \
                mock.patch.object(file_ops, "_cb", lambda: self.fake), mock.patch.object(verify_loop, "_cb", lambda: self.fake):
            return asyncio.run(go())

    def edit(self, old, new="x"):
        return ("edit_file", {"path": "system_report_2.html", "old_string": old, "new_string": new})

    def test_the_error_lists_the_real_line_and_asks_for_a_read(self):
        (out,) = self.run_calls(self.edit(INVENTED))
        self.assertIn("old_string not found", out)
        self.assertIn("2026-10-15", out)                                  # the real line is shown, not another sub line
        self.assertIn("refused until you call read_file", out)

    def test_a_second_blind_attempt_is_refused_until_the_file_is_read(self):
        _, second = self.run_calls(self.edit(INVENTED), self.edit(INVENTED.replace("lorem", "12-01")))
        self.assertIn("before editing it", second)
        self.assertEqual(self.fake.files["system_report_2.html"], PAGE)

    def test_after_a_real_read_the_edit_goes_through(self):
        _, _, edited = self.run_calls(
            self.edit(INVENTED),
            ("read_file", {"path": "system_report_2.html"}),
            self.edit(REAL_LINE, '<div class="sub">Last boot: 2026-10-15 · up 20 days</div>'))
        self.assertIn("edited", edited)
        self.assertIn("up 20 days", self.fake.files["system_report_2.html"])

    def test_another_invented_edit_after_a_read_needs_another_read(self):
        out = self.run_calls(self.edit(INVENTED), ("read_file", {"path": "system_report_2.html"}), self.edit(INVENTED),
                             self.edit(REAL_LINE, "<p>x</p>"))
        self.assertIn("refused until you call read_file", out[2])          # the read is revoked again
        self.assertIn("before editing it", out[3])

    def test_an_ambiguous_match_does_not_cost_the_read(self):
        self.fake.files["system_report_2.html"] = "<p>a</p>\n<p>a</p>\n<p>b</p>\n"
        amb, ok = self.run_calls(self.edit("<p>a</p>"), self.edit("<p>b</p>", "<p>c</p>"))
        self.assertIn("2", amb)                                            # matches more than once: the model has the text
        self.assertIn("edited", ok)                                        # ...so no forced re-read

    def test_a_correct_first_edit_is_unaffected(self):
        (out,) = self.run_calls(self.edit(REAL_LINE, '<div class="sub">Last boot: now</div>'))
        self.assertIn("edited", out)
        self.assertIn("Last boot: now", self.fake.files["system_report_2.html"])


if __name__ == "__main__":
    unittest.main()
