"""tests/test_llama_bench_parse.py - pin core.llama_bench.parse_bench_json.

The parser has burned us twice: a tautological row filter (ng_ >= ng_) made
the last row win by accident, and avg_ns (nanoseconds per token) was accepted
as if it were tokens/sec. These tests pin both against a REAL fixture
captured from build b11429 (tests/fixtures/llama_bench_b11429.json) plus
synthetic variants for other field-name generations.
"""

import json
import unittest
from pathlib import Path

from core.llama_bench import parse_bench_json

FIXTURE = Path(__file__).parent / "fixtures" / "llama_bench_b11429.json"
REAL_PP_TS = 28.728729
REAL_TG_TS = 10.109277


def _rows_from_fixture() -> list:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ParseRealFixture(unittest.TestCase):
    def test_real_two_row_build(self):
        out = parse_bench_json(FIXTURE.read_text(encoding="utf-8"))
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["pp_ts"], REAL_PP_TS, places=4)
        self.assertAlmostEqual(out["tg_ts"], REAL_TG_TS, places=4)
        self.assertFalse(out["combined"])

    def test_rows_attribute_to_the_right_slot(self):
        rows = _rows_from_fixture()
        self.assertEqual(rows[0]["n_prompt"], 16)
        self.assertEqual(rows[0]["n_gen"], 0)
        self.assertEqual(rows[1]["n_prompt"], 0)
        self.assertEqual(rows[1]["n_gen"], 16)

    def test_salvage_json_from_markdown_table(self):
        rows = _rows_from_fixture()
        out = parse_bench_json("### some table\ncolumns...\n```\n"
                               + json.dumps(rows) + "\n```\ntrailing text")
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["tg_ts"], REAL_TG_TS, places=4)

    def test_zero_stddev_present(self):
        rows = _rows_from_fixture()
        for row in rows:
            row["stddev_ts"] = 0.25
        out = parse_bench_json(json.dumps(rows))
        self.assertTrue(out["ok"])
        self.assertEqual(out["pp_stddev_ts"], 0.25)
        self.assertEqual(out["tg_stddev_ts"], 0.25)


class FallbackGenerations(unittest.TestCase):
    def test_avg_ns_only_build_converts_units(self):
        # a build that emits ns/token instead of avg_ts: 1e9/1582704700 t/s
        rows = [
            {"n_prompt": 16, "n_gen": 0, "avg_ns": 556933800},
            {"n_prompt": 0, "n_gen": 16, "avg_ns": 1582704700},
        ]
        out = parse_bench_json(json.dumps(rows))
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["pp_ts"], 1e9 / 556933800, places=6)
        self.assertAlmostEqual(out["tg_ts"], 1e9 / 1582704700, places=6)

    def test_avg_ns_stddev_propagates_with_jacobian(self):
        # ts = 1e9/ns => sd_ts = 1e9*sd_ns/ns^2
        rows = [
            {"n_prompt": 0, "n_gen": 16, "avg_ns": 1_000_000_000, "stddev_ns": 10_000_000},
        ]
        out = parse_bench_json(json.dumps(rows))
        self.assertAlmostEqual(out["tg_ts"], 1.0, places=6)
        self.assertAlmostEqual(out["tg_stddev_ts"], 1e9 * 1e7 / 1e18, places=6)

    def test_avg_ns_zero_is_not_treated_as_a_rate(self):
        rows = [{"n_prompt": 0, "n_gen": 16, "avg_ns": 0}]
        out = parse_bench_json(json.dumps(rows))
        self.assertFalse(out["ok"])

    def test_combined_row_build(self):
        # older builds ram prefill+generation into one row
        rows = [{"n_prompt": 512, "n_gen": 128, "avg_ts": 7.5}]
        out = parse_bench_json(json.dumps(rows))
        self.assertTrue(out["ok"])
        self.assertTrue(out["combined"])
        self.assertAlmostEqual(out["tg_ts"], 7.5, places=6)
        self.assertIsNone(out["pp_ts"])


class Refusals(unittest.TestCase):
    def test_unparseable(self):
        out = parse_bench_json("no json here at all")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "unparseable")

    def test_non_list_json(self):
        out = parse_bench_json('{"ok": true}')
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "unparseable")

    def test_no_recognizable_rows(self):
        out = parse_bench_json(json.dumps([{"foo": 1}, {"bar": 2}]))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "no_recognizable_rows")

    def test_garbage_rows_skipped(self):
        rows = [
            {"n_prompt": "abc", "n_gen": None, "avg_ts": "z"},
            {"n_prompt": 4, "n_gen": 0, "avg_ts": "not-a-number"},
            {"n_prompt": 0, "n_gen": 8, "avg_ts": 5.5},
        ]
        out = parse_bench_json(json.dumps(rows))
        self.assertTrue(out["ok"])
        self.assertIsNone(out["pp_ts"])
        self.assertAlmostEqual(out["tg_ts"], 5.5, places=6)


if __name__ == "__main__":
    unittest.main()
