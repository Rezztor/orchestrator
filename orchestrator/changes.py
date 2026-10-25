"""The change path: a dry-run plan with a digest, people's approval of that digest, one run, verify, rollback.

The model can only propose: a change capability's handler is a dry run, and its result becomes a PLANNED row. People
approve the digest they were shown (never the person who asked; the risk class sets how many). The run sends that
digest to the executor, which plans again and refuses on any difference, then applies, verifies and backs out.

PLANNED -> APPROVED -> EXECUTING -> VERIFIED | ROLLED_BACK | SUPERSEDED | FAILED, or PLANNED -> REJECTED.
A PLANNED or APPROVED row past expires_at is shown as EXPIRED and can no longer be approved or run.
"""
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from . import db, guard, policy
from .plugins import Refused

APPROVER_ROLES = ("approver", "tenant-admin")
OUTCOMES = {"verified": "VERIFIED", "rolled_back": "ROLLED_BACK", "superseded": "SUPERSEDED"}  # anything else FAILED
# shortcut: a failed change is audited and shown on the page; nobody is paged until a notification channel exists.
# shortcut: a core restart during a run leaves the row EXECUTING; mark it FAILED by hand after checking the devices.


def user_row(conn, name):
    user = conn.execute("SELECT id, name, role FROM users WHERE name = %s", (name,)).fetchone()
    if user is None:
        raise PermissionError(f"no user {name!r} in this tenant")
    return user


def shown_state(change):
    return "EXPIRED" if change["expired"] and change["state"] in ("PLANNED", "APPROVED") else change["state"]


def propose(conn, request_id, user, pol, cap, args):
    """Run the capability's dry run and record it as a PLANNED change. Returns what the model is told."""
    plan = cap["handler"](conn, **args)
    bad = {h: v["status"] for h, v in plan["hosts"].items() if v["status"] != "ok"}
    if bad:
        raise Refused("the dry run did not succeed on " + ", ".join(f"{h} ({s})" for h, s in sorted(bad.items())))
    critical = conn.execute("SELECT count(*) AS n FROM devices WHERE critical AND name = ANY(%s::text[])",
                            (plan["targets"],)).fetchone()["n"]
    risk = policy.risk(cap, plan["rollback"], critical)
    shown = {"rollback": plan["rollback"], "hosts": guard.redact(plan["hosts"])}
    change_id = conn.execute(
        "INSERT INTO changes (tenant_id, request_id, requested_by, capability, args, plan, digest, risk, "
        "approvals_needed, expires_at) VALUES (current_setting('app.tenant_id')::uuid, %s, %s, %s, %s, %s, %s, %s, "
        "%s, now() + make_interval(mins => %s)) RETURNING id",
        (request_id, user["id"], cap["id"], Jsonb(args), Jsonb(shown), plan["digest"], risk, pol["approvals"][risk],
         pol["limits"]["change_ttl_minutes"])).fetchone()["id"]
    db.audit(conn, "orchestrator", "change.planned", {
        "change": str(change_id), "request": str(request_id), "requested_by": user["name"], "capability": cap["id"],
        "args": guard.redact(args), "digest": plan["digest"], "risk": risk, "approvals_needed": pol["approvals"][risk]})
    return {"change": str(change_id), "state": "PLANNED", "risk": risk, "approvals_needed": pol["approvals"][risk],
            "plan": shown, "note": "Nothing has changed. People must approve this plan and run it."}


