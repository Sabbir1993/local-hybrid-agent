"""Policy eval set for 'should the company KB be searched?': labelled prompts, one decision each.

Run alone to see the precision / recall line:  python -m tests.test_kb_policy_eval
A threshold or keyword change that flips a labelled decision fails here, so it is judged on data, not anecdotes."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import knowledge_router as kr  # noqa: E402

# (own words, has attachments, voice, expected consult)
CASES = [
    # --- attached data: analyse it, never search the KB ---
    ("summarise this data set", True, False, False),
    ("summarize this", True, False, False),
    ("analyse the attached sheet", True, False, False),
    ("how many rows are pending", True, False, False),
    ("group by status and give the totals", True, False, False),
    ("plot the trend by month", True, False, False),
    ("find duplicates in this file", True, False, False),
    ("what are the top 5 customers by amount", True, False, False),
    ("translate the notes column to Bengali", True, False, False),
    ("convert this to a pdf report", True, False, False),
    # --- attached data + explicit company intent: search ---
    ("compare this with our leave policy", True, False, True),
    ("does this follow the company policy", True, False, True),
    ("check these employees against the knowledge base", True, False, True),
    ("is this consistent with the HR handbook", True, False, True),
    ("use our internal guidelines to review this", True, False, True),
    # --- no attachment: company questions search ---
    ("what is the leave policy", False, False, True),
    ("who is the engineering manager", False, False, True),
    ("show HR contacts", False, False, True),
    ("what are the company working hours", False, False, True),
    ("find employee details of Rahim", False, False, True),
    ("which holidays does the office have in October", False, False, True),
    ("what does the knowledge base say about payroll", False, False, True),
    ("who is our CEO", False, False, True),
    # --- no attachment: small talk never searches ---
    ("hi", False, False, False),
    ("hello", False, False, False),
    ("thanks", False, False, False),
    ("ok", False, False, False),
    ("good morning", False, False, False),
    ("bye", False, False, False),
    ("", False, False, False),
    # --- voice keeps the company-only rule ---
    ("tell me a joke", False, True, False),
    ("what is the leave policy", False, True, True),
]


def decide(text: str, att: bool, voice: bool) -> bool:
    return kr.should_consult_kb(text, att, voice_company_only=voice)[0]


class PolicyEvalTests(unittest.TestCase):
    def setUp(self):
        self._t = mock.patch.object(kr, "_get_active_source_titles", lambda ids=None: {1: "Automated Test Document", 2: "Employee Base"})
        self._t.start()

    def tearDown(self):
        self._t.stop()

    def test_every_labelled_decision(self):
        wrong = [(t, a, v, want) for t, a, v, want in CASES if decide(t, a, v) != want]
        self.assertEqual(wrong, [], "decisions that changed")

    def test_precision_and_recall(self):
        tp = sum(1 for t, a, v, w in CASES if w and decide(t, a, v))
        fp = sum(1 for t, a, v, w in CASES if not w and decide(t, a, v))
        fn = sum(1 for t, a, v, w in CASES if w and not decide(t, a, v))
        precision, recall = tp / max(1, tp + fp), tp / max(1, tp + fn)
        print(f"\nKB policy eval: {len(CASES)} prompts, precision {precision:.2f}, recall {recall:.2f}")
        self.assertGreaterEqual(precision, 0.95)
        self.assertGreaterEqual(recall, 0.95)

    def test_the_old_behaviour_would_have_failed_the_attachment_cases(self):
        """The reported bug, as a regression check: with the file text in the query the old gate said yes."""
        sheet = "--- FILE: Pending list SAP & SSL.xlsx ---\n- Employee: text\n- Policy: leave policy HR\n--- END Pending list SAP & SSL.xlsx ---"
        raw = "summarise this data set\n\n" + sheet
        self.assertTrue(kr.is_company_or_kb_query(raw))
        self.assertFalse(decide(kr.own_words(raw), kr.has_attachment_text(raw), False))


if __name__ == "__main__":
    unittest.main()
