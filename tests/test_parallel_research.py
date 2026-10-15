"""Deck research: the broad searches are planned to run side by side."""
import unittest

from core import deck_research as dr


class Plan(unittest.TestCase):
    def test_up_to_four_facets_and_never_more_than_half_the_budget(self):
        q = dr.parallel_queries("Dhaka", 10)
        self.assertEqual(len(q), 4)
        self.assertTrue(all(x.startswith("Dhaka ") for x in q))
        self.assertEqual(len(dr.parallel_queries("Dhaka", 5)), 2)
        self.assertEqual(len(set(q)), 4)

    def test_tiny_budget_or_no_topic_means_no_prefetch(self):
        self.assertEqual(dr.parallel_queries("Dhaka", 3), [])
        self.assertEqual(dr.parallel_queries("   ", 10), [])
        self.assertEqual(dr.parallel_queries("", 10), [])

    def test_flag_turns_it_off(self):
        self.assertEqual(dr.parallel_queries("Dhaka", 10, {"chat": {"deck_parallel_research": False}}), [])

    def test_long_requests_are_trimmed(self):
        q = dr.parallel_queries("make a very deep informative presentation about the history of Dhaka with many images " * 3, 10)
        self.assertTrue(all(len(x) < 140 for x in q))

    def test_the_subject_is_pulled_out_of_the_request(self):
        for ask, want in (("Make a deep informative PPT of Dhaka with images from web", "Dhaka"),
                          ("create a presentation about cyber security for banks", "cyber security for banks"),
                          ("build me a pdf guide on onboarding new agents", "onboarding new agents"),
                          ("Dhaka", "Dhaka")):
            self.assertEqual(dr.topic_of(ask), want, ask)
        self.assertEqual(dr.parallel_queries("Make a ppt of Dhaka with images", 10)[0], "Dhaka overview key facts")

    def test_the_chat_route_uses_it_once_in_the_build_phase(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("deck_research.parallel_queries("), 1)
        self.assertIn("asyncio.gather(*[tool_web_search", src)


if __name__ == "__main__":
    unittest.main()
