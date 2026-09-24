---
name: payment-api-docs
title: Payment API Integration Docs
description: Write merchant-facing integration docs for a payment API endpoint - request/response, signatures, callbacks, error codes, and a security checklist.
triggers: api docs, integration guide, merchant docs, document endpoint
category: engineering
author: SSL Wireless
version: 1.0.0
---

Produce integration documentation for the endpoint(s) the user points at (read the route
code first if it is in the workspace). Use this structure:

1. **Overview** - what the endpoint does, when a merchant calls it, sandbox vs live base URL
   as `[SANDBOX_BASE_URL]` / `[LIVE_BASE_URL]`.
2. **Authentication** - how credentials are sent. Show `[STORE_ID]`, `[STORE_PASSWORD]`,
   `[API_KEY]` placeholders only; never real values.
3. **Request** - method, path, headers, and a field table: name, type, required, constraints
   (length, format, allowed values), description. Amounts: decimal BDT, 2 places.
4. **Response** - success example and field table.
5. **Callbacks / IPN** - payload, how to verify the signature/hash server-side, idempotency,
   retry behaviour, and "always re-validate the transaction with the validation API before
   fulfilling an order".
6. **Error codes** - table of code, meaning, merchant action.
7. **Security checklist** - server-to-server calls only for secrets, TLS 1.2+, never log or
   store card data on the merchant side, validate amount/currency on callback, allowlist
   callback source.
8. **Examples** - curl plus one server language the user prefers, all using placeholders.

Keep examples copy-pasteable, and mark every sensitive value with `[PLACEHOLDER]` form.
