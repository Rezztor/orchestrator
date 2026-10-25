# 0002 The model proposes, code disposes

**Decided by** RIfky, 2026-10-25 (policy as Python rules plus YAML). **Status** accepted.

Risk class, permission, approval count, target scope and limits are computed by `orchestrator/policy.py` from the
shipped `policy.yaml`, the tenant's policy and the plugin manifest. The model's view is never asked. A tenant's policy
can only narrow the shipped one. Tool output is data, marked untrusted, and instruction-like text in it is flagged.

**Why:** a hijacked or wrong model must not be able to raise its own permissions or lower a change's risk.
**Cost:** policy changes are code reviews, not a rules editor; a tenant cannot grant more than the shipped ceiling.
