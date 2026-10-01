# NOTICE

This project's own code is MIT licensed — see [LICENSE](LICENSE).

It bundles third-party components. **Each keeps its own terms**, recorded per component in
`skill_catalog/<name>/PROVENANCE.json` with the licence file kept inside the skill folder.
Nothing below is covered by the MIT licence.

## Bundled skill licences

| Skill | Licence | Notes |
|---|---|---|
| `superpowers-*` (11 skills) | MIT — Copyright (c) 2025 Jesse Vincent | `LICENSE.upstream` in each folder |
| `sentry` | MIT | `LICENSE.txt` |
| `cloudflare-agents-sdk`, `cloudflare-wrangler` | Apache-2.0 | `LICENSE.txt` |
| **`docx`** | **Proprietary — © Anthropic, PBC. All rights reserved** | see below |
| **`pdf`** | **Proprietary — © Anthropic, PBC. All rights reserved** | see below |
| `frontend-design` | Apache-2.0 | `LICENSE.txt` in folder |

### `docx` and `pdf` are not open source

These two carry Anthropic's own terms, quoted at the top of each folder's `LICENSE.txt`:

> Use of these materials (including all code, prompts, assets, files, and other components of
> this Skill) is governed by your agreement with Anthropic regarding use of Anthropic's
> services. If no separate agreement exists, use is governed by Anthropic's Consumer Terms of
> Service.

That is **not** an OSI-approved licence and is **not** compatible with the MIT licence above.
They are redistributed here unmodified, with their terms intact, because they are useful
locally. If you are redistributing this project, review those terms first or remove the two
folders. Everything else in `skill_catalog/` is permissively licensed and MIT-compatible.

## Dependencies

Python and npm dependencies keep their own licences; see `requirements.txt`,
`requirements.lock` and `companion/package.json`. `scripts/audit_deps.py` exists to check for
known vulnerabilities.