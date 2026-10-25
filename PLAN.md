# Rezztor Orchestrator — design plan

**Status:** draft for RIfky's review · **Date:** 2026-10-25 · **Diagrams:** [`design/index.html`](design/index.html)

## 1. What we are building

One agent that an engineer gives a prompt to, and that then **finds out, decides, gets approval, executes,
verifies and reports** across an enterprise's infrastructure and cloud. Every system it touches (Ansible, the
Rezztor device bridge, Azure, SolarWinds, the licence server, the portal, ticketing, SIEM) is reached through a
**plugin**. The orchestrator is the only place where intent becomes action, so it is also the only place where
policy, approval and audit have to be right.

Audience: enterprise customers, self-hosted or air-gapped, several teams or business units per installation,
regulated change management (CAB, CR/SR numbers, four-eyes).

Non-goals for v1: a public SaaS, a marketplace that fetches code at runtime, autonomous goal-setting.

## 2. Research: five open-source harnesses

| Harness | What it is | Take | Avoid |
|---|---|---|---|
| **Hermes Agent** (Nous Research, Feb 2026) | Long-running personal agent: skills, memory, gateways, cron, 6+ terminal backends | Skills the agent **writes after solving a task** and refines in use (agentskills.io format); two-layer memory (curated `MEMORY.md`/`USER.md` snapshot + FTS5 search over past sessions); pluggable execution backends (local, Docker, SSH, Modal…); scheduled natural-language jobs | Personal-assistant trust model: one user, chat gateways, self-written skills go live immediately |
| **Agent Zero** (agent0ai) | Hierarchical prompt-driven framework; the base EAgent was forked from | **Hierarchy**: Agent 0 delegates to subordinates with their own focused context; behaviour in editable prompts; tools/extensions/plugins discovered from the filesystem | Prompt prose as the only policy; code execution in the same container; single-tenant files |
| **DeepSeek Harness** (Aug 2026, MIT, v0.1 preview) | "Everything is a plugin": models, tools, search, skills, sessions, UIs | **Uniform plugin contract** for every moving part; agent presets (tool set + prompt) reusable across sessions; trajectory view (steps, tool calls, timing, tokens); "code mode" that batches tool calls; can host other agents as sub-agents | Developer preview; no documented permission or sandbox model |
| **OpenHands** (V1 SDK) | Coding agent platform with a security subsystem | **Action risk levels** (LOW / MEDIUM / HIGH / UNKNOWN), pluggable **security analyzers** fused worst-case-wins and failing closed; **confirmation policy** as an object (`ConfirmRisky`, unknown ⇒ confirm); every decision recorded in the event stream; sandbox runtime as a separate container over an API | Letting the model rate its own risk as the deciding signal |
| **Goose** (Block / Agentic AI Foundation) | MCP-native agent | Every extension is an **MCP server in its own process**; **per-tool permission** `allow / confirm / deny` in config; YAML **recipes** that declare the extensions, parameters and sub-recipes a workflow needs; extensions start with a cleared environment | `allow` as a global switch that disables every prompt |

A 2026 source study of eleven coding harnesses (arXiv 2609.00006) adds three facts that shape this design: none of
them is built on a general agent framework (hand-rolled async loop), skills are now more widely adopted than MCP,
and **behavioural policy is moving out of prompt prose into configuration**.

### What all five agree on, and what none of them has

They agree: a small loop, tools behind a registry, skills as files, sub-agents with narrow context, a separate
sandbox for code. None of them has what an enterprise infra product needs: **tenants, RBAC, approval that cannot be
replayed, change records, separate read and write credentials, an audit that the agent cannot edit, and a policy
engine that sits outside the model.** That gap is the product.

## 3. What Rezztor already has (and this design keeps)

The EAgent (`eagent-netops-final`) has already paid for most of the hard lessons. 94 ADRs; the ones this design
carries over unchanged:

- **Executors are not the runner** (0023, 0030, 0061): network change and cloud change run in their own executors,
  never in the code-execution sandbox. The application holds only a job token.
- **The approved artefact is a digest** (0053, 0062) and the **approval is a database row used exactly once** (0077).
- **The requesting engineer is the accountable approver** (0039); a waiting change is announced to who may sign (0089).
- **Read and change use separate identities and keys** (0063); change roles deny delete/stop by grant list (0065).
- **The model never sees a stored secret**; output is scrubbed twice (0044); secret aliases resolve only in execution (0086).
- **Free code is off by default and always high risk** (0070); free-form show commands pass a shape check (0046).
- **The agent does not set its own goals** (0031); **induced skills are private until approved** (0025).
- **Automatic sources propose, never overwrite** (0040–0043); drift is shown, never put back (0072).
- **An audit sink is additive only** (0028); auth fails closed (0004); RLS on owned tables (0034).
- **One nftables fence of our own** (0079); no code fetched at runtime; signed offline updates (0080, 0090).

