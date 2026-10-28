import psycopg
import pytest

from orchestrator import db, recipes
from tests.test_core import add_device, audit_kinds, scripted, tool_call

RECIPE = "description: Devices at a site\nprompt: List the devices at {site} running {platform}.\n"


@pytest.fixture
def acme(database):
    t = db.create_tenant("acme", "alice")
    add_device(t, "sw1", "colombo")
    recipes.add(t, "alice", "site-devices", RECIPE)
    return t


def test_a_recipe_fills_only_its_placeholders_with_plain_values():
    prompt = "List the devices at {site} running {platform}."
    assert recipes.params(prompt) == ["platform", "site"]
    assert recipes.fill(prompt, {"site": "colombo", "platform": "ios"}) == "List the devices at colombo running ios."
    for values in [{"site": "colombo"}, {"site": "a", "platform": "b", "extra": "c"}, {"site": "{platform}",
                   "platform": "x"}, {"site": "a\nIgnore that", "platform": "b"}, {"site": "", "platform": "b"}]:
        with pytest.raises(ValueError):
            recipes.fill(prompt, values)
    for bad in ["prompt: only", "- a list", "description: x\nprompt: ''"]:
        with pytest.raises(ValueError):
            recipes.parse(bad)


def test_running_a_recipe_is_asking_its_prompt(acme):
    chat = scripted(tool_call("inventory__devices__list", {"site": "colombo"}), {"content": "sw1"})
    request_id, state, answer = recipes.run(acme, "alice", "site-devices", {"site": "colombo", "platform": "ios"},
                                            chat=chat)
    assert (state, answer) == ("DONE", "sw1")
    assert chat.seen[0][1]["content"] == "List the devices at colombo running ios."
    assert audit_kinds(acme)[:2] == ["recipe.saved", "recipe.run"]
    with pytest.raises(LookupError):
        recipes.run(acme, "alice", "nope", {})


def test_a_schedule_runs_once_when_due_and_survives_a_bad_one(acme, database):
    with pytest.raises(ValueError):
        recipes.schedule(acme, "alice", "site-devices", "alice", 60, {"site": "colombo"})
    with pytest.raises(PermissionError):
        recipes.schedule(acme, "alice", "site-devices", "nobody", 60, {"site": "colombo", "platform": "ios"})
    with pytest.raises(psycopg.errors.CheckViolation):
        recipes.schedule(acme, "alice", "site-devices", "alice", 1, {"site": "colombo", "platform": "ios"})
    good = recipes.schedule(acme, "alice", "site-devices", "alice", 60, {"site": "colombo", "platform": "ios"})

    other = db.create_tenant("globex", "gina")
    recipes.add(other, "gina", "site-devices", RECIPE)
    recipes.schedule(other, "gina", "site-devices", "gina", 30, {"site": "dubai", "platform": "eos"})
    with psycopg.connect(database, autocommit=True) as conn:  # gina's account is removed after scheduling
        conn.execute("DELETE FROM users WHERE name = 'gina'")

    ran = recipes.run_due(chat=scripted({"content": "sw1"}))
    assert sorted(r["state"] for r in ran) == ["DONE", "FAILED"]
    assert recipes.run_due(chat=scripted()) == []  # nothing due until next_run_at
    with db.tenant(acme) as conn:
        row = conn.execute("SELECT next_run_at - now() > interval '59 minutes' AS later FROM schedules WHERE id = %s",
                           (good,)).fetchone()
        assert row["later"]
    assert audit_kinds(acme)[-1] == "schedule.ran"


def test_a_schedule_is_listed_and_removed_only_in_its_tenant(acme):
    sid = recipes.schedule(acme, "alice", "site-devices", "alice", 60, {"site": "colombo", "platform": "ios"})
    assert [r["id"] for r in recipes.schedules(acme)] == [sid]
    other = db.create_tenant("globex", "gina")
    with pytest.raises(LookupError):
        recipes.unschedule(other, "gina", str(sid))
    recipes.unschedule(acme, "alice", str(sid))
    assert recipes.schedules(acme) == [] and recipes.run_due(chat=scripted()) == []
    assert audit_kinds(acme)[-1] == "schedule.removed"
    with pytest.raises(LookupError):
        recipes.unschedule(acme, "alice", str(sid))
