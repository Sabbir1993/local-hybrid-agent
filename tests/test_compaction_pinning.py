"""Compaction keeps loaded skills and says what each digested tool result was, so the model does not
re-run calls it already made (the "read_skill ran 3 times with the same result" loop)."""

import json
import unittest

from core.agent_loop import compaction as C


def step(i, name, args, result):
    return [{"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": result}]


def run(n_steps, skill_at=0, skill_text="SKILL BODY: do a, b, c"):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "review the code"}]
    for i in range(n_steps):
        if i == skill_at:
            msgs += step(i, "read_skill", {"name": "code-review"}, skill_text)
        else:
            msgs += step(i, "read_file", {"path": f"src/f{i}.js"}, f"line one of file {i}\n" + "x" * 3000)
    return msgs


class PinTests(unittest.TestCase):
    def test_skill_result_survives_compaction_verbatim(self):
        out = C.compact_messages(run(30), 6000)
        self.assertIn("SKILL BODY: do a, b, c", [m.get("content") for m in out if m["role"] == "tool"])

    def test_pinned_unit_keeps_its_tool_call_pairing(self):
        out = C.compact_messages(run(30), 6000)
        ids_calls = {tc["id"] for m in out for tc in (m.get("tool_calls") or [])}
        ids_results = {m["tool_call_id"] for m in out if m["role"] == "tool"}
        self.assertEqual(ids_results - ids_calls, set())

    def test_huge_skill_is_not_pinned(self):
        out = C.compact_messages(run(30, skill_text="S" * (C.PIN_MAX_CHARS + 10)), 6000)
        self.assertFalse(any(m.get("content", "").startswith("SSSS") for m in out if m["role"] == "tool"))

    def test_only_newest_copy_of_a_skill_is_kept(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        for i in range(3):
            msgs += step(i, "read_skill", {"name": "code-review"}, f"copy {i}")
        for i in range(3, 30):
            msgs += step(i, "read_file", {"path": f"f{i}"}, "x" * 3000)
        out = C.compact_messages(msgs, 6000)
        kept = [m["content"] for m in out if m["role"] == "tool" and m["content"].startswith("copy")]
        self.assertEqual(kept, ["copy 2"])


class DigestTests(unittest.TestCase):
    def test_digest_names_the_file_and_first_line(self):
        out = C.compact_messages(run(30, skill_at=29), 6000)
        digest = out[1]["content"]
        self.assertIn("read_file(src/f0.js): line one of file 0", digest)

    def test_recent_results_default_is_three(self):
        self.assertEqual(C._keep_recent_results(), 3)


if __name__ == "__main__":
    unittest.main()
