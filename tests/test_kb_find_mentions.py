import unittest

from core import kb_mentions

HEADER = "Name | Role | Phone"


def kid_of(path):
    return int(path.split(":")[1]) if path.startswith("kb:") else None


def chunk(kid, text):
    return ("knowledge", f"kb:{kid}", text, None)


ENTRIES = [
    chunk(1, f"{HEADER}\nAlice Rahman | Engineer | 0171"),
    chunk(1, "Rahim Uddin | Manager | 0181\nAlicia Khan | HR | 0191"),    # a later chunk: no header line in it
    chunk(2, "Leave policy\nRahim Uddin approved the policy in 2024."),
    chunk(3, "Secret roster\nRahim Uddin | CFO | 0000"),
    ("workspace", "kb:1", "Rahim Uddin in a workspace file", None),     # not knowledge: ignored
]
TITLES = {1: "Staff list", 2: "HR policy", 3: "Restricted"}


def find(name, allowed=(1, 2), **kw):
    return kb_mentions.find_mentions(name, ENTRIES, TITLES, kid_of, set(allowed), **kw)


class FindMentionsTests(unittest.TestCase):
    def test_name_in_several_documents_is_not_capped_at_six(self):
        many = [chunk(i, f"Rahim Uddin | role {i}") for i in range(10, 40)]
        r = kb_mentions.find_mentions("rahim uddin", many, {i: f"Doc {i}" for i in range(10, 40)}, kid_of,
                                      set(range(10, 40)))
        self.assertEqual(len(r["docs"]), 30)

    def test_row_is_paired_with_its_header_even_from_another_chunk(self):
        r = find("Rahim Uddin")
        staff = next(d for d in r["docs"] if d["title"] == "Staff list")
        self.assertEqual(staff["header"], HEADER)
        self.assertEqual(staff["lines"], ["Rahim Uddin | Manager | 0181"])

    def test_access_control_is_respected(self):
        titles = [d["title"] for d in find("Rahim Uddin", allowed=(1, 2))["docs"]]
        self.assertNotIn("Restricted", titles)

    def test_whole_words_only(self):
        r = find("Ali")
        self.assertEqual(r["docs"], [])                    # Alice / Alicia are different names

    def test_word_order_and_middle_initial_still_match(self):
        self.assertTrue(find("Uddin Rahim")["docs"])

    def test_other_source_types_are_ignored(self):
        self.assertEqual(sum(d["total"] for d in find("Rahim Uddin")["docs"]), 2)

    def test_truncation_is_reported(self):
        r = find("Rahim Uddin", max_docs=1)
        self.assertEqual(r["more_docs"], 1)
        self.assertIn("more document", kb_mentions.render("Rahim Uddin", r))

    def test_empty_and_missing_names(self):
        self.assertEqual(find("   ")["docs"], [])
        self.assertIn("no document", kb_mentions.render("Nobody", find("Nobody")))

    def test_regex_characters_in_a_name_are_literal(self):
        self.assertEqual(find("Rahim (Uddin")["docs"], [])


if __name__ == "__main__":
    unittest.main()