## 4. Design principles

1. **The model proposes, code disposes.** Risk class, permission, approval need and scope are computed by the
   policy engine from the plugin manifest and the target, never taken from the model's own rating.
2. **Read freely, change deliberately.** Discovery is cheap and wide; every change is a plan, a digest, an
   approval row and a verification.
3. **Every integration is a plugin with a manifest.** The manifest is the contract the policy engine reads.
4. **Tool output is data, never instructions.** Anything that came back from a device, API, ticket or web page is
   tainted and cannot raise privileges or start a change on its own.
5. **Least privilege per step.** A sub-agent gets only the capabilities its step needs, for the targets in scope,
   for the time the step takes.
6. **Everything is replayable from the audit**, nothing is editable in it.
7. **Fail closed.** Unknown risk, unreachable policy engine, missing approval, broken analyzer ⇒ stop.

## 5. Architecture

Five planes (see diagram 1 in `design/index.html`):

```
 Interfaces      Web UI · API · ChatOps · Scheduler · Webhooks (ticket opened, alert fired)
      │  prompt + identity (OIDC/SAML, tenant, role)
 Orchestrator    Intake → Planner → Orchestrator loop → Specialist sub-agents → Reporter
 core            Context builder (skills, memory, inventory, runbooks)  · Model gateway
      │  every tool call
 Control plane   Policy engine (OPA/Rego-style rules) · Risk classifier · Approval service
                 Guardrails (input / action / output) · Budget & rate limits · Kill switch
      │  allowed call + job token
 Integration     Plugin host: manifests, schemas, per-plugin process (MCP or in-process),
 plane           credentials by reference · Inventory/CMDB · Event bus
      │  signed job
 Execution       Ansible executor · Device bridge · Cloud executor (OpenTofu) · Code runner pool
 plane           SolarWinds/SIEM/ticket connectors · each with its own identity and network fence
```

Cross-cutting: **Audit ledger** (append-only, hash-chained, SIEM forward), **Tenancy** (tenant id on every row,
RLS, per-tenant keys and executors), **Observability** (trajectories, cost, latency per step).

### 5.1 The orchestrator loop

A hand-rolled async loop (as all eleven studied harnesses do), with explicit states the UI and audit can show:

`INTAKE → DISCOVER → PLAN → POLICY → APPROVE → EXECUTE → VERIFY → (ROLLBACK) → REPORT`

- **Intake**: classify the request (question, investigation, change, scheduled job), resolve tenant, scope and
  targets; refuse what is out of policy before spending tokens.
- **Discover**: read-only tools across plugins, in parallel, via specialist sub-agents (network, cloud, identity,
  monitoring). Results are tainted data.
- **Plan**: a structured **change plan** (JSON): steps, plugin capability per step, targets, expected effect,
  pre-checks, verification, back-out. Not prose.
- **Policy**: the engine evaluates every step: capability allowed for role? target in scope? change window open?
  blast radius under limit? It returns the risk class and the approvers required.
- **Approve**: the plan digest goes to an approval row (single use, expiry, CR/SR number where policy demands).
- **Execute**: the executor re-plans and compares digests; a mismatch supersedes the job (EAgent 0062).
- **Verify**: post-checks from the plan; failure ⇒ automatic back-out where the plugin declares one, else stop
  and page.
- **Report**: summary, evidence, links to the audit and the change record; the ticket is updated.

### 5.2 Multi-agent shape

Agent Zero's hierarchy, with OpenHands' discipline:

- **Orchestrator** owns the conversation, the plan and the approval. It never holds change capabilities itself.
- **Specialists** (network, cloud, identity, monitoring, ticketing) are presets in the DeepSeek sense: a prompt, a
  skill set and an allowed capability list. They get a narrowed context and return findings or a plan fragment.
- **Executors** are not agents. They run approved, digested jobs. No model in the execution path.
- A **reviewer** sub-agent (different model or prompt) critiques every change plan before it reaches a human:
  missing back-out, wider scope than asked, steps not justified by discovery.

## 6. Skills

- Format: `SKILL.md` folders (agentskills.io, as Hermes and the EAgent use), with frontmatter: name, description,
  domain, required capabilities, risk ceiling, tenant visibility, version.
- **Three sources**: shipped (signed, in the release), tenant-authored (reviewed in the UI), **induced** (the agent
  writes one after a successful task, Hermes-style). Induced skills are **private and inert until an admin
  approves** (EAgent 0025), and a skill can never raise its own risk ceiling.
