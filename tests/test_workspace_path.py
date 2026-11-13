"""A stored folder on the user's machine must become a real absolute path, never one under the server's own folder."""
import unittest
from pathlib import Path
from unittest import mock

from core.workspace_path import normalise_device_path


class NormaliseDevicePath(unittest.TestCase):
    def test_drive_paths_are_kept_or_made_windows_style(self):
        self.assertEqual(normalise_device_path(r"D:\project_folder\agent-x"), r"D:\project_folder\agent-x")
        self.assertEqual(normalise_device_path("D:/project_folder/agent-x"), r"D:\project_folder\agent-x")

    def test_a_stray_leading_separator_before_the_drive_is_removed(self):
        # the stored value that made writes land under the server's install folder
        self.assertEqual(normalise_device_path(r"\D:\reports\weekly"), r"D:\reports\weekly")
        self.assertEqual(normalise_device_path("/D:/reports/weekly"), r"D:\reports\weekly")

    def test_a_posix_absolute_path_stays_posix(self):
        self.assertEqual(normalise_device_path("/home/me/reports"), "/home/me/reports")

    def test_relative_drive_relative_unc_and_parent_parts_are_refused(self):
        for bad in ("reports", "D:reports", r"\\srv\share\x", r"D:\a\..\b", "", "   "):
            with self.assertRaises(ValueError, msg=bad):
                normalise_device_path(bad)

    def test_the_resolved_file_is_on_the_device_folder_not_the_server_folder(self):
        from core.agent_tools import workspace
        stored = r"/D:/project_folder/agent-x"
        folder = lambda: workspace._device_folder(stored, "the work folder")
        with mock.patch.object(workspace, "active_workspace", folder), \
                mock.patch("core.agent_tools.active_workspace", folder, create=True):
            p = workspace._ws_resolve("_agent_run_report_f39b35de.py")
        self.assertTrue(str(p).lower().startswith(r"d:\project_folder\agent-x"), str(p))
        self.assertNotIn("local-hybrid-agent", str(p))


class SavedAndUsedTheSameWay(unittest.TestCase):
    def test_the_save_rule_stores_the_normalised_folder(self):
        from core.auth_db.custom_agents import clean_work_dir
        self.assertEqual(clean_work_dir(r"\D:\reports\weekly"), r"D:\reports\weekly")
        self.assertEqual(clean_work_dir(""), "")
        for bad in ("reports", r"D:\\", r"C:\Windows\x"):
            with self.assertRaises(ValueError, msg=bad):
                clean_work_dir(bad)

    def test_a_stored_folder_that_is_not_a_full_path_is_refused_with_a_clear_message(self):
        from core.agent_tools import workspace
        with self.assertRaises(workspace.WorkspaceAccessDenied) as cm:
            workspace._device_folder("reports", "this agent's work folder")
        self.assertIn("not a full path on your machine", str(cm.exception))
        self.assertIn("pick the folder again", str(cm.exception))

    def test_a_valid_stored_folder_is_an_absolute_path(self):
        from core.agent_tools import workspace
        p = workspace._device_folder(r"\D:\reports\weekly", "this agent's work folder")
        self.assertIsInstance(p, Path)
        self.assertEqual(str(p), r"D:\reports\weekly")


if __name__ == "__main__":
    unittest.main()
