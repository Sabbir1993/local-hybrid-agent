---
name: incident-postmortem
title: Incident Postmortem
description: Turn incident notes, logs and timelines into a blameless postmortem with impact, root cause, and tracked action items.
triggers: postmortem, post-mortem, incident review, rca, root cause
category: operations
author: SSL Wireless
version: 1.0.0
---

Write a blameless postmortem from the material the user gives you. Ask for anything
missing from the "Needed" list before drafting.

**Needed:** detection time, resolution time, affected services, customer/merchant impact
(failed or delayed transactions, count and BDT value), timeline notes, what fixed it.

## Structure
1. **Summary** - 3 sentences: what broke, impact, how it was resolved.
2. **Impact** - duration, transactions affected, merchants affected, settlement impact,
   whether cardholder data was exposed (if *yes or unknown*, flag for the security team and
   note PCI-DSS incident response and Bangladesh Bank reporting obligations).
3. **Timeline** - UTC+6 (Asia/Dhaka) timestamps, detection → mitigation → resolution.
4. **Root cause** - use 5-whys; separate the trigger from the underlying cause.
5. **What went well / what didn't** - detection, alerting, runbooks, communication.
6. **Action items** - table: action, type (prevent / detect / mitigate), owner, due date.
   Every root cause needs at least one *prevent* item.

Describe people by role, not name. Mask any PAN, token or credential that appears in pasted
logs as `[REDACTED]`.
