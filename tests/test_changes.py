import json
from datetime import datetime, timezone

import pytest

from orchestrator import changes, db, loop, plugins, policy
from tests.test_core import add_device, audit_kinds, scripted, set_policy, tool_call
from tests.test_policy_guard import add_user

DIGEST = "d" * 64


def change_cap(applied, outcome="verified", rollback=True, risk="medium", status="ok"):
    """A change capability whose dry run and apply are recorded instead of reaching an executor."""
    def plan(conn, devices):
        return {"digest": DIGEST, "rollback": rollback, "targets": devices,
                "hosts": {d: {"status": status,
                              "tasks": [{"task": "set", "changed": True, "diff": "+password=hunter2"}]}
                          for d in devices}}

    def apply(digest, devices):
        applied.append((digest, devices))
        if outcome == "crash":
            raise ConnectionError("executor went away")
        return {"outcome": outcome}
    caps = plugins.load()
    caps["ansible.test.set"] = {"id": "ansible.test.set", "kind": "change", "risk": risk, "description": "",
                                "params": {"type": "object", "properties": {"devices": {"type": "array"}}},
                                "handler": plan, "apply": apply}
    return caps


def propose(tenant_id, user, caps, devices=("sw1",)):
    chat = scripted(tool_call("ansible__test__set", {"devices": list(devices)}), {"content": "Planned."})
    assert loop.run(tenant_id, user, "set it", chat=chat, caps=caps)[1] == "DONE"
    return json.loads(chat.seen[1][-1]["content"])


@pytest.fixture
def acme(database):
    t = db.create_tenant("acme", "alice")
    for name, role in [("oscar", "operator"), ("ava", "approver"), ("bob", "approver"), ("vic", "viewer")]:
        add_user(t, name, role)
    add_device(t, "sw1", "colombo")
    return t


def test_a_change_is_only_a_plan_until_people_approve_and_run_it(acme):
    applied = []
    caps = change_cap(applied)
    out = propose(acme, "oscar", caps)["untrusted_tool_output"]["result"]
    change = out["change"]
    assert (out["state"], out["risk"], out["approvals_needed"]) == ("PLANNED", "medium", 1)
    assert out["plan"]["hosts"]["sw1"]["tasks"][0]["diff"] == "+[redacted]" and applied == []

    with pytest.raises(plugins.Refused, match="PLANNED, not approved"):
        changes.run(acme, "oscar", change, caps)
    with pytest.raises(PermissionError):
        changes.decide(acme, "vic", change, DIGEST, True)
    with pytest.raises(plugins.Refused, match="not the plan you were shown"):
        changes.decide(acme, "ava", change, "e" * 64, True)
    assert changes.decide(acme, "ava", change, DIGEST, True) == "APPROVED"
    with pytest.raises(PermissionError):
        changes.run(acme, "vic", change, caps)

    assert changes.run(acme, "oscar", change, caps)[0] == "VERIFIED"
    assert applied == [(DIGEST, ["sw1"])]
    with pytest.raises(plugins.Refused, match="VERIFIED"):  # one run per approval
        changes.run(acme, "oscar", change, caps)
    kinds = audit_kinds(acme)
    assert [k for k in kinds if k.startswith("change.")] == \
        ["change.planned", "change.approved", "change.executing", "change.verified"]
    with db.tenant(acme) as conn:
        assert db.verify_audit(conn) is None
        assert changes.listing(conn)[0]["decisions"] == [{"user": "ava", "decision": "approve", "reason": None}]


def test_the_requester_never_approves_and_high_risk_needs_two_people(acme):
    with db.tenant(acme) as conn:
        conn.execute("UPDATE devices SET critical = true")
    caps = change_cap([], rollback=False)
    out = propose(acme, "ava", caps)["untrusted_tool_output"]["result"]
    assert (out["risk"], out["approvals_needed"]) == ("high", 2)  # medium, +1 no rollback, +1 critical, capped
    with pytest.raises(PermissionError, match="someone else"):
        changes.decide(acme, "ava", out["change"], DIGEST, True)
    assert changes.decide(acme, "bob", out["change"], DIGEST, True) == "PLANNED"
    with pytest.raises(plugins.Refused, match="already decided"):
        changes.decide(acme, "bob", out["change"], DIGEST, True)
    assert changes.decide(acme, "alice", out["change"], DIGEST, True) == "APPROVED"

    other = propose(acme, "oscar", caps)["untrusted_tool_output"]["result"]["change"]
    assert changes.decide(acme, "bob", other, DIGEST, False, "not in this window") == "REJECTED"
    with pytest.raises(plugins.Refused, match="REJECTED"):
        changes.run(acme, "oscar", other, caps)


