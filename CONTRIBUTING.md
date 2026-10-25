# Contributing to Rezztor Orchestrator

Rezztor Orchestrator is open source, not open contribution. Rezztor Technologies writes all of its code, and pull
requests from outside are closed without review. That keeps the ownership of every line in one place.

You are free to use, change and run the code under the AGPL-3.0, including in production, and to keep your own fork.
What helps most:

- **Bug reports and feature ideas** as GitHub issues: what you did, what happened, what you expected.
- **Security problems** privately, as [SECURITY.md](SECURITY.md) says.

## House rules (for maintainers)

- **Our own code only.** No code, prompt text, test, schema or configuration is copied or adapted from another
  project, whatever its licence. Ideas and published specifications are fine to implement ourselves. Libraries are
  installed as dependencies, never vendored, only under AGPL-compatible licences, and each is listed in
  [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) in the change that adds it. `PLAN.md` §2a has the full rules.
- **Nothing fetches code or configuration at run time**, and nothing installs or updates itself.
- **Authentication fails closed**, and every change path goes through policy, approval and audit.
- **Never commit secrets.**
- Commit subjects read as plain statements of intent.
