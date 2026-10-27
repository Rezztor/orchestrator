# Rezztor Orchestrator

The enterprise agent that takes a prompt and orchestrates infrastructure and cloud work across a customer's estate
and Rezztor's products: discover, plan, get approval, execute through plugins, verify, report.

Phases 1 to 6: the request loop, tenants with row-level security, the hash-chained audit ledger, plugins (inventory,
Ansible facts and reviewed Ansible playbooks through our own executor), the policy engine and guardrails, the change
path with approvals, skills, recipes and schedules, specialist sub-agents and a change reviewer, web sign-in, and the
integrations below, then hardening: red-team and eval suites, running more than one copy, and a signed release.

## Install

On a Linux host with Docker (with the compose plugin), curl and openssl:

```bash
curl -fsSL https://raw.githubusercontent.com/Rezztor/orchestrator/main/deploy/install.sh | bash
```

It asks for the model endpoint (any OpenAI-compatible URL ending in `/v1`; empty to set it later), then writes
`./rezztor-orchestrator` with generated passwords and tokens and a self-signed certificate, pulls the images from
`ghcr.io/rezztor/orchestrator-*`, starts them on port 443 and prints the sign-in for the first organisation's
administrator, who enrols an authenticator on first sign-in. `ORCH_HTTPS_PORT`, `ORCH_ORG`, `ORCH_ADMIN`,
`ORCH_MODEL_URL`, `ORCH_MODEL` and `ORCH_MODEL_KEY` set beforehand skip the questions. A model on the same host is
reached as `host.docker.internal` (an Ollama there must listen on more than 127.0.0.1). Upgrade: set `ORCH_VERSION`
in its `.env`, then `docker compose pull && docker compose up -d`.
Administration runs from that folder as `docker compose run --rm migrate python -m orchestrator COMMAND ...`; a
command that reads a file needs it mounted, for example `-v "$PWD/policy.yaml:/tmp/p.yaml:ro"` and then `/tmp/p.yaml`.
Webhook receivers behind an internal CA: put the CA bundle in `certs/` and `SSL_CERT_FILE=/certs/BUNDLE` in
`secrets.env`.

## Run it from source

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

The page (`orchestrator/static/`, plain JavaScript, no build step, nothing loaded from elsewhere) has Overview, Ask,
Changes, Activity (the ledger, approvers and administrators), Recipes, Devices and Skills, and for a tenant
administrator People and policy and Integrations: add people and set roles (a role change signs the person out), reset
an authenticator, add devices and mark them critical, edit the tenant's policy, add or remove webhooks and triggers,
and stop or resume agent work. Settings that name a server secret (`route-set`, `oidc-set`) and installation-wide
ones stay on the command line with the installation's owner: a tenant administrator who could set them could point a
secret at a server of their own.

Phase 3 change path: the agent can only propose. A change capability's dry run (Ansible check mode with diffs)
becomes a PLANNED change with a digest; its risk is the manifest's, one class higher without a rollback playbook and
one higher for a critical device (`device-critical`). People approve that digest on the page (`approver` or
`tenant-admin`, never the person who asked; `approvals` in the policy sets how many per risk class), then the
requester or an approver runs it once, inside the `change_window` if one is set and only while the kill switch is
off. The executor plans again and refuses on any difference, applies, verifies with another check-mode run and runs
`NAME.rollback.yml` when the apply or the verification fails. Plans expire after `change_ttl_minutes`.

Phase 4:

- **Skills** are `SKILL.md` files (front matter `name`, `description`, `capabilities`, then the procedure). Shipped ones
  live in `skills/`; a tenant administrator adds the tenant's own with `skill-add`. The agent sees the names of the
  skills its role can use and reads one with `skills.read`. It may propose a new one (`skills.propose`), which stays
  inert until a tenant administrator approves it on the page. A skill grants nothing: tool calls are still checked.
- **Specialists** (`agents/*.yaml`): the orchestrator can hand one investigation to a specialist with
  `agents.delegate`. It gets only its own capability patterns, never more than the user's role, cannot delegate
  further, and spends the same request budget.
- **Reviewer**: every change plan goes to a second, tool-less model call (`ORCH_REVIEW_MODEL` if set) whose notes
  are shown on the change card. Advice only; people decide.
