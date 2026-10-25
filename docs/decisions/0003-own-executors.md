# 0003 Our own executors behind a small job protocol

**Decided by** RIfky, 2026-10-25 (our own Ansible executor first). **Status** accepted.

The core never holds device or cloud credentials and never sends Ansible or OpenTofu text. Executors are separate
services (`executors/`) that take a capability id, device or module names and plain values over
`POST /v1/jobs` with a bearer token, map them to fixed commands, and return only allowed fields. A tenant can have
its own executor on its own network segment (`route-set`).

**Why:** credentials and network reach stay where the customer puts them, and a compromised core cannot run
arbitrary commands. **Cost:** every new kind of change needs an executor capability written here.
