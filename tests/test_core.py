import json
import os

import psycopg
import pytest

from orchestrator import db, loop


def add_device(tenant_id, name, site):
    with db.tenant(tenant_id) as conn:
        conn.execute("INSERT INTO devices (tenant_id, name, address, platform, site) "
                     "VALUES (current_setting('app.tenant_id')::uuid, %s, '10.0.0.1', 'ios', %s)", (name, site))


def set_policy(tenant_id, text):
    with db.tenant(tenant_id) as conn:
        conn.execute("INSERT INTO policies (tenant_id, document, set_by) VALUES "
                     "(current_setting('app.tenant_id')::uuid, %s, 'test') ON CONFLICT (tenant_id) "
                     "DO UPDATE SET document = EXCLUDED.document", (text,))


def tool_call(name, args, call_id="c1"):
    return {"content": None, "tool_calls": [{"id": call_id, "type": "function",
                                             "function": {"name": name, "arguments": json.dumps(args)}}]}


def scripted(*replies):
    """A model that returns the given replies in order and records what it was sent."""
    seen = []

    def chat(messages, tools):
        seen.append([dict(m) for m in messages])
        return replies[len(seen) - 1]
    chat.seen = seen
    return chat


def audit_kinds(tenant_id):
    with db.tenant(tenant_id) as conn:
        return [r["kind"] for r in conn.execute("SELECT kind FROM audit ORDER BY seq")]


def test_a_tenant_sees_and_writes_only_its_own_rows(database):
    a = db.create_tenant("acme", "alice")
    b = db.create_tenant("globex", "bob")
    add_device(a, "acme-sw1", "colombo")
    add_device(b, "globex-sw1", "dubai")

    with db.tenant(a) as conn:
        assert [r["name"] for r in conn.execute("SELECT name FROM devices")] == ["acme-sw1"]
        assert [r["name"] for r in conn.execute("SELECT name FROM tenants")] == ["acme"]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO devices (tenant_id, name, address, platform) VALUES (%s, 'x', 'y', 'z')", (b,))

    with psycopg.connect(os.environ["ORCH_DB_APP_URL"]) as conn:  # no tenant set: nothing visible
        assert conn.execute("SELECT count(*) FROM devices").fetchone()[0] == 0

    with pytest.raises(PermissionError):
        loop.run(a, "bob", "list devices", chat=scripted())


def test_the_audit_chain_detects_an_edit_and_the_app_cannot_edit(database):
    t = db.create_tenant("acme", "alice")
    with db.tenant(t) as conn:
        for i in range(3):
            db.audit(conn, "alice", "test", {"i": i, "text": "naïve ✓"})
    with db.tenant(t) as conn:
        assert db.verify_audit(conn) is None
        seq = conn.execute("SELECT seq FROM audit ORDER BY seq OFFSET 1 LIMIT 1").fetchone()["seq"]
    with pytest.raises(psycopg.Error):
        with db.tenant(t) as conn:
            conn.execute("UPDATE audit SET data = '{}' WHERE seq = %s", (seq,))

    with psycopg.connect(database, autocommit=True) as conn:  # only the owner, with the guard off, can edit
        conn.execute("ALTER TABLE audit DISABLE TRIGGER audit_append_only")
        conn.execute("UPDATE audit SET data = '{\"i\": 99}' WHERE seq = %s", (seq,))
        conn.execute("ALTER TABLE audit ENABLE TRIGGER audit_append_only")
    with db.tenant(t) as conn:
        assert db.verify_audit(conn) == seq


def test_a_read_request_runs_to_done_and_tool_output_is_marked_untrusted(database):
    t = db.create_tenant("acme", "alice")
    add_device(t, "core-sw1", "colombo")
    add_device(t, "edge-rt1", "kandy")
    chat = scripted(tool_call("inventory__devices__list", {"site": "colombo"}),
                    {"content": "One device in Colombo: core-sw1."})

    request_id, state, answer = loop.run(t, "alice", "What is in Colombo?", chat=chat)

    assert (state, answer) == ("DONE", "One device in Colombo: core-sw1.")
    tool_msg = json.loads(chat.seen[1][-1]["content"])["untrusted_tool_output"]
    assert [d["name"] for d in tool_msg["result"]] == ["core-sw1"]
    assert audit_kinds(t) == ["request.received", "request.state", "tool.read", "request.state"]
    with db.tenant(t) as conn:
        assert conn.execute("SELECT state FROM requests WHERE id = %s", (request_id,)).fetchone()["state"] == "DONE"
        assert db.verify_audit(conn) is None


def test_unknown_tools_and_bad_arguments_are_refused(database):
    t = db.create_tenant("acme", "alice")
    chat = scripted(tool_call("shell__run", {}), tool_call("inventory__devices__list", {"site": 5}),
                    tool_call("inventory__devices__list", {"where": "1=1"}), {"content": "Nothing found."})

    _, state, _ = loop.run(t, "alice", "reload every switch", chat=chat)

    assert state == "DONE"
    refusals = [json.loads(m[-1]["content"])["refused"] for m in chat.seen[1:]]
    assert "no capability" in refusals[0] and "must be string" in refusals[1] and "unknown argument" in refusals[2]
    assert audit_kinds(t).count("tool.refused") == 3


def test_a_request_always_ends_in_a_state(database):
    t = db.create_tenant("acme", "alice")

    def broken(messages, tools):
        raise TimeoutError("model did not answer")
    assert loop.run(t, "alice", "hi", chat=broken)[1:] == ("FAILED", "TimeoutError: model did not answer")

    set_policy(t, "limits: {max_steps: 3}")
    looping = scripted(*[tool_call("inventory__devices__list", {})] * 3)
    assert loop.run(t, "alice", "hi", chat=looping)[1:] == ("FAILED", "no answer within 3 steps")