- Skills describe procedure; they cannot carry credentials or call tools that the policy would not allow anyway.
- Each skill can ship **eval cases** (prompt + expected plan shape) run in CI and on upgrade.
- **Recipes** (Goose): a parameterised, saved workflow ("patch a switch stack", "onboard a subscription") that pins
  the plugins and skills it uses; schedulable.

## 7. Guardrails (what to do, what not to do)

Three layers, all code, all configurable per tenant, all audited.

| Layer | Checks | Default action |
|---|---|---|
| **Input** | prompt-injection classifier on user text **and on every tool result**; secret detection in chat (redirect to the secure form); scope words ("all", "every site") flagged; language/policy filter | block or ask |
| **Action** | capability allow-list per role and per sub-agent; target in scope (tenant, site, tag); risk class from manifest + target criticality; blast-radius limit (devices, resources per change); change window / freeze calendar; rate limit; free code off | deny / require approval |
| **Output** | secret and PII redaction (twice, EAgent 0044); no raw credentials or config secrets in replies; claims about state must cite a tool result from this run | redact |

**Hard rules the product ships with** (tenant can tighten, never loosen): no delete/stop/wipe without a named
plugin capability marked `destructive` and two approvers; no change outside a declared scope; no change during a
freeze; no self-modification of policy, skills, or plugins; no goal the user did not give (0031); no network
egress from the core except to declared plugin endpoints.

## 8. Agent protection

Threats specific to an agent that can change infrastructure, and the control for each:

| Threat | Control |
|---|---|
| **Indirect prompt injection** (a device banner, a ticket, a log line says "ignore previous…") | Taint tracking: tool results are wrapped and labelled; tainted text can never be the source of a capability call's arguments for a change without passing the plan → policy → human path. Input classifier also runs on tool results. |
| **Model rates its own risk low** | Risk is computed from the manifest and target; the model's rating is a second signal only (OpenHands analyzers, worst-case wins, fail closed). |
| **Approval replay / tampering** | Digest approval row used once, expiry, approver ≠ requester for high risk (EAgent 0062/0077). |
| **Credential theft** | Credentials live in the executor or a vault; plugins get references; the model sees names and fingerprints only; separate read and change identities. |
| **Malicious or vulnerable plugin** | Plugins signed and shipped in the release, no runtime install; each runs in its own process/container with a cleared environment (Goose) and only its declared egress; manifest declares capabilities and the host enforces them. |
| **Skill poisoning** | Induced and tenant skills reviewed before use; skills cannot widen permissions. |
| **Runaway loop / cost** | Step, token, time and money budgets per request and per tenant; circuit breaker on repeated failures; **kill switch** per tenant and global that cancels in-flight jobs. |
| **Model provider leak** | Model gateway with per-tenant routing: local model for device data where required (EAgent 0052), redaction before any external call, no training-use providers. |
| **Insider misuse** | RBAC, four-eyes on high risk, every prompt and action attributed to a person, audit forwarded to the customer SIEM. |

## 9. Plugin contract

Every integration is a folder with a `plugin.yaml` and code. The manifest is what the policy engine reads:

```yaml
name: ansible
version: 1.4.0
signed_by: rezztor-release
transport: process          # process (MCP stdio/HTTP) | inprocess (shipped only)
egress: [executor.ansible.local:8443]
credentials: [ansible.executor_token]   # references, resolved by the host
capabilities:
  - id: ansible.facts.read
    kind: read
    risk: low
    targets: device
    schema: schemas/facts_read.json
  - id: ansible.playbook.run
    kind: change
    risk: medium            # floor; target criticality can raise it
    targets: device
    plan: true              # must produce a dry-run/check-mode plan with a digest
    verify: true            # must declare post-checks
    rollback: playbook      # how back-out is done, or "none" (raises risk)
    schema: schemas/playbook_run.json
```

Rules: a `change` capability must support **plan → digest → apply → verify**; no `rollback` ⇒ risk +1; a capability
not in the manifest does not exist. Transport is **MCP-compatible** so third-party MCP servers can be wrapped, but a
wrapped server still needs a Rezztor manifest and signing before it is enabled.

