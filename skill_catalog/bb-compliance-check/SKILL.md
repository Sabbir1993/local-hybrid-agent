---
name: bb-compliance-check
title: Bangladesh Bank Compliance Check
description: Checklist review of a payment feature or change against Bangladesh Bank guidance (ICT Security Guideline, PSO/PSP rules, data localization, AML/CFT).
triggers: bangladesh bank, bb compliance, psp, pso, bfiu, aml
category: compliance
author: SSL Wireless
version: 1.0.0
---

Assess the feature or change described by the user against the areas below. For each,
answer **Met / Gap / N/A / Needs confirmation**, with one line of evidence or the question
the team must answer. Do not state legal conclusions - flag items for the compliance team.

## Areas
1. **ICT Security Guideline** - access control and least privilege, change management with
   approval records, audit trails, vulnerability management, incident reporting timelines.
2. **Data localization** - customer and transaction data stored and processed in Bangladesh
   unless an approved exception exists. Flag any third-party/cloud egress (APIs, SaaS,
   LLM providers, logging/analytics vendors).
3. **Payment system rules (PSO/PSP)** - settlement timelines, merchant onboarding / KYC,
   customer fund safeguarding, dispute and refund handling, fee disclosure.
4. **AML/CFT (BFIU)** - transaction monitoring hooks, suspicious transaction reporting,
   record retention (typically 5 years), sanctions screening.
5. **Customer protection** - clear consent, complaint handling, BDT amount display and
   receipts, Bangla/English notices where customer-facing.
6. **Business continuity** - DR site, backup and restore tested, RTO/RPO documented.

## Output
A table of the six areas, then a prioritized list of gaps with a suggested owner
(engineering / compliance / operations). Remind the user to verify against the latest
Bangladesh Bank circulars, since guidance is updated regularly.
