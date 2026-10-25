# 0001 Our own code only, AGPL-3.0 with a paid Enterprise subscription

**Decided by** RIfky, 2026-10-25. **Status** accepted.

Rezztor Orchestrator is written from scratch in this repository. No code, prompts, tests or schemas are copied or
adapted from any other project, Rezztor's own EAgent and netops-eagent included; other projects are studied for ideas
only (PLAN.md §2, §2a). It is licensed AGPL-3.0, with signed builds, support, patches and integrations sold as the
Rezztor Enterprise subscription; outside pull requests are closed.

**Why:** clear ownership lets Rezztor relicense and sell it, and the AGPL keeps a hosted fork from closing the code.
**Cost:** everything is built here, including what libraries elsewhere already do. Dependencies must have an
AGPL-compatible licence and a line in THIRD-PARTY-NOTICES.md. The repository goes public after RIfky's first test on
real equipment, not before.
