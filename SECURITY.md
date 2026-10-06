# Security

## Threat model & deployment envelope

Single box, single process, SQLite, TLS-terminates-elsewhere. Trust root:
`auth.db` (argon2 password hashes, sha256 session/token/backup-code hashes,
TOTP secrets). A filesystem read of the DB directory bypasses every control
here. Bind to `127.0.0.1` and put TLS in front of anything remote — the server
sets no HSTS/security headers itself.

## Accepted residual risks (register reviewed 2026-10-01)

| ID | Risk | Accepted because |
|---|---|---|
| R1 | 8-char min passwords (PCI asks 12) | argon2 + lockout (10/30min) + per-IP throttle (30/15min) + MFA available; revisit for regulated data |
| R2 | TOTP secrets decryptable in `auth.db` | no KEK infra; on-disk key next to the DB adds theatre; DB dir is the trust root |
| R3 | API tokens up to 90 days | fenced perms never attach to tokens; scopes intersect at use; minting needs verified session |
| R4 | Throttles + MFA tickets in-memory | restart zeroes IP table / voids tickets fail-closed; single-tenant box |
| R5 | (`eval_results*.json` tracked) | **closed 2026-10-06** — gitignored |
| R6 | No HA | one process; a restart drops streams and timers, nothing is lost but time |
| R7 | SQLite single-writer | one box, one team |
| R8 | Plain HTTP on the wire | loopback-only default; tunnel required for anything remote |
| R9 | Server-trusting companion | `approved_in_app` skips the device prompt; scoped to approved folders |
| R10 | Sub-agent `_ws_changes` leak into parent diff set | correctness wart, not a boundary; context-local fix if it matters |
| R11 | No OCR engine | image-only PDFs fail closed; no native OCR by design (supply-chain surface) |
| R12 | Footnotes/endnotes/charts have no extraction path | DOCX body+tables+headers pinned by ingestion tests; revisit if corpuses need it |

## What the code does enforce

- **Auth:** argon2 passwords, TOTP MFA, LDAP SSO, RBAC (20 permissions),
  API tokens, CSRF + security headers, body-size + rate limits, per-IP login
  throttle + per-account lockout. Session cookies carry only a random token;
  only its sha256 is stored.
- **Payment data:** PAN detection (Luhn-checked) blocks card numbers at input,
  masks them at output, on cloud egress, and before KB chunking/embedding
  (`core/pan.py`).
- **SSRF:** `core/net_guard.py` re-checks every hop including redirects for
  private/loopback/link-local/mapped/NAT64 addresses before server-side
  fetches. Residual rebinding gap documented; keep this host network-isolated
  from PCI scope.
- **Shell:** permission-gated with allow-patterns; compound commands
  (`& | < > ^ ;`, `$(...)`) and risky argument flags (`--ext-diff`, `-c`, UNC
  paths) never auto-approve. Agent commands run on the user's machine through
  the Companion, not on the server.
- **Supply chain:** hash-locked deps (`requirements.lock`), `pip-audit` gate
  in CI, `NOTICE.md` tracks bundled third-party skill licences (note: the
  `docx`/`pdf` skills are Anthropic-proprietary, not OSI licences).

## Reporting a vulnerability

This is a local tool without a public maintainer list. If you found a flaw,
open an issue rather than posting details of a live exploit — the threat model
above presumes a trusted single-user box, and the same discipline that applies
to payment-adjacent code applies here: do not put cardholder data, credentials
or PANs into this app, and keep it network-isolated from PCI scope.
