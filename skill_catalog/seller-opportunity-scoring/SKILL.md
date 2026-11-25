---
name: seller-opportunity-scoring
title: Seller Opportunity Scoring
description: Score and rank the seller's products against a target account with editable weights and live ranking formulas in Excel.
triggers: opportunity scoring, score opportunities, rank opportunities, whitespace, upsell opportunities, cross-sell
category: sales
version: 1.0.0
---

# Seller Opportunity Scoring

Builds the `Opportunities` sheet for read_skill(account-intelligence-workbook). Everything is parameterised by `{seller}`, `{account}` and `{catalogue}`; never hardcode names or products.

## Inputs
- `{catalogue}`: the seller's products from the user, knowledge base or memory. If none is available, ask once. Do not invent products or prices.
- Evidence from the other sheets (digital channels, vendors, roadmap, budget, procurement, relationship).

## Layout (one row per product x target department)
| Rank | Product | Target department | Evidence (fact + Source ID) | Fit (1-5) | Urgency (1-5) | Budget signal (1-5) | Competitor risk (1-5) | Score | Priority | Next step |
Weights live in a small input table on the same sheet (yellow cells, editable), e.g. Fit 35%, Urgency 25%, Budget 25%, Competitor risk 15% (inverse). Defaults are a starting point: say so in a `note:`.

## Formulas (the score and rank are never typed)
- Score = `=ROUND(Fit*w_fit + Urgency*w_urg + Budget*w_bud + (6-Risk)*w_risk, 2)` using named ranges for weights (`name: w_fit = Opportunities!<cell>`).
- Rank = `=RANK(<score cell>, <score column range>)`; ties broken by adding a tiny row-based term: `=<score>+ROW()/100000` in a helper column if needed.
- Priority = `=IF(<score> >= 4, "High", IF(<score> >= 3, "Medium", "Low"))`.
- Top-5 block (a `### Block:` on the same sheet, or the Exec Summary): row k = `=INDEX(<product col>, MATCH(k, <rank col>, 0))`, score = `=LARGE(<score col>, k)`.
- Exec Summary KPIs point at these cells.
Use only allowed functions (LARGE, RANK, INDEX, MATCH, IF, ROUND, IFERROR; not FILTER/SORT).

## Rules
1. Every Fit/Urgency/Budget/Risk rating must cite evidence in the row. No evidence = score 1-2 and say "no evidence found".
2. Mark the ratings as analyst judgement in a note; the user can change the yellow input cells and the ranking updates.
3. Separate "already sold" products (relationship sheet) from whitespace; do not rank them as new opportunities.
4. Add `highlight: Priority = High green`, `Priority = Low grey`, and `validate: Fit (1-5) between 1 and 5` (same for the other ratings).
5. Do not state revenue or win-probability numbers unless the user supplied the method.
