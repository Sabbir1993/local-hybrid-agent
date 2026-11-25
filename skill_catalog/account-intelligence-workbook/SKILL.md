---
name: account-intelligence-workbook
title: Account Intelligence Workbook
description: Build a multi-sheet, formula-driven Excel account-intelligence workbook on a target company (profile, financials, technology, competitors, scored opportunities for the seller, sources).
triggers: account intelligence, account plan, strategic account, key account, kam, company profile workbook, account research workbook
category: sales
version: 1.0.0
---

# Account Intelligence Workbook

Produces one .xlsx that a sales / key-account team can use: facts about the target company, a scored list of opportunities for the seller, and a source for every number. Output is built with `write_file` (.xlsx) using the workbook outline format (see read_skill(spreadsheet) for formula and chart rules).

## 0. Parameters (never hardcode)
Resolve these first. Order: user prompt -> user memory/profile -> knowledge base -> ask ONCE.
- `{account}`: the company being analysed.
- `{seller}`: the company selling (whose opportunities are scored). Needed for the Opportunities sheet and the "Why it matters to {seller}" column.
- `{catalogue}`: the seller's products/services (use `search_knowledge_base` if the user has uploaded it). If unknown, ask for 3-8 product names; do not invent products.
- `{jurisdiction}` / `{regulator}`: from the account's country and sector.
- `{author}`: use the user's name ONLY if it exists in their profile or memory; otherwise omit the field.
Never copy example names from this skill, from earlier chats or from other workbooks into a new account's file. Every title, header and note must use the resolved parameters.

## 1. Choose modules
Mandatory core: **Cover**, **Exec Summary**, **Financials**, **Sources**.
Optional modules (pick from the request and the account type; user instructions override this list):
| Module | Use for |
|---|---|
| Overview | size, ownership, footprint, key figures |
| Segments | customer / business segments |
| Digital Channels | apps, portals, APIs, adoption numbers |
| Innovation | launches, partnerships, pilots |
| Build vs Buy | what they build in-house vs buy |
| Vendors | known technology vendors / partners |
| Awards | recognitions |
| Competitors | peer benchmark on the same metrics |
| IT Budget | technology spend, estimates flagged Estimated |
| Procurement | how they buy (tenders, committees, cycles) |
| CX Sentiment | reviews, ratings, complaints |
| SWOT | strengths, weaknesses, opportunities, threats |
| Roadmap | announced plans, next 3-5 years |
| Seller Relationship | what {seller} already sells to {account} |
| Opportunities | scored list for {seller} (see read_skill(seller-opportunity-scoring)) |
| Regulatory | see read_skill(regulatory-context-check) |
Account type guides defaults: bank/FI -> Digital Channels, IT Budget, Procurement, Regulatory; telco -> Segments, Digital Channels, Vendors; merchant/retail -> Segments, CX Sentiment, Competitors. Sheet limit is 20 and sheet names max 31 chars; merge or drop modules to fit.

## 2. Research
Follow read_skill(account-research). Collect facts into rows of: `Metric | Value | As of | Trend | Why it matters to {seller} | Source ID | Status`. Source IDs and Status follow read_skill(account-source-register).

## 3. Outline skeleton (workbook builder)
Rules of the builder:
- Each `## Sheet:` holds one table; directives (`format:`, `highlight:`, `validate:`, `widths:`, `freeze:`, `input:`, `note:`) go after the table. If the builder reports a warning, fix the outline and rebuild; do not ignore it.
- **Report layout (use on every data sheet):** `banner: <sheet title>` and `subtitle: <unit, basis, period>` lines go right under `## Sheet:` and render as merged title rows. `| >> SECTION NAME | | |` inside a table is a merged section band. `input: Col1, Col2` marks columns the reader may change (yellow fill, blue text).
- **Row offset (critical for formulas):** header row = 1 with no banner, 3 with a banner only, 4 with banner + subtitle. Data row n is at row `header + n`; a section band counts as a data row. Example with banner + subtitle: band = row 5, first metric = row 6. Write every formula and every cross-sheet reference with these real row numbers, and re-check them after the table is final.
- `widths:` accepts header names or letters (`widths: Metric=34, B=12`).

```
# {account} - Strategic Account Intelligence
theme: corporate
## Sheet: Cover
| Item | Detail |
|---|---|
| Account | {account} |
| Prepared for | {seller} |
| Prepared by | {author, only if known} |
| As of | <today's date> |
| Classification | Internal use only |
| How to read | Start with Exec Summary. Status colours show data reliability. Yellow cells are inputs you can change. Every figure has a Source ID in the Sources sheet. |
## Dashboard: Exec Summary
title: {account} - Executive Summary
kpi: <Headline metric> = ='Financials'!<cell> | format=currency:<ISO code>
kpi: Top opportunity score = =MAX('Opportunities'!<score column range>)
kpi: Data coverage = =COUNTIF(<status col>,"Verified")/COUNTA(<status col>) | format=percent
note: Figures update when the detail sheets change.
## Sheet: Financials
banner: Financial performance - {account}
subtitle: Unit: <currency, scale>; basis: <consolidated/standalone>; source IDs in column <Source ID>
| Metric | Unit | <Year 1> | ... | <Year N> | Source ID | Status |
|---|---|---|---|---|---|---|
| >> INCOME STATEMENT | | | | | | |
| Revenue | <unit> | <value or Not disclosed> | ... |
| Operating cost | ... |
| Operating profit | <unit> | =C6-C7 | ... |
| Margin | % | =C8/C6 | ... |
input: <Year 1>, <Year N>
chart: line | title=<metric> trend | x=Metric | y=<year columns>
(rows above assume banner + subtitle: header row 4, band row 5, Revenue row 6, Operating cost row 7, Operating profit row 8)
## Sheet: <Module>
banner: <Module title>
subtitle: <what the sheet shows, period, unit>
| Metric | Value | As of | Trend | Why it matters to {seller} | Source ID | Status |
|---|---|---|---|---|---|---|
| ... |
highlight: Status = Verified green
highlight: Status = Reported amber
highlight: Status = Estimated yellow
highlight: Status = Not disclosed grey
validate: Status in [Verified, Reported, Estimated, Not disclosed]
widths: Metric=34, Why it matters to {seller}=50
## Sheet: Sources
| Source ID | Title | Publisher | Date | URL | Reliability |
|---|---|---|---|---|---|
| S1 | ... |
```
Formula rules: derived numbers (profit, margin, growth, totals, rankings) MUST be formulas that reference input cells, never typed values. Only inputs are typed. Only use functions the builder allows (SUM, IF, IFERROR, INDEX, MATCH, LARGE, RANK, XLOOKUP, SUMPRODUCT, COUNTIF...; not FILTER/SORT/LET).

## 4. Quality gates before delivering
1. Every fact row has a Source ID that exists in Sources, and a Status.
2. No "N/A" left blank: unresolved after the gap-fill pass = `Not disclosed` with status Not disclosed.
3. Headline figures (profit, assets, customers) confirmed by a second source or marked Reported.
4. Open the saved file with `doc_inspect`/lint tooling; fix errors (#REF, #NAME, text-as-number).
5. Scan every sheet for any name that is not {account} or {seller} (leftover from examples or other runs) and remove it.
6. Add a privacy note if the workbook names individual executives; keep to public roles only.
7. Reply with the download link, the module list, the count of Not disclosed cells and the top 3 opportunities. Do not paste the workbook content into chat.
