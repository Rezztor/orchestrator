# Rezztor Orchestrator

The enterprise agent that takes a prompt and orchestrates infrastructure and cloud work across a customer's estate
and Rezztor's products: discover, plan, get approval, execute through plugins, verify, report.

Phases 1 and 2: the request loop, tenants with row-level security, the hash-chained audit ledger, read-only plugins
(inventory, Ansible facts through our own executor) and web sign-in with chat.

## Run it

```bash
pip install -r requirements.txt -r requirements.dev.txt
export ORCH_DB_URL=postgresql://owner:...@127.0.0.1:5432/orch      # owner role: migrates, creates tenants
export ORCH_DB_APP_PASSWORD=...                                    # password migrate gives the orch_app role
export ORCH_DB_APP_URL=postgresql://orch_app:...@127.0.0.1:5432/orch
export ORCH_MODEL_URL=http://127.0.0.1:8000/v1 ORCH_MODEL=...     # any OpenAI-compatible endpoint; ORCH_MODEL_KEY if hosted
python -m orchestrator migrate
python -m orchestrator tenant-add acme alice                       # prints the tenant id
python -m orchestrator ask <tenant-id> alice "Which devices are in Colombo?"
python -m orchestrator audit-verify <tenant-id>
python -m orchestrator set-password <tenant-id> alice               # at least 12 characters
python -m orchestrator.web                                         # sign in and chat at http://127.0.0.1:8080
```

Phase 2 guardrails: each role gets only the capabilities `orchestrator/policy.yaml` allows, and a tenant's own policy
(`policy-set`) can only narrow that; limits cap steps, tool calls, tokens, targets per call and requests per hour.
Prompts holding a secret are refused and stored redacted; tool output and answers are redacted before the model or
the browser sees them; instruction-like text in tool output is flagged. A tenant administrator can stop all agent
work from the page or with `stop`; `stop-all` stops the whole installation.

The session cookie is `Secure`: browsers accept it on `localhost`; anywhere else serve HTTPS (`ORCH_WEB_CERT` and
`ORCH_WEB_KEY`, or TLS in front).

The Ansible executor is a separate service with its own Ansible inventory and credentials; the core sends it only a
capability and device names:

```bash
pip install -r executors/requirements.txt
ORCH_ANSIBLE_TOKEN=<32+ chars> ORCH_ANSIBLE_INVENTORY=/etc/orch/inventory.yml python -m executors.ansible_executor
# in the core's environment: ORCH_ANSIBLE_URL=http://127.0.0.1:8470 ORCH_ANSIBLE_TOKEN=<same token>
```

Tests need PostgreSQL 16+ and an empty database: `ORCH_TEST_DB_URL=postgresql://owner@127.0.0.1:5432/orch_test pytest`.
Without it they skip.

- [`PLAN.md`](PLAN.md): research, architecture, guardrails, agent protection, plugin contract, approvals, audit,
  multi-tenancy, roadmap and the open decisions.
- [`design/index.html`](design/index.html): the same design as a page with architecture diagrams. Open it in a
  browser.

## Licence

Copyright (C) 2026 Rezztor Technologies (Private) Limited.

Rezztor Orchestrator is free software under the GNU Affero General Public License version 3 ([LICENSE](LICENSE)).
You may run it in your enterprise and change it. If you run a modified version as a network service, you must offer
its source to the people who use it.

Every line is written by Rezztor Technologies; no code is taken from other projects. Builds signed by Rezztor,
support, patches for supported releases and the integrations that come with them are part of the **Rezztor
Enterprise** subscription. "Rezztor" and its logo are trademarks and are not licensed with the code: see
[TRADEMARKS.md](TRADEMARKS.md). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
