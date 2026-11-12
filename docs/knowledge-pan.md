# Card numbers in the knowledge base

PCI DSS v4: a card number (PAN) is masked wherever it is shown (at most the first 6 / last 4 digits; here only the last 4) and
unreadable wherever it is stored (Req. 3.4 / 3.5). Less data also goes to third-party models (cloud lanes).

## What happens at ingest (default)

`knowledge.mask_pan_at_ingest` in `config/app.json` is **true** by default. When a source is ingested (upload, URL, pasted text,
re-index) card numbers are masked BEFORE anything is written to `memory.db`:

| Stored where | Masked how |
|---|---|
| spreadsheet rows (`kb_rows`) | cell by cell (`kb_tables.replace_source`), columns keep their place |
| text chunks, the full-text index (`chunks_fts`), the entity index | the text is masked before it is chunked (`core.memory.indexing.index_knowledge_source`) |
| text sent to the embedder | the same masked chunks, so a raw number never reaches the embedding model |

The mask is the one used everywhere else: `[card ****1111]` (the last four digits).

**Only real card numbers are masked**: 13-19 digits that pass the Luhn check and have a known issuer prefix (Visa, Mastercard,
Amex, Discover, Diners, JCB, UnionPay, RuPay, Maestro, Mir, Troy), or, in a column named like a card column ("Card No", "PAN",
"কার্ড"), or next to the word "card", a 13-19 digit number that fails the check (a mistyped number still leaks otherwise). Phone
numbers, NIDs, account numbers, transaction ids, amounts and dates are left alone. A CVV/CVC next to its keyword is masked too.
A number written in prose within about 50 characters after a card number is read as card context and masked as well, the same rule
the read paths use.

### A number split over several cells

A card number can be spread over neighbouring cells of one row (`4111 1111 | 1111 1111`, four cells of four digits,
`4111111111 | 111111`). A single-cell check does not see it, and a SQL `a || b` would put it back together. So at storing time
each row is also checked as a whole (`core/pan.py`: `split_pan_cells`, `mask_split_pan_cells`):

- a piece is a short cell with only digits, spaces and dashes (3 to 12 digits); empty cells between pieces do not break the run
- 2 to 4 neighbouring pieces whose digits together pass the card check (Luhn + known prefix, 13-19 digits) are one number; the
  first and middle pieces need 4+ digits, the last 3+ (the rule of the text detector), so a quantity next to an amount stays
- stored as `[card part]` in every piece but the last and `[card ****1111]` in the last one
- pieces separated by a cell with other content (a name, a note) are not neighbours and are not joined
- this check is **always on**, also with the switch off: it is what keeps `kb_sql` from rebuilding a number, and it changes a row
  only when the row really holds a card number

`kb_query` output and the `kb_sql` copy path apply the same check when they read rows, so rows stored by an older version are
hidden there too. The in-place `kb_sql` path reads the stored cells, so for those old rows run the step below.

## Turning it off

```json
"knowledge": { "mask_pan_at_ingest": false }
```

Sources are then stored as they come (apart from the split-cell rule above). Every read path (`kb_query`, `kb_sql`, search,
cloud egress, chat output) still masks card numbers on the way out, whichever way the switch is set.

## Sources ingested before the switch existed

They are not rewritten automatically. Each table has a `masked` flag (`kb_tables.masked`, added to an existing database by a
guarded `ALTER TABLE`; an old table counts as 0). When the switch is on, the server logs one line at startup:

    [server_manager] N knowledge source(s) were stored before card numbers were masked at ingest; see docs/knowledge-pan.md ...

Two ways to apply it:

1. **Re-index** (complete): the existing admin route `POST /knowledge/{id}/reindex` (permission `knowledge.manage`) re-reads the
   stored file or URL of one source and ingests it again through the masking above. Chunks and embeddings are rebuilt from the
   masked text. Pasted-text sources cannot be re-indexed this way: add the text again. (`POST /knowledge/reindex-outdated` only
   queues sources that are stale for other reasons, so it does not cover this.)
2. **In place** (no original needed): `kb_tables.remask_all()` masks every table that is not flagged yet (rows) and the text of the
   knowledge chunks (the full-text index follows through its triggers), and sets the flag. It returns
   `{"tables": n, "rows": n, "chunks": n}` and is safe to run again.

   ```
   python -X utf8 -c "from core import kb_tables; print(kb_tables.remask_all())"
   ```

   Limits: the stored embedding vectors were computed from the old text and are not touched, and a number cut in two by a chunk
   boundary is not seen. Use the re-index route when either matters.

## Cloud answers

`kb_query` in a cloud answer only ever looks at sources an admin cleared for cloud models. A source that is not cleared is not
counted, filtered, grouped or used for a hint, so nothing in the reply can depend on its data (a `count` with a `contains` filter
cannot be used to probe it). The reply says how many readable sources were left out, and nothing else about them:
`N source(s) are not available to a cloud model and were not searched.` Rows that a sensitive-content rule holds back inside a
cleared source are still screened as before. `kb_sql` is not available to a cloud answer at all.