- **Recipes and schedules**: `recipe-add` saves a prompt with `{placeholders}`; people run it from the page or with
  `recipe-run`, and `schedule-add` runs it as a named user every N minutes while `python -m orchestrator scheduler`
  runs. A recipe run is an ordinary request: same role, policy and change path.

Phase 5 integrations:

- **Cloud executor** (`python -m executors.tofu_executor`): reviewed OpenTofu modules in `ORCH_TOFU_MODULES` (one
  folder each, `README.md` first line says what it does; `examples/tofu/azure-resource-group` is an Azure one), working
  copies and local state in `ORCH_TOFU_STATE`, provider credentials (`ARM_*`) in its own environment. The agent
  proposes `cloud.module.apply` (module, workspace, variables); the plan shows each resource change with sensitive
  values masked. The executor plans again at run time and refuses on any difference, applies the saved plan and
  verifies that nothing is left to change. There is no automatic rollback, so these changes are high risk. Core:
  `ORCH_TOFU_URL`, `ORCH_TOFU_TOKEN`.
- **SolarWinds** (`solarwinds.nodes.status`): node status for inventory devices through a fixed SWQL query with
  parameters. `ORCH_SOLARWINDS_URL` (https://host:17774), `ORCH_SOLARWINDS_USER`, `ORCH_SOLARWINDS_PASSWORD`,
  `ORCH_SOLARWINDS_CA`.
- **ServiceNow change records**: `change_record: [high, destructive]` in the policy makes those risk classes need a
  record number on the change (attached on the page). Before the run the record must be approved, Scheduled or
  Implement, and inside its planned window; afterwards the outcome goes into its work notes. `ORCH_SNOW_URL`,
  `ORCH_SNOW_USER`, `ORCH_SNOW_PASSWORD`, `ORCH_SNOW_CA`.
- **SIEM**: `python -m orchestrator siem-forward` (owner role) sends every tenant's ledger as RFC 5424 syslog with a
  JSON message to `ORCH_SIEM_TARGET` (`udp://`, `tcp://` or `tls://host:port`), keeping its place per tenant.
- **ChatOps**: `webhook-add TENANT teams|slack|json URL` posts planned, approved, rejected and finished changes and
  kill-switch events, with a link (`ORCH_PUBLIC_URL`) to the change on the page. People approve on the page, where
  they are signed in; a chat button cannot prove who pressed it.

Phase 6 hardening:

- **Red team** (`tests/test_redteam.py`): the model is assumed hijacked. Every line of
  `tests/redteam/injections.txt` must be flagged when it shows up in tool output, and a scripted attack (tools that do
  not exist, too many targets, another tenant's device, smuggled arguments, a made-up specialist) must leave nothing
  but one PLANNED change that the hijacked account cannot approve and another tenant cannot see. Add every new attack
  to the corpus.
- **Evals** (`evals/cases.yaml` and each skill's `evals.yaml`, `python -m orchestrator eval`): prompts with the capabilities a good
  answer must call and must never touch, run against the configured model. Each case gets a new `eval-*` tenant and
  canned results, so no device or cloud is reached; point it at a scratch database.
- **More than one copy**: web processes share the sign-in lockout through the database, schedulers claim each
  schedule before running it and mark runs whose process died FAILED, SIEM forwarders take turns, and `GET /healthz`
  answers 200 while the process reaches the database.
- **Deployment**: `Dockerfile` (targets `core`, `ansible`, `tofu`, `postgres`) and `deploy/compose.yaml`:

  ```bash
  cd deploy && cp env.example .env && $EDITOR .env    # put certs/tls.crt and certs/tls.key beside it
  mkdir -p ansible/playbooks && $EDITOR ansible/inventory
  docker compose up -d                                 # --profile cloud, --profile siem for those
  docker compose run --rm migrate python -m orchestrator tenant-add acme alice
  docker compose run --rm migrate python -m orchestrator set-password <tenant-id> alice
  ```

  Pushing a tag `vX.Y.Z` runs the tests and publishes the four images to `ghcr.io/rezztor/orchestrator-*`
  (`.github/workflows/release.yml`). For a site with no internet: `scripts/build_release.sh v0.7.0 release.key` builds
  from the pushed tag in a fresh clone; the customer runs `verify_release.sh RELEASE_DIR rezztor-release.pub`, then `docker load -i RELEASE_DIR/orchestrator-*-images.tar.gz`.

After the roadmap, the plan's remaining controls:

- **Authenticator codes**: approvers and tenant administrators give a six-digit code (any TOTP app) after the password
  or the directory; the first sign-in shows a key to enrol. A code counts once, five wrong ones end the session, and
  `mfa-reset TENANT USER` forgets a lost phone.
- **Directory sign-in** (OpenID Connect: Entra ID, Okta, Keycloak and the like): `oidc-set TENANT ISSUER CLIENT_ID
  SECRET_ENV [CLAIM]`, the client secret in that environment variable (`deploy/secrets.env`), and
  `ORCH_PUBLIC_URL/oidc/callback` registered as the redirect URI. The user must already exist in the tenant (matched
  on `preferred_username` unless another claim is named); the role stays the tenant's.
- **Break-glass**: a tenant administrator (not the requester) can approve a change alone with a reason; it may run
  outside the change window, never in a freeze and never when destructive. Every webhook hears of it and the ledger and
  the card flag it.
- **Freeze**: `freeze: [{start: "2026-12-20T00:00", end: "2027-01-05T00:00", reason: Year end}]` in the policy (UTC):
  nothing runs inside one.
- **Budgets**: `max_seconds` per request and `max_tokens_per_day` per tenant join the step, token and tool-call limits.
- **Signed anchors**: with `ORCH_ANCHOR_KEY` (Ed25519 PEM) the scheduler signs each tenant's newest ledger row hourly
  into the ledger itself, and so into the SIEM; `audit-verify` checks them with `ORCH_ANCHOR_PUB`.
- **Steps and evidence**: each answer on the page opens its steps from the ledger; a finished change exports its
  evidence (request, plan, approvals, run, ledger rows, chain state) as JSON for an auditor.
- **CEF**: `ORCH_SIEM_FORMAT=cef` sends ArcSight CEF instead of JSON.
- **A tenant's own executors and model**: `route-set TENANT ansible|tofu|model URL SECRET_ENV [MODEL]` binds an
  executor on that tenant's network segment, or a model endpoint (for example a local model for device data that must
  not leave), to one tenant; the secret stays in the named environment variable (`deploy/secrets.env`).
- **Triggers**: `trigger-add TENANT RECIPE USER` prints a secret address; another system (a SolarWinds alert action,
  a ServiceNow business rule) posts JSON there and the recipe runs as USER with its placeholders from the JSON's
  top-level fields, under the same policy, limits and change path as any request.
- **The customer's own MCP servers**, read only: manifests in `ORCH_MCP_PLUGINS` (`deploy/mcp/`), described in
  `orchestrator/mcp.py`. Capabilities are `mcp.NAME.<...>`, bound to the tenants the manifest names.
- **Skill evals**: a skill folder may hold `evals.yaml`; `python -m orchestrator eval` runs them with `evals/cases.yaml`.
- **CI**: `.github/workflows/test.yml` runs the suite on every push, on GitHub's hosted runners.
- **Decision records**: [`docs/decisions/`](docs/decisions/README.md).

The Ansible executor is a separate service with its own Ansible inventory and credentials; the core sends it only a
capability, device names and, for a playbook, its name and variables. A playbook reads its variables as
`{{ orch.NAME }}`; values are plain text, numbers or true/false, never templates:

```bash
pip install -r executors/requirements.txt
ORCH_ANSIBLE_TOKEN=<32+ chars> ORCH_ANSIBLE_INVENTORY=/etc/orch/inventory.yml \
  ORCH_ANSIBLE_PLAYBOOKS=/etc/orch/playbooks python -m executors.ansible_executor   # NAME.yml, NAME.rollback.yml
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

Every line is written by Rezztor Technologies; no code is taken from other projects. Everything here, the images
on GitHub's container registry included, is free to use; nothing is sold and no licence key is needed (decision
0010). Questions and bugs go to the issues. "Rezztor" and its logo are trademarks and are not licensed with the code: see
[TRADEMARKS.md](TRADEMARKS.md). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
