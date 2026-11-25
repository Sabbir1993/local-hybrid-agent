---
name: regulatory-context-check
title: Regulatory Context Check
description: Jurisdiction-aware regulatory and compliance notes for an account sheet: applicable regulator, data-residency, security and AML rules, with sources and open questions.
triggers: regulatory context, regulator, compliance notes, data localization, jurisdiction rules, central bank guidance
category: sales
version: 1.0.0
---

# Regulatory Context Check

Optional `Regulatory` module for read_skill(account-intelligence-workbook). Jurisdiction comes from the account's country and sector; never assume one.

## Procedure
1. Identify `{jurisdiction}` and the sector regulator(s) for `{account}` (e.g. central bank, telecom regulator, securities commission, data-protection authority).
2. Research current published guidance with `web_search`/`web_fetch` (primary regulator site first). Record title, issuing body, date and Source ID. If you cannot find a current document, say so; do not quote rules from memory.
3. Build rows: `Area | Requirement (summary) | Applies to | Impact on {seller}'s offering | Source ID | Status`.
   Areas to cover when relevant: information-security guideline, data residency/localisation, payment and e-money licensing, AML/CFT, outsourcing and cloud rules, customer-data privacy, reporting duties.
4. Add a column `Needs confirmation` (Yes/No) and set Yes where the text is ambiguous or you only found secondary sources.
5. Flag privacy issues proactively: any personal data in the workbook, cross-border transfer of the account's data, use of cloud models for sensitive material.

## Rules
- This is a research aid, not legal advice; state that on the sheet's `note:`.
- Keep summaries short and in your own words; cite the document rather than copying text.
- If the {seller} offers payment or messaging products, note card-data scope and security-standard implications as questions to confirm, without asserting certification status.
