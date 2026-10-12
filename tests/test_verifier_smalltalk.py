import unittest

from core.verifier.config import is_smalltalk


class SmalltalkTests(unittest.TestCase):
    def test_greetings_and_thanks_are_skipped(self):
        for q in ("hi", "Hi!", "hello", "Hey 👋", "thanks", "thank you!", "Good morning", "how are you?",
                  "assalamu alaikum", "হ্যালো", "ধন্যবাদ"):
            self.assertTrue(is_smalltalk(q), q)

    def test_real_questions_are_still_checked(self):
        for q in ("hi, what is our leave policy", "what is 17*23", "hello world in python", "thanks but that is wrong",
                  "", "share me the nearest Dhaka bank booth from me"):
            self.assertFalse(is_smalltalk(q), q)


if __name__ == "__main__":
    unittest.main()
