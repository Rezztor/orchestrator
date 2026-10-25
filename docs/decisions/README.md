# Decision records

One file per decision that shapes the product: what was decided, why, and what it costs. A later decision that
changes one adds a new record and marks the old one superseded; records are not rewritten.

| No. | Decision | Date |
|---|---|---|
| [0001](0001-clean-room-agpl.md) | Our own code only, AGPL-3.0 with a paid Enterprise subscription | 2026-10-25 |
| [0002](0002-model-proposes-code-disposes.md) | The model proposes, code disposes: policy in Python rules plus YAML | 2026-10-25 |
| [0003](0003-own-executors.md) | Our own executors behind a small job protocol | 2026-10-25 |
| [0004](0004-tenancy-postgres-rls.md) | Tenants in PostgreSQL with forced row-level security | 2026-10-25 |
| [0005](0005-digest-approvals.md) | Approval of a plan digest, re-planned at run time | 2026-10-25 |
| [0006](0006-codes-on-top-of-the-directory.md) | Authenticator codes for approvers, even after directory sign-in | 2026-10-25 |
| [0007](0007-signed-releases-from-tags.md) | Releases built from a tag in a fresh clone and signed with Ed25519 | 2026-10-25 |
| [0008](0008-mcp-read-only.md) | Customer MCP servers are read-only and bound to named tenants | 2026-10-25 |