def decide(tenant_id, user_name, change_id, digest, approve, reason=None):
    """Record one person's approval or rejection of the digest they were shown. Returns the change's state."""
    with db.tenant(tenant_id) as conn:
        user = user_row(conn, user_name)
        if user["role"] not in APPROVER_ROLES:
            raise PermissionError("only an approver or a tenant administrator can decide on a change")
        change = conn.execute("SELECT *, expires_at <= now() AS expired FROM changes WHERE id = %s FOR UPDATE",
                              (change_id,)).fetchone()
        if change is None:
            raise LookupError("no such change")
        if change["requested_by"] == user["id"]:
            raise PermissionError("you asked for this change; someone else must approve it")
        if change["state"] != "PLANNED" or change["expired"]:
            raise Refused(f"this change is {shown_state(change)}, not waiting for a decision")
        if digest != change["digest"]:
            raise Refused("this is not the plan you were shown; reload it")
        added = conn.execute(
            "INSERT INTO approvals (change_id, tenant_id, user_id, digest, decision, reason) "
            "VALUES (%s, current_setting('app.tenant_id')::uuid, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (change_id, user["id"], digest, "approve" if approve else "reject", reason)).rowcount
        if not added:
            raise Refused("you have already decided on this change")
        approvals = conn.execute("SELECT count(*) AS n FROM approvals WHERE change_id = %s AND decision = 'approve'",
                                 (change_id,)).fetchone()["n"]
        state = "REJECTED" if not approve else "APPROVED" if approvals >= change["approvals_needed"] else "PLANNED"
        conn.execute("UPDATE changes SET state = %s, updated_at = now() WHERE id = %s", (state, change_id))
        db.audit(conn, user_name, "change.approved" if approve else "change.rejected",
                 {"change": str(change_id), "digest": digest, "reason": reason, "state": state})
        return state


def start(tenant_id, user_name, change_id):
    """Claim an approved change for its one run (the requester or an approver). Returns the row to pass to finish."""
    with db.tenant(tenant_id) as conn:
        user = user_row(conn, user_name)
        change = conn.execute("SELECT *, expires_at <= now() AS expired FROM changes WHERE id = %s FOR UPDATE",
                              (change_id,)).fetchone()
        if change is None:
            raise LookupError("no such change")
        if change["requested_by"] != user["id"] and user["role"] not in APPROVER_ROLES:
            raise PermissionError("only the person who asked or an approver can run a change")
        if change["state"] != "APPROVED" or change["expired"]:
            raise Refused(f"this change is {shown_state(change)}, not approved to run")
        pol = policy.for_tenant(conn)
        if kill := policy.stopped(conn):
            raise Refused(f"agent work is stopped: {kill}")
        if not policy.window_open(pol, datetime.now(timezone.utc)):
            raise Refused("the change window is closed")
        conn.execute("UPDATE changes SET state = 'EXECUTING', updated_at = now() WHERE id = %s", (change_id,))
        db.audit(conn, user_name, "change.executing", {"change": str(change_id), "digest": change["digest"]})
        return change


def finish(tenant_id, change, caps):
    """Send the approved digest to the capability's apply handler and record the outcome. Returns (state, result)."""
    try:
        result = caps[change["capability"]]["apply"](change["digest"], **change["args"])
    except Exception as exc:  # the change must end in a state, whatever failed
        result = {"outcome": "error", "error": f"{type(exc).__name__}: {exc}"}
    state, result = OUTCOMES.get(result.get("outcome"), "FAILED"), guard.redact(result)
    with db.tenant(tenant_id) as conn:
        conn.execute("UPDATE changes SET state = %s, result = %s, updated_at = now() WHERE id = %s",
                     (state, Jsonb(result), change["id"]))
        db.audit(conn, "executor", "change." + state.lower(), {
            "change": str(change["id"]), "outcome": result.get("outcome"), "result_digest": db.digest(result)})
    return state, result


def run(tenant_id, user_name, change_id, caps):
    return finish(tenant_id, start(tenant_id, user_name, change_id), caps)


def listing(conn):
    """The tenant's 50 latest changes, as the page shows them."""
    rows = conn.execute(
        "SELECT c.id, c.capability, c.args, c.plan, c.digest, c.risk, c.approvals_needed, c.result, c.created_at, "
        "c.expires_at, u.name AS requested_by, CASE WHEN c.state IN ('PLANNED', 'APPROVED') AND c.expires_at <= now() "
        "THEN 'EXPIRED' ELSE c.state END AS state, coalesce((SELECT json_agg(json_build_object('user', d.name, "
        "'decision', a.decision, 'reason', a.reason) ORDER BY a.at) FROM approvals a JOIN users d ON d.id = a.user_id "
        "WHERE a.change_id = c.id), '[]') AS decisions "
        "FROM changes c JOIN users u ON u.id = c.requested_by ORDER BY c.created_at DESC LIMIT 50").fetchall()
    return [{**r, "args": guard.redact(r["args"])} for r in rows]
