"""tests/test_retention_download.py - generated-file retention purge and the opt-in download recovery."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.agent_tools  # noqa: F401, E402
from core.media import storage  # noqa: E402


def touch(path: Path, age_days: float, data: bytes = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_old_media_goes_new_media_and_other_files_stay(self):
        old, new = self.root / "user_1/generated/old.png", self.root / "user_1/generated/new.png"
        clip = self.root / "user_2/generated/old.mp4"
        doc = self.root / "user_1/report-v1-abcd1234.pdf"                        # a document: never touched
        note = self.root / "user_1/generated/readme.txt"                        # not media this module saves
        for f, age in ((old, 40), (new, 2), (clip, 31), (doc, 400), (note, 400)):
            touch(f, age)
        self.assertEqual(storage.purge_generated(30, root=self.root), 2)
        self.assertFalse(old.exists() or clip.exists())
        self.assertTrue(new.exists() and doc.exists() and note.exists())

    def test_zero_keeps_everything(self):
        f = self.root / "user_1/generated/a.png"
        touch(f, 999)
        self.assertEqual(storage.purge_generated(0, root=self.root), 0)
        self.assertTrue(f.exists())

    def test_folders_inside_generated_are_left_alone(self):
        d = self.root / "user_1/generated/sub"
        touch(d / "a.png", 999)
        self.assertEqual(storage.purge_generated(1, root=self.root), 0)
        self.assertTrue((d / "a.png").exists())

    def test_the_startup_hook_reads_the_config(self):
        with mock.patch("core.small_model.APP_CONFIG", {"chat": {"generated_retention_days": 30}}), \
                mock.patch.object(storage, "purge_generated", return_value=3) as p:
            self.assertEqual(storage.purge_generated_configured(), 3)
            p.assert_called_once_with(30)
        with mock.patch("core.small_model.APP_CONFIG", {"chat": {"generated_retention_days": "soon"}}):
            self.assertEqual(storage.purge_generated_configured(), 0)
        with mock.patch("core.small_model.APP_CONFIG", {}):
            self.assertEqual(storage.purge_generated_configured(), 0)


class DownloadRecoveryTests(unittest.TestCase):
    def test_off_by_default_and_switchable(self):
        from routes.agent import download
        with mock.patch("core.small_model.APP_CONFIG", {}):
            self.assertFalse(download._recovery_on())
        with mock.patch("core.small_model.APP_CONFIG", {"chat": {"download_recovery": True}}):
            self.assertTrue(download._recovery_on())

    def test_a_missing_file_is_not_rebuilt_from_chat_history(self):
        from routes.agent import download
        with mock.patch.object(download, "get_current_user_id", return_value=7), \
                mock.patch("core.small_model.APP_CONFIG", {}), \
                mock.patch("core.db._projects_db") as db:
            self.assertIsNone(download._resolve_requested_file("nope-never-existed.md"))
            db.execute.assert_not_called()                                      # the chat history was not even read


if __name__ == "__main__":
    unittest.main()