First-party plugins (in order): **inventory**, **Ansible executor**, **Rezztor device bridge**, **Azure (cloud
executor)**, **SolarWinds**, **SIEM forward**, **ticketing** (ServiceNow/Jira as CR source), **mail**,
**licence/portal** (Rezztor's own products).

## 10. Approvals and change management

- Risk classes: `read`, `change-low`, `change-medium`, `change-high`, `destructive`.
- Policy maps class × target criticality × environment (prod/non-prod) to: auto, one approver, two approvers
  (four-eyes, approver ≠ requester), plus CR/SR number, plus change window.
- Approval is of the **plan digest**; any difference at apply ⇒ new card.
- Approval rows are single use and expire; break-glass exists, needs a reason, pages the tenant admin, and is
  flagged in the audit.
- Cards show a masked summary, the diff, the back-out, the blast radius and the reviewer sub-agent's notes.
- Approvals from Web UI, ChatOps buttons or the ticketing system (CAB approval writes the row).

## 11. Audit

- Every event (prompt, plan, policy decision with the rule that fired, approval, job, tool call, result digest,
  model call metadata, cost) goes to an **append-only ledger**: PostgreSQL table with a hash chain (each row hashes
  the previous), periodic anchor signed by the instance key.
- The agent and the application role can insert, never update or delete.
- Forwarded to the customer SIEM (syslog/CEF/JSON, EAgent `_ea_siem_forward`).
- **Trajectory view** (DeepSeek Harness): per request, every step, tool call, timing and token use, linked to the
  audit rows.
- Export per change for auditors: request → plan → approvals → execution → verification evidence.

## 12. Multi-tenancy

- Hierarchy: **installation → tenant (customer or business unit) → workspace (team/site) → user**.
- `tenant_id` on every row, PostgreSQL **RLS forced** on every tenant table (extends EAgent 0034 from user to tenant).
- Per-tenant: policy bundle, skills, plugins enabled, credentials (separate vault namespace and keys), model
  routing, budgets, executors (an executor is bound to one tenant and its network segment).
- Roles: `platform-admin`, `tenant-admin`, `approver`, `operator`, `viewer`, plus custom roles built from
  capabilities.
- Identity from the customer's directory (OIDC/SAML, SCIM for groups), MFA required for approvers.

## 13. Deployment

Docker Compose like the EAgent: `core`, `policy`, `plugin-host`, executors, `postgres` (+pgvector), `runner`,
local model optional. Air-gapped by default; signed offline updates; nftables guard. HA (two cores, Postgres
replica) is a later phase.

## 14. Build approach and roadmap

**Recommended:** a new core in this repo (Python 3.12, same stack as EAgent so modules port directly), reusing the
EAgent's proven modules (approval store, change store, credential scrub, RLS helpers, executors). The EAgent keeps
shipping; its Ansible and Azure executors become the orchestrator's first execution plugins.

| Phase | Outcome | Rough size |
|---|---|---|
| 0 | Decisions below, repo skeleton, CI runner, ADR folder | days |
| 1 | Core loop with states, model gateway, audit ledger, tenants + RLS, Web UI chat; read-only plugins (inventory, Ansible facts) | 3–4 wks |
| 2 | Policy engine, risk classifier, guardrails (input/action/output), taint tracking, budgets, kill switch | 3 wks |
| 3 | Change path: plan → digest → approval row → execute → verify → rollback; Ansible + device bridge change capabilities | 4 wks |
| 4 | Skills (shipped, tenant, induced with review), recipes, scheduler, specialist sub-agents, reviewer agent | 3 wks |
| 5 | Azure executor, SolarWinds, ticketing (CR source), SIEM, ChatOps approvals | 4 wks |
| 6 | Hardening: red-team suite (prompt injection corpus, approval replay, plugin escape), eval suite, HA, release build | 3 wks |

## 15. Decisions for RIfky

1. **Build approach** — (a) new core in this repo, EAgent modules reused, EAgent executors become plugins
   **[recommended]**; (b) grow the EAgent itself into the orchestrator; (c) start from another harness (OpenHands SDK
   or Hermes) and add the enterprise layer.
2. **Policy engine** — (a) rules in Python, data in YAML, per tenant **[recommended for v1]**; (b) embed OPA/Rego.
3. **First integration after inventory** — (a) Ansible executor **[recommended, already built]**; (b) the device
   bridge; (c) Azure.
4. **Product name** — working name "Rezztor Orchestrator"; repo `Rezztor/orchestrator`.

## Sources

- Hermes Agent: <https://github.com/NousResearch/hermes-agent>, <https://agentic-ai.readthedocs.io/en/latest/AgentPlatforms/hermes-agent/>
- Agent Zero: <https://github.com/agent0ai/agent-zero>
- DeepSeek Harness: <https://www.datacamp.com/tutorial/deepseek-harness>, <https://cryptobriefing.com/deepseek-harness-open-source-developer-preview/>
- OpenHands security: <https://docs.openhands.dev/sdk/arch/security>, paper <https://arxiv.org/pdf/2407.16741>
- Goose extensions: <https://mintlify.wiki/block/goose/concepts/extensions>
- Harness engineering study: <https://arxiv.org/abs/2609.00006>
- EAgent ADRs: `Rezztor/eagent-netops-final` `docs/internal/decisions/`
