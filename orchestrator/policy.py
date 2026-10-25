"""The policy engine: which role may use which capability, the limits on a request, and what a change needs.

Decided by code from the shipped policy, the tenant's policy and the plugin manifest; never by the model.
"""
import re
from datetime import timedelta
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from .plugins import Refused

SHIPPED = yaml.safe_load(Path(__file__).with_name("policy.yaml").read_text())
RISKS = ["low", "medium", "high", "destructive"]
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HHMM = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def parse(text):
    """Validate a tenant policy document. Raises ValueError with the first problem found."""
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict) or set(doc) - {"roles", "deny", "limits", "approvals", "change_window"}:
        raise ValueError("a policy has only roles, deny, limits, approvals and change_window")
    roles, deny, limits = doc.get("roles", {}), doc.get("deny", []), doc.get("limits", {})
    approvals, window = doc.get("approvals", {}), doc.get("change_window")
    if not (isinstance(roles, dict) and set(roles) <= set(SHIPPED["roles"])
            and all(isinstance(p, list) and all(isinstance(x, str) for x in p) for p in roles.values())):
        raise ValueError(f"roles maps {', '.join(SHIPPED['roles'])} to lists of capability patterns")
    if not (isinstance(deny, list) and all(isinstance(x, str) for x in deny)):
        raise ValueError("deny is a list of capability patterns")
    if not (isinstance(limits, dict) and set(limits) <= set(SHIPPED["limits"])
            and all(positive(v) for v in limits.values())):
        raise ValueError(f"limits are positive whole numbers for {', '.join(SHIPPED['limits'])}")
    if not (isinstance(approvals, dict) and set(approvals) <= set(RISKS)
            and all(positive(v) for v in approvals.values())):
        raise ValueError(f"approvals are positive whole numbers for {', '.join(RISKS)}")
    if window is not None and not (
            isinstance(window, dict) and set(window) == {"days", "start", "end"}
            and isinstance(window["days"], list) and window["days"] and set(window["days"]) <= set(DAYS)
            and all(isinstance(window[k], str) and HHMM.fullmatch(window[k]) for k in ("start", "end"))):
        raise ValueError(f"change_window is {{days: [{', '.join(DAYS)}], start: HH:MM, end: HH:MM}} in UTC")
    return {"roles": roles, "deny": deny, "limits": limits, "approvals": approvals, "change_window": window}


def effective(tenant_text=None):
    tenant = parse(tenant_text) if tenant_text else parse("{}")
    return {"layers": [SHIPPED["roles"], tenant["roles"]],
            "deny": SHIPPED["deny"] + tenant["deny"],
            "limits": {k: min(v, tenant["limits"].get(k, v)) for k, v in SHIPPED["limits"].items()},
            "approvals": {k: max(v, tenant["approvals"].get(k, v)) for k, v in SHIPPED["approvals"].items()},
            "windows": [w for w in (SHIPPED["change_window"], tenant["change_window"]) if w]}


def for_tenant(conn):
    row = conn.execute("SELECT document FROM policies").fetchone()
    return effective(row and row["document"])


def stopped(conn):
    """The reason the kill switch gives, installation-wide or for this tenant, or None."""
    row = conn.execute("SELECT reason FROM installation_kill UNION ALL SELECT reason FROM kill_switch LIMIT 1").fetchone()
    return row and row["reason"]


def may_use(pol, role, cap_id):
    match = lambda patterns: any(fnmatchcase(cap_id, p) for p in patterns)  # noqa: E731
    shipped, tenant = pol["layers"]
    return (match(shipped.get(role, [])) and (role not in tenant or match(tenant[role]))
            and not match(pol["deny"]))


def check(pol, role, cap, args):
    """Raise Refused unless this role may make this call now. The message names the rule."""
    if not may_use(pol, role, cap["id"]):
        raise Refused(f"policy: the {role} role may not use {cap['id']}")
    most = pol["limits"]["max_targets_per_call"]
    for key, value in args.items():
        if isinstance(value, list) and len(value) > most:
            raise Refused(f"policy: {key} names {len(value)} items; at most {most} in one call")


def risk(cap, rollback, critical):
    """The manifest's risk, one class higher with no rollback and one higher for a critical target.

    Only the manifest can make a change destructive; the model's opinion is never asked.
    """
    level = RISKS.index(cap["risk"])
    if cap["risk"] != "destructive":
        level = min(RISKS.index("high"), level + (not rollback) + bool(critical))
    return RISKS[level]


def window_open(pol, now):
    """True when `now` (UTC) is inside every change window the policy sets. A window belongs to the day it opens."""
    def inside(w):
        t, day, yesterday = now.strftime("%H:%M"), DAYS[now.weekday()], DAYS[(now - timedelta(days=1)).weekday()]
        if w["start"] <= w["end"]:
            return day in w["days"] and w["start"] <= t < w["end"]
        return (day in w["days"] and t >= w["start"]) or (yesterday in w["days"] and t < w["end"])
    return all(inside(w) for w in pol["windows"])
