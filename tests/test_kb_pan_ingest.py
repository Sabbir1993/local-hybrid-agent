"""Card numbers are masked when a knowledge source is ingested (knowledge.mask_pan_at_ingest, default on): the stored rows, the
chunks, the full-text index and the text sent to the embedder never hold one. PCI DSS 3.5."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import kb_structure, kb_tables, pan
from core.agent_tools import search_tools
from tests.test_kb_tables import TITLES

RAW = "4111111111111111"
RAW_GROUPED = "4111 1111 1111 1111"
SHEET = [["Merchant", "Card No", "Phone", "NID", "Account", "Amt"],
         ["Acme", RAW_GROUPED, "01711223344", "1234567890123", "1234567890123456", "1,200"],
         ["Bob", "5500005555555559", "01811223344", "9876543210123", "2000000000000001", "800"],
         ["Cy", "4111 1111", "1111 1111", "", "", "5"]]
PROSE = f"The customer paid with card {RAW_GROUPED} on Monday. Phone 01711223344, NID 1234567890123. Ref {RAW}."


class IngestBase(unittest.TestCase):
    def setUp(self):
        import core.memory as memory
        self._tmp = tempfile.TemporaryDirectory()
        for p in (mock.patch.object(memory, "_conn", None), mock.patch.object(memory, "MEMORY_DB_FILE", Path(self._tmp.name) / "m.db")):
            p.start()
            self.addCleanup(p.stop)
        kb_tables._ready = False
        self.sent = []

        async def fake_embed(texts):
            self.sent.extend(texts)
            return [[0.1, 0.2, 0.3] for _ in texts]
        p = mock.patch.object(memory, "_embed_texts", fake_embed)
        p.start()
        self.addCleanup(p.stop)
        from core.memory.store import _db
        self.db = _db()

    def tearDown(self):
        try:
            self.db.close()
            self._tmp.cleanup()
        except Exception:
            pass

    def ingest(self, source_id, text, flag=True):
        from core.memory import index_knowledge_source
        with mock.patch.dict("core.small_model.APP_CONFIG", {"knowledge": {"mask_pan_at_ingest": flag}}):
            kb_tables.replace_source(source_id, text)
            return asyncio.run(index_knowledge_source(source_id, text))

    def stored(self):
        """Everything a source left in memory.db as one string: rows, chunks, the full-text index."""
        parts = [r[0] for r in self.db.execute("SELECT cells FROM kb_rows")]
        parts += [r[0] for r in self.db.execute("SELECT text FROM chunks")]
        parts += [r[0] for r in self.db.execute("SELECT text FROM chunks_fts")]
        return "\n".join(parts)

    def fts_hits(self, token):
        return self.db.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (f'"{token}"',)).fetchone()[0]


class MaskAtIngestTests(IngestBase):
    def test_a_spreadsheet_with_card_numbers_leaves_none_behind(self):
        self.ingest(5, kb_structure.rows_to_text(SHEET, "Txn"))
        data = self.stored()
        for needle in (RAW, RAW_GROUPED, "5500005555555559", "55000055", "4111 1111", "41111111"):
            self.assertNotIn(needle, data, needle)
        self.assertIn("[card ****1111]", data)
        self.assertIn("[card ****5559]", data)
        self.assertEqual(self.fts_hits("4111"), 0)                                         # the full-text index has no card digits either
        self.assertEqual(self.fts_hits("5500005555555559"), 0)
        self.assertGreater(self.db.execute("SELECT count(*) FROM chunks").fetchone()[0], 0)

    def test_prose_chunks_are_masked_too(self):
        self.ingest(6, PROSE)
        data = self.stored()
        self.assertNotIn(RAW, data)
        self.assertNotIn("4111 1111", data)
        self.assertIn("[card ****1111]", data)
        self.assertEqual(self.fts_hits("4111"), 0)

    def test_the_embedder_only_ever_sees_the_masked_text(self):
        self.ingest(5, kb_structure.rows_to_text(SHEET, "Txn"))
        self.ingest(6, PROSE)
        self.assertTrue(self.sent)
        joined = "\n".join(self.sent)
        for needle in (RAW, RAW_GROUPED, "5500005555555559", "4111 1111"):
            self.assertNotIn(needle, joined, needle)
        self.assertIn("[card ****1111]", joined)

    def test_with_the_switch_off_the_source_is_stored_as_it_came(self):
        self.ingest(5, kb_structure.rows_to_text(SHEET, "Txn"), flag=False)
        self.ingest(6, PROSE, flag=False)
        data = self.stored()
        self.assertIn(RAW_GROUPED, data)                                                    # in the rows ...
        self.assertIn("5500005555555559", data)
        self.assertIn(RAW, "\n".join(r[0] for r in self.db.execute("SELECT text FROM chunks")))   # ... and in the chunks
        self.assertIn(RAW, "\n".join(self.sent))                                            # ... and it reached the embedder
        self.assertGreater(self.fts_hits("4111"), 0)

    def test_the_switch_is_on_by_default(self):
        with mock.patch.dict("core.small_model.APP_CONFIG", {"knowledge": {}}):
            self.assertTrue(pan.ingest_mask_enabled())
        with mock.patch.dict("core.small_model.APP_CONFIG", {}, clear=True):
            self.assertTrue(pan.ingest_mask_enabled())
        for off in (False, "false", "off", 0):
            with mock.patch.dict("core.small_model.APP_CONFIG", {"knowledge": {"mask_pan_at_ingest": off}}):
                self.assertFalse(pan.ingest_mask_enabled(), off)
        from core.small_model import APP_CONFIG
        self.assertIs(APP_CONFIG["knowledge"].get("mask_pan_at_ingest"), True)             # the shipped default

    def test_numbers_that_are_not_cards_are_untouched(self):
        self.ingest(5, kb_structure.rows_to_text(SHEET, "Txn"))
        self.ingest(6, "Invoice 1234567890123 total 500. Call 01711223344. Account 2000000000000001. Order 8801711223344556.")
        data = self.stored()
        for keep in ("01711223344", "1234567890123", "1234567890123456", "2000000000000001", "1,200", "9876543210123", "8801711223344556"):
            self.assertIn(keep, data, keep)
        self.assertIn("Invoice 1234567890123 total 500. Call 01711223344.", "\n".join(r[0] for r in self.db.execute("SELECT text FROM chunks")))
        rows = [json.loads(r[0]) for r in self.db.execute("SELECT cells FROM kb_rows ORDER BY rowid")]
        self.assertEqual(rows[0][1:], ["[card ****1111]", "01711223344", "1234567890123", "1234567890123456", "1,200"])
        self.assertEqual(rows[2], ["Cy", "[card part]", "[card ****1111]", "", "", "5"])

    def test_columns_keep_their_place_when_a_cell_is_masked(self):
        self.ingest(5, kb_structure.rows_to_text(SHEET, "Txn"))
        spec = {"source": "txn", "select": ["Merchant", "Card No", "Phone"], "filters": [{"column": "Merchant", "op": "=", "value": "Acme"}]}
        out = kb_tables.render(spec, kb_tables.run_query(spec, {5}, {5: "Cards"}))
        self.assertIn("| R2 | Acme | [card ****1111] | 01711223344 |", out)

    def test_reingesting_is_idempotent(self):
        text = kb_structure.rows_to_text(SHEET, "Txn")
        self.ingest(5, text)
        first = self.stored()
        self.ingest(5, text)
        self.assertEqual(self.stored(), first)
        self.assertEqual(self.db.execute("SELECT count(*) FROM kb_rows").fetchone()[0], 3)
        masked = kb_structure.mask_text(text)
        self.assertEqual(kb_structure.mask_text(masked), masked)
        self.assertEqual(kb_structure.mask_text(PROSE), kb_structure.mask_text(kb_structure.mask_text(PROSE)))

    def test_a_card_column_masks_a_mistyped_number_too(self):
        self.ingest(5, kb_structure.rows_to_text([["Merchant", "Card No"], ["Dee", "4111111111111112"]], "T"))
        self.assertNotIn("4111111111111112", self.stored())
        self.ingest(6, kb_structure.rows_to_text([["Merchant", "Ref"], ["Dee", "4111111111111112"]], "T"))   # same digits, not a card column
        self.assertIn("4111111111111112", self.stored())

    def test_a_cvv_in_a_short_cell_is_masked(self):
        self.ingest(5, kb_structure.rows_to_text([["Merchant", "Note"], ["Dee", "cvv 123"]], "T"))
        self.assertNotIn("123", self.stored().replace("[card", ""))

    def test_a_sheet_with_other_digits_is_masked_too(self):
        bn = lambda s: "".join(chr(0x09e6 + int(c)) if c.isdigit() else c for c in s)
        self.ingest(5, kb_structure.rows_to_text([["Merchant", "Card No"], ["Dee", bn(RAW_GROUPED)]], "T"))
        self.ingest(6, kb_structure.rows_to_text([["Merchant", "A", "B"], ["Dee", bn("4111 1111"), bn("1111 1111")]], "T"))
        self.assertNotIn(bn("4111"), self.stored())


class MaskedFlagAndRemaskTests(IngestBase):
    def test_tables_are_marked_masked_only_when_the_switch_is_on(self):
        self.ingest(5, kb_structure.rows_to_text(SHEET, "Txn"))
        self.ingest(6, kb_structure.rows_to_text(SHEET, "Txn"), flag=False)
        flags = dict(self.db.execute("SELECT source_id, masked FROM kb_tables"))
        self.assertEqual(flags, {5: 1, 6: 0})
        self.assertEqual(kb_tables.unmasked_sources(), 1)

    def test_remask_all_masks_what_was_stored_before(self):
        self.ingest(6, kb_structure.rows_to_text(SHEET, "Txn"), flag=False)
        self.ingest(7, PROSE, flag=False)
        self.assertIn(RAW_GROUPED, self.stored())
        done = kb_tables.remask_all()
        self.assertEqual(done["tables"], 1)
        self.assertGreaterEqual(done["rows"], 2)
        self.assertGreaterEqual(done["chunks"], 1)
        data = self.stored()
        for needle in (RAW, RAW_GROUPED, "5500005555555559", "4111 1111"):
            self.assertNotIn(needle, data, needle)
        self.assertEqual(self.fts_hits("4111"), 0)                                         # the full-text index followed the update
        self.assertEqual(kb_tables.unmasked_sources(), 0)
        self.assertEqual(kb_tables.remask_all(), {"tables": 0, "rows": 0, "chunks": 0})    # nothing left to do

    def test_an_old_database_gets_the_column_by_a_guarded_alter(self):
        db = self.db
        db.execute("DROP TABLE IF EXISTS kb_tables")
        db.execute("""CREATE TABLE kb_tables (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, sheet TEXT NOT NULL,
                      columns TEXT NOT NULL, nrows INTEGER NOT NULL)""")
        db.execute("INSERT INTO kb_tables (source_id, sheet, columns, nrows) VALUES (1, 'S', '[\"A\"]', 1)")
        db.commit()
        kb_tables._ready = False
        kb_tables._ensure()
        kb_tables._ensure()                                                                 # a second run is a no-op
        self.assertEqual(db.execute("SELECT masked FROM kb_tables").fetchone()[0], 0)       # an old table counts as not yet masked
        self.assertEqual(kb_tables.unmasked_sources(), 1)
