# CLAUDE.md

Rezztor Orchestrator, design stage. Read `PLAN.md` first.

- **Our own code only** (`PLAN.md` §2a, `CONTRIBUTING.md`): never copy or adapt code, prompts, tests or schemas from
  any other project, Rezztor's EAgent / netops-eagent included. Implement ideas and published specs ourselves.
  A new dependency needs an AGPL-compatible licence and a line in `THIRD-PARTY-NOTICES.md`.
- Licence: AGPL-3.0, copyright Rezztor Technologies (Private) Limited; outside PRs are closed.
- Coding style: the ponytail approach by default (smallest working change, standard library first, no speculative
  abstractions), never at the cost of the security controls in `PLAN.md`.
- Commit subjects read as plain statements of intent.
