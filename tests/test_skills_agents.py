import json

import pytest

from orchestrator import changes, db, loop, skills
from tests.test_changes import DIGEST, change_cap
from tests.test_core import add_device, audit_kinds, scripted, tool_call
from tests.test_policy_guard import add_user


@pytest.fixture
def acme(database):
    t = db.create_tenant("acme", "alice")
    add_user(t, "oscar", "operator")
    add_user(t, "vic", "viewer")
    add_device(t, "sw1", "colombo")
    return t


def result(chat, step):
    return json.loads(chat.seen[step][-1]["content"])


def test_skills_are_offered_by_role_and_read_on_demand(acme):
    chat = scripted(tool_call("skills__read", {"name": "login-banner"}), {"content": "ok"})
    loop.run(acme, "oscar", "set the banner", chat=chat)
    system = chat.seen[0][0]["content"]
    assert "- device-health-check:" in system and "- login-banner:" in system
    assert "# Login banner" in result(chat, 1)["untrusted_tool_output"]["result"]["procedure"]

    chat = scripted({"content": "ok"})
    loop.run(acme, "vic", "hello", chat=chat)  # a viewer cannot use ansible, so neither skill is worth offering
    assert "Skills" not in chat.seen[0][0]["content"]


def test_a_skill_the_agent_proposes_is_inert_until_an_admin_approves_it(acme):
    proposal = {"name": "vlan-audit", "description": "Audit VLANs at a site.", "body": "1. List devices.\n"
                "2. Read facts. password=hunter2", "capabilities": ["inventory.devices.list"]}
    chat = scripted(tool_call("skills__propose", proposal), tool_call("skills__read", {"name": "vlan-audit"}),
                    tool_call("skills__propose", {**proposal, "name": "login-banner"}), {"content": "done"})
    loop.run(acme, "oscar", "audit vlans and save how", chat=chat)
    assert result(chat, 1)["untrusted_tool_output"]["result"]["state"] == "proposed"
    assert result(chat, 2)["refused"] == "no skill 'vlan-audit'"  # inert
    assert "already exists" in result(chat, 3)["refused"]  # a shipped name cannot be taken over

    with db.tenant(acme) as conn:
        stored = skills.listing(conn)[0]
        assert (stored["state"], stored["source"], stored["proposed_by"]) == ("proposed", "induced", "agent")
        assert "hunter2" not in stored["body"]
        with pytest.raises(PermissionError):
            skills.decide(conn, {"name": "oscar", "role": "operator"}, "vlan-audit", True)
        skills.decide(conn, {"name": "alice", "role": "tenant-admin"}, "vlan-audit", True)
        assert skills.index(conn, loop.policy.for_tenant(conn), "operator")["vlan-audit"] == "Audit VLANs at a site."
        skills.decide(conn, {"name": "alice", "role": "tenant-admin"}, "vlan-audit", False)
        assert "vlan-audit" not in skills.usable(conn)
        with pytest.raises(LookupError):
            skills.decide(conn, {"name": "alice", "role": "tenant-admin"}, "vlan-audit", True)  # retired stays retired
    other = db.create_tenant("globex", "gina")
    with db.tenant(other) as conn:
        assert skills.listing(conn) == []
    assert "skill.proposed" in audit_kinds(acme) and "skill.approved" in audit_kinds(acme)


def test_shipped_skills_parse_and_bad_ones_are_refused():
    assert set(skills.shipped()) == {"device-health-check", "login-banner"}
    for bad in ["no front matter", "---\nname: Bad Name\ndescription: x\n---\nbody", "---\nname: ok\n---\nbody",
                "---\nname: ok\ndescription: x\nowner: me\n---\nbody"]:
        with pytest.raises(ValueError):
            skills.parse(bad)


def test_a_specialist_gets_narrowed_tools_and_shares_the_budget(acme):
    chat = scripted(tool_call("agents__delegate", {"agent": "network", "task": "Which devices are in Colombo?"}),
                    tool_call("inventory__devices__list", {"site": "colombo"}),  # the specialist's turn
                    tool_call("agents__delegate", {"agent": "network", "task": "again"}),  # not a tool it has
                    {"content": "sw1 is in Colombo."},  # the specialist's findings
                    {"content": "Colombo has sw1."})
    seen_tools = []
    inner = chat

    def chat(messages, tools):  # noqa: F811
        seen_tools.append(sorted(t["function"]["name"] for t in tools))
        return inner(messages, tools)
    request_id, state, answer = loop.run(acme, "oscar", "What is in Colombo?", chat=chat)

    assert (state, answer) == ("DONE", "Colombo has sw1.")
    assert "agents__delegate" in seen_tools[0] and "ansible__playbook__run" in seen_tools[0]
    assert seen_tools[1] == ["ansible__facts__read", "ansible__playbooks__list", "inventory__devices__list",
                             "skills__read", "solarwinds__nodes__status"]
    assert "no capability 'agents__delegate'" in json.loads(inner.seen[3][-1]["content"])["refused"]
    findings = json.loads(inner.seen[4][-1]["content"])["untrusted_tool_output"]["result"]
    assert findings == {"agent": "network", "findings": "sw1 is in Colombo."}
    with db.tenant(acme) as conn:
        actors = [r["actor"] for r in conn.execute("SELECT actor FROM audit WHERE kind = 'tool.read' ORDER BY seq")]
        assert actors == ["network", "orchestrator"]
        assert conn.execute("SELECT state FROM requests WHERE id = %s", (request_id,)).fetchone()["state"] == "DONE"

    # the specialist's steps count against the request's max_steps
    looping = scripted(tool_call("agents__delegate", {"agent": "network", "task": "loop"}),
                       *[tool_call("inventory__devices__list", {})] * 10)
    assert loop.run(acme, "oscar", "loop", chat=looping)[1:] == ("FAILED", "no answer within 8 steps")


def test_a_viewer_delegating_gets_no_more_than_a_viewer(acme):
    tools = []
    replies = iter([tool_call("agents__delegate", {"agent": "network", "task": "facts for sw1"}),
                    {"content": "I can only list devices."}, {"content": "done"}])

    def chat(messages, offered):
        tools.append(sorted(t["function"]["name"] for t in offered))
        return next(replies)
    loop.run(acme, "vic", "facts for sw1", chat=chat)
    assert tools[1] == ["inventory__devices__list", "skills__read", "solarwinds__nodes__status"]


def test_the_reviewer_notes_every_plan_and_its_absence_does_not_block(acme):
    caps = change_cap([])
    reviewer = scripted({"content": "Scope matches the request. No back-out concerns."})
    chat = scripted(tool_call("ansible__test__set", {"devices": ["sw1"]}), {"content": "Planned."})
    loop.run(acme, "oscar", "set sw1", chat=chat, caps=caps, reviewer=reviewer)
    planned = result(chat, 1)["untrusted_tool_output"]["result"]
    assert planned["review"] == "Scope matches the request. No back-out concerns."
    sent = json.loads(reviewer.seen[0][1]["content"])
    assert sent["request"] == "set sw1" and sent["plan"]["hosts"]["sw1"]["tasks"][0]["diff"] == "+[redacted]"

    def down(messages, tools):
        raise ConnectionError("no route")
    chat = scripted(tool_call("ansible__test__set", {"devices": ["sw1"]}), {"content": "Planned."})
    loop.run(acme, "oscar", "set sw1", chat=chat, caps=caps, reviewer=down)
    with db.tenant(acme) as conn:
        latest = changes.listing(conn)[0]
    assert latest["review"].startswith("The reviewer could not be reached") and latest["digest"] == DIGEST
