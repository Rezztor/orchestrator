"""What people read back from the ledger: one request's steps (the trajectory) and one change's evidence for an
auditor. Both come from the tenant's own rows; nothing here writes."""
from . import changes, db

SEES_ALL = changes.APPROVER_ROLES


def ledger(conn, where, params):
    return conn.execute(f"SELECT seq, at, actor, kind, data, hash FROM audit WHERE {where} ORDER BY seq",
                        params).fetchall()


def trajectory(conn, me, request_id):
    """Every step of one request as the ledger has it. The person who asked, approvers and administrators only."""
    request = conn.execute("SELECT r.id, r.state, r.prompt, r.answer, r.tokens, r.created_at, r.updated_at, "
                           "u.name AS asked_by FROM requests r JOIN users u ON u.id = r.user_id WHERE r.id = %s",
                           (request_id,)).fetchone()
    if request is None:
        raise LookupError("no such request")
    if request["asked_by"] != me["user"] and me["role"] not in SEES_ALL:
        raise PermissionError("only the person who asked, an approver or an administrator can see these steps")
    return {**request, "steps": ledger(conn, "data->>'request' = %s", (str(request_id),))}


def evidence(conn, me, change_id):
    """One change from request to outcome, with the ledger rows that prove it and the chain's state, for an auditor."""
    change = next((c for c in changes.listing(conn, change_id)), None)
    if change is None:
        raise LookupError("no such change")
    if change["requested_by"] != me["user"] and me["role"] not in SEES_ALL:
        raise PermissionError("only the person who asked, an approver or an administrator can export this change")
    request_id = conn.execute("SELECT request_id FROM changes WHERE id = %s", (change_id,)).fetchone()["request_id"]
    head = conn.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    return {"change": change,
            "request": trajectory(conn, {"user": change["requested_by"], "role": "viewer"}, request_id),
            "ledger": ledger(conn, "data->>'change' = %s", (str(change_id),)),
            "chain": {"holds": db.verify_audit(conn) is None, "head_seq": head["seq"], "head_hash": head["hash"]},
            "exported_by": me["user"]}
