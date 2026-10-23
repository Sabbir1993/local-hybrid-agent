"""The daily image / video cap counts what a person makes on this PC as well as in the cloud.

Run: python -m unittest tests.test_media_quota -v   (from the repo root, or  cd tests && python -m unittest test_media_quota)
"""
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import auth_db
from core.media import jobs
from core.media.service import MediaError


class DailyCapTests(unittest.TestCase):
    def test_local_and_cloud_generations_both_count(self):
        uid = 7_000_000 + uuid.uuid4().int % 1_000_000          # a user of its own: the audit log is the real database
        for source in ("cloud", "local", "this_pc"):
            auth_db.insert_audit(uid, "u", "media.image", source, None, "allow", None, None)
        auth_db.insert_audit(uid, "u", "media.video", "local", None, "allow", None, None)
        auth_db.insert_audit(uid, "u", "media.image", "local", None, "deny", None, None)      # refused: not counted
        self.assertEqual(jobs.used_today(uid, "image"), 3)
        self.assertEqual(jobs.used_today(uid, "video"), 1)

    def test_the_cap_stops_a_person_who_reached_it(self):
        with mock.patch.object(jobs, "daily_limit", lambda kind: 3), mock.patch("core.media.used_today", lambda u, k: 3):
            with self.assertRaises(MediaError) as cm:
                jobs.check_cloud_quota(1, "image")
        self.assertIn("Daily limit reached for images (3)", str(cm.exception))

    def test_zero_means_no_limit(self):
        with mock.patch.object(jobs, "daily_limit", lambda kind: 0), mock.patch("core.media.used_today", lambda u, k: 999):
            jobs.check_cloud_quota(1, "video")


if __name__ == "__main__":
    unittest.main()