def test_viewers_cannot_propose_and_a_failed_dry_run_records_nothing(acme):
    chat = scripted(tool_call("ansible__test__set", {"devices": ["sw1"]}), {"content": "no"})
    loop.run(acme, "vic", "set it", chat=chat, caps=change_cap([]))
    assert "viewer role may not use" in json.loads(chat.seen[1][-1]["content"])["refused"]

    refused = propose(acme, "oscar", change_cap([], status="unreachable"))["refused"]
    assert refused == "the dry run did not succeed on sw1 (unreachable)"
    with db.tenant(acme) as conn:
        assert changes.listing(conn) == []


def test_a_closed_window_the_kill_switch_and_expiry_stop_a_run(acme):
    caps = change_cap([])
    change = propose(acme, "oscar", caps)["untrusted_tool_output"]["result"]["change"]
    changes.decide(acme, "ava", change, DIGEST, True)

    others = [d for d in policy.DAYS if d != policy.DAYS[datetime.now(timezone.utc).weekday()]]
    set_policy(acme, f"change_window: {{days: [{', '.join(others)}], start: '00:00', end: '23:59'}}")
    with pytest.raises(plugins.Refused, match="window is closed"):
        changes.run(acme, "oscar", change, caps)
    set_policy(acme, "{}")
    loop.set_kill_switch(acme, "alice", "incident")
    with pytest.raises(plugins.Refused, match="stopped: incident"):
        changes.run(acme, "oscar", change, caps)
    loop.set_kill_switch(acme, "alice", None)
    with db.tenant(acme) as conn:
        conn.execute("UPDATE changes SET expires_at = now() - interval '1 minute'")
    with pytest.raises(plugins.Refused, match="EXPIRED"):
        changes.run(acme, "oscar", change, caps)
    with db.tenant(acme) as conn:
        assert changes.listing(conn)[0]["state"] == "EXPIRED"


@pytest.mark.parametrize("outcome, state", [("rolled_back", "ROLLED_BACK"), ("superseded", "SUPERSEDED"),
                                            ("rollback_failed", "FAILED"), ("crash", "FAILED")])
def test_every_run_ends_in_a_state(acme, outcome, state):
    caps = change_cap([], outcome=outcome)
    change = propose(acme, "oscar", caps)["untrusted_tool_output"]["result"]["change"]
    changes.decide(acme, "ava", change, DIGEST, True)
    assert changes.run(acme, "oscar", change, caps)[0] == state
    assert audit_kinds(acme)[-1] == "change." + state.lower()


def test_change_windows_and_risk_rules():
    pol = policy.effective("change_window: {days: [sat], start: '22:00', end: '04:00'}")
    assert policy.window_open(pol, datetime(2026, 10, 24, 23, 0))      # Saturday night
    assert policy.window_open(pol, datetime(2026, 10, 25, 3, 59))      # into Sunday morning
    assert not policy.window_open(pol, datetime(2026, 10, 25, 4, 0))
    assert not policy.window_open(pol, datetime(2026, 10, 24, 21, 59))
    assert policy.window_open(policy.effective(), datetime(2026, 10, 21, 12, 0))  # no window set: always open
    assert policy.effective("approvals: {low: 3}")["approvals"]["low"] == 3
    assert policy.effective("approvals: {high: 1}")["approvals"]["high"] == 2  # a tenant can only ask for more
    for bad in ["approvals: {low: 0}", "change_window: {days: [someday], start: '1', end: '2'}",
                "change_window: {days: [mon], start: 22:00, end: '04:00'}"]:  # unquoted 22:00 is a YAML number
        with pytest.raises(ValueError):
            policy.parse(bad)
    cap = {"risk": "low"}
    assert [policy.risk(cap, True, False), policy.risk(cap, False, False), policy.risk(cap, False, True)] == \
        ["low", "medium", "high"]
    assert policy.risk({"risk": "destructive"}, True, False) == "destructive"
