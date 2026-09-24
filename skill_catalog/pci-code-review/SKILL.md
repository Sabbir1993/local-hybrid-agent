---
name: pci-code-review
title: PCI-DSS Code Review
description: Review code that touches cardholder data against PCI-DSS v4.0 secure-coding requirements. Use for payment, checkout, tokenization or logging changes.
triggers: pci review, pci-dss, cardholder data, pan, card data review
category: security
author: SSL Wireless
version: 1.0.0
---

Review the change for PCI-DSS v4.0 risks. Work through each area below, cite `file:line`
for every finding, and rank findings Critical / High / Medium / Low.

## 1. Cardholder data (Req. 3)
- PAN never stored unless strictly required; if stored, rendered unreadable (tokenization,
  strong one-way hash, or AES-256 with managed keys). Flag any plaintext PAN at rest.
- Sensitive authentication data (full track, CVV2/CVC2, PIN block) is **never** stored after
  authorization - not in DB, cache, queues, logs, or temp files.
- Displayed PAN is masked to at most first 6 / last 4.

## 2. Transmission (Req. 4)
- All cardholder data in transit uses TLS 1.2+ with certificate validation on. Flag
  `verify=False`, disabled hostname checks, or plain-HTTP callbacks/webhooks.

## 3. Logging (Req. 10)
- Logs, error messages, stack traces and analytics events contain no PAN, CVV, tokens,
  merchant secrets or passwords. Check exception handlers and request/response dumps.
- Security-relevant actions (auth, refunds, config changes) are audit-logged with user,
  time, action and result.

## 4. Secure coding (Req. 6.2.4)
- Injection: parameterized SQL, no string-built queries or shell commands.
- Input validation on amount, currency, merchant id, callback URLs (allowlist hosts).
- Authn/authz on every endpoint; no IDOR on transaction/order ids.
- Secrets come from a vault/keychain/env - never hard-coded or committed.
- Crypto uses vetted libraries; no custom crypto, no ECB, no MD5/SHA-1 for security.

## 5. Output
Use `[PLACEHOLDER]` for any sensitive value in examples - never echo a real PAN, token
or credential found in the code; describe its location instead.
End with a short verdict: *ship / fix first / blocker*.
