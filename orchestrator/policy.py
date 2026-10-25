"""The policy engine: which role may use which capability, and the limits on a request.

Decided by code from the shipped policy, the tenant's policy and the plugin manifest; never by the model.
"""
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from .plugins import Refused

SHIPPED = yaml.safe_load(Path(__file__).with_name("policy.yaml").read_text())


def parse(text):
    """Validate a tenant policy document. Raises ValueError with the first problem found."""
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict) or set(doc) - {"roles", "deny", "limits"}:
        raise ValueError("a policy has only roles, deny and limits")
    roles, deny, limits = doc.get("roles", {}), doc.get("deny", []), doc.get("limits", {})
    if not (isinstance(roles, dict) and set(roles) <= set(SHIPPED["roles"])
            and all(isinstance(p, list) and all(isinstance(x, str) for x in p) for p in roles.values())):
        raise ValueError(f"roles maps {', '.join(SHIPPED['roles'])} to lists of capability patterns")
    if not (isinstance(deny, list) and all(isinstance(x, str) for x in deny)):
        raise ValueError("deny is a list of capability patterns")
    if not (isinstance(limits, dict) and set(limits) <= set(SHIPPED["limits"])
            and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in limits.values())):
        raise ValueError(f"limits are positive whole numbers for {', '.join(SHIPPED['limits'])}")
    return {"roles": roles, "deny": deny, "limits": limits}


def effective(tenant_text=None):
    tenant = parse(tenant_text) if tenant_text else {"roles": {}, "deny": [], "limits": {}}
    return {"layers": [SHIPPED["roles"], tenant["roles"]],
            "deny": SHIPPED["deny"] + tenant["deny"],
            "limits": {k: min(v, tenant["limits"].get(k, v)) for k, v in SHIPPED["limits"].items()}}


def for_tenant(conn):
    row = conn.execute("SELECT document FROM policies").fetchone()
    return effective(row and row["document"])


def may_use(pol, role, cap_id):
    match = lambda patterns: any(fnmatchcase(cap_id, p) for p in patterns)  # noqa: E731
    shipped, tenant = pol["layers"]
    return (match(shipped.get(role, [])) and (role not in tenant or match(tenant[role]))
            and not match(pol["deny"]))


def check(pol, role, cap, args):
    """Raise Refused unless this role may make this call now. The message names the rule."""
    if not may_use(pol, role, cap["id"]):
        raise Refused(f"policy: the {role} role may not use {cap['id']}")
    if cap["kind"] != "read":
        raise Refused(f"policy: {cap['id']} changes something and needs an approved plan; "
                      "the approval path is not enabled yet")
    most = pol["limits"]["max_targets_per_call"]
    for key, value in args.items():
        if isinstance(value, list) and len(value) > most:
            raise Refused(f"policy: {key} names {len(value)} items; at most {most} in one call")
