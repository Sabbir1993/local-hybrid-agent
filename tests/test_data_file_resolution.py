"""Attached data files whose names differ only by person and ID (one HR record per employee): a made-up name must fail
with the real names listed, never resolve to another person's file; the chat prompt lists the exact names to copy."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import agent_tools, prompt_scope
from core.doc_tools.location import data_file_choices, resolve_common
from core.request_context import set_current_user

NAMES = [
    "Achivement Record Form 1 - Md Abdul Kader Zilani - 1941.xlsx",
    "Achivement Record Form 1 - Md Ejhar Uddin Nomani -1504.xlsx",
    "Achivement Record Form 1 - Moinul Hasan Khan - 1890.xlsx",
    "Achievement Record Form 1 - Sabbir Hosain Chowdury - 1439.xlsx",
    "Achivement Record Form 1 - Ariful Isalm - 1932.xlsx",
]


class DataFileResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(agent_tools, "COMMON_ROOT", Path(self.tmp.name))
        self._p.start()
        set_current_user(21)
        for n in NAMES:
            (agent_tools.common_workspace() / n).write_bytes(b"x")

    def tearDown(self):
        self._p.stop()
        set_current_user(None)
        self.tmp.cleanup()

    def test_exact_name_resolves(self):
        self.assertEqual(resolve_common(NAMES[4]).name, NAMES[4])

    def test_invented_names_do_not_resolve_to_another_record(self):
        for guess in ("Achivement Record Form 1 - Ariful Isalm - 1931.xlsx",
                      "Achivement Record Form 1 - Ariful Isalm - 1941.xlsx",
                      "Achivement Record Form 1 - Ariful Isalm - 1504.xlsx"):
            self.assertIsNone(resolve_common(guess), guess)

    def test_ambiguous_part_of_a_name_is_refused(self):
        self.assertIsNone(resolve_common("Record Form 1.xlsx"))           # in every file's name

    def test_unique_part_of_a_name_still_resolves(self):
        self.assertEqual(resolve_common("Moinul Hasan Khan.xlsx").name, NAMES[2])

    def test_miss_error_suggests_and_lists_the_real_files(self):
        from core.agent_tools.data_tools import tool_data_query
        out = tool_data_query({"file": "Achivement Record Form 1 - Ariful Isalm - 1931.xlsx", "count": True})
        self.assertTrue(out.startswith("error: no attached data file"), out)
        self.assertIn(f"Did you mean: '{NAMES[4]}'", out)
        for n in NAMES:
            self.assertIn(n, out)

    def test_choices_never_show_another_users_files(self):
        set_current_user(22)
        self.assertIn("No data files are attached", data_file_choices("anything.xlsx"))


class AttachedDataListTests(unittest.TestCase):
    def test_names_come_from_the_client_note_in_order(self):
        msgs = [
            {"role": "user", "content": f'--- FILE: {NAMES[0]} ---\nprofile\n[... call data_query(file="{NAMES[0]}") ...]'
                                        f'\n--- END {NAMES[0]} ---\n--- FILE: {NAMES[4]} ---\n[data_query(file="{NAMES[4]}")]'},
            {"role": "assistant", "content": 'data_query(file="made up.xlsx")'},
            {"role": "user", "content": f'again [data_query(file="{NAMES[0]}")]'},
        ]
        self.assertEqual(prompt_scope.attached_data_files(msgs), [NAMES[0], NAMES[4]])
        block = prompt_scope.attached_data_block(prompt_scope.attached_data_files(msgs))
        self.assertIn(f"1. {NAMES[0]}", block)
        self.assertIn(f"2. {NAMES[4]}", block)

    def test_header_is_the_fallback(self):
        self.assertEqual(prompt_scope.attached_data_files([{"role": "user", "content": "--- FILE: a.csv ---\nrows"}]), ["a.csv"])
        self.assertEqual(prompt_scope.attached_data_block([]), "")


if __name__ == "__main__":
    unittest.main()
