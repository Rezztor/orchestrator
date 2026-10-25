# CLAUDE.md

Rezztor Orchestrator: roadmap phases 1–6 built, plus the plan's later controls. Read `PLAN.md` first, then
`README.md` (how to run it) and `docs/decisions/` (why it is the way it is).

Tests need PostgreSQL 16+: `ORCH_TEST_DB_URL=postgresql://owner@127.0.0.1:5432/orch_test pytest` (every table is
dropped first; never point it at a database you care about). Two runs cannot share one database.

- **Our own code only** (`PLAN.md` §2a, `CONTRIBUTING.md`): never copy or adapt code, prompts, tests or schemas from
  any other project, Rezztor's EAgent / netops-eagent included. Implement ideas and published specs ourselves.
  A new dependency needs an AGPL-compatible licence and a line in `THIRD-PARTY-NOTICES.md`.
- Licence: AGPL-3.0, copyright Rezztor Technologies (Private) Limited; outside PRs are closed.
- Coding style: the ponytail approach by default (smallest working change, standard library first, no speculative
  abstractions), never at the cost of the security controls in `PLAN.md`.
- Commit subjects read as plain statements of intent.
- A decision that shapes the product gets a record in `docs/decisions/`.
