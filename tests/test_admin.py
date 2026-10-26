"""The screens' data and a tenant administrator's changes from the page."""
import subprocess
import sys

from orchestrator import db, model, recipes
from orchestrator.__main__ import main
from tests.test_core import scripted, tool_call
from tests.test_web import call, sign_in, site  # noqa: F401  (site is a fixture)


def test_an_admin_manages_people_and_others_cannot(site):
    _, alice, _ = sign_in(site)
    assert call(site, "/api/people/add", {"name": "vince", "role": "viewer", "password": "viewer password 1"},
                cookie=alice)[0] == 200
    assert call(site, "/api/people/add", {"name": "vince", "role": "viewer"}, cookie=alice)[0] == 400
    assert call(site, "/api/people/add", {"name": "Bad Name", "role": "viewer"}, cookie=alice)[0] == 400
    people = call(site, "/api/people", cookie=alice)[1]["people"]
    assert {p["name"]: (p["role"], p["password"]) for p in people} == {"alice": ("tenant-admin", True),
                                                                      "vince": ("viewer", True)}

    _, vince, _ = sign_in(site, "viewer password 1", "vince")
    assert call(site, "/api/people", cookie=vince)[0] == 403
    assert call(site, "/api/people/add", {"name": "eve", "role": "tenant-admin"}, cookie=vince)[0] == 403
    assert call(site, "/api/audit", cookie=vince)[0] == 403

    # a new approver signs in again, so the authenticator step cannot be skipped on an old session
    assert call(site, "/api/people/role", {"name": "vince", "role": "approver"}, cookie=alice)[0] == 200
    assert call(site, "/api/me", cookie=vince)[0] == 401
    assert call(site, "/api/people/role", {"name": "alice", "role": "viewer"}, cookie=alice)[0] == 403
    assert call(site, "/api/people/mfa-reset", {"name": "nobody"}, cookie=alice)[0] == 404


def test_devices_policy_and_connections_from_the_page(site, monkeypatch):
    _, alice, _ = sign_in(site)
    assert call(site, "/api/devices/add", {"name": "edge-r1", "address": "10.0.0.2", "platform": "ios"},
                cookie=alice)[0] == 200
    assert call(site, "/api/devices/critical", {"name": "edge-r1", "critical": True}, cookie=alice)[0] == 200
    devices = {d["name"]: d["critical"] for d in call(site, "/api/devices", cookie=alice)[1]["devices"]}
    assert devices == {"core-sw1": False, "edge-r1": True}

    assert call(site, "/api/policy/set", {"document": "approvals: {high: 0}"}, cookie=alice)[0] == 400
    assert call(site, "/api/policy/set", {"document": "approvals: [high"}, cookie=alice)[0] == 400
    assert call(site, "/api/policy/set", {"document": "approvals: {high: 3}"}, cookie=alice)[0] == 200
    assert call(site, "/api/policy", cookie=alice)[1]["effective"]["approvals"]["high"] == 3

    monkeypatch.setenv("ORCH_PUBLIC_URL", "https://orch.example")
    assert call(site, "/api/webhooks/add", {"kind": "teams", "url": "http://insecure"}, cookie=alice)[0] == 400
    assert call(site, "/api/webhooks/add", {"kind": "teams", "url": "https://hooks.example/abc/SECRET"},
                cookie=alice)[0] == 200
    recipes.add(_tenant(), "cli", "health", "description: d\nprompt: check {site}")
    status, body, _ = call(site, "/api/triggers/add", {"recipe": "health", "run_as": "alice"}, cookie=alice)
    assert status == 200 and body["address"].startswith("https://orch.example/hooks/")

    seen = call(site, "/api/connections", cookie=alice)[1]
    assert seen["webhooks"][0]["host"] == "hooks.example" and "SECRET" not in str(seen)
    trigger = seen["triggers"][0]["id"]
    assert len(trigger) == 12 and call(site, "/api/triggers/remove", {"id": trigger}, cookie=alice)[0] == 200
    assert call(site, body["address"].removeprefix("https://orch.example"), {"site": "x"})[0] == 404
    assert call(site, "/api/webhooks/remove", {"id": seen["webhooks"][0]["id"]}, cookie=alice)[0] == 200


def test_history_overview_and_the_ledger(site, monkeypatch):
    _, alice, _ = sign_in(site)
    monkeypatch.setattr(model, "chat", scripted(tool_call("inventory__devices__list", {}), {"content": "core-sw1"}))
    request = call(site, "/api/ask", {"prompt": "list devices"}, cookie=alice)[1]["request"]

    assert [r["id"] for r in call(site, "/api/requests", cookie=alice)[1]["requests"]] == [request]
    overview = call(site, "/api/overview", cookie=alice)[1]
    assert overview["requests_24h"] == {"DONE": 1} and len(overview["per_hour"]) == 24
    assert sum(overview["per_hour"]) == 1 and overview["waiting"] == 0

    page = call(site, f"/api/audit?ref={request}", cookie=alice)[1]
    assert page["chain"]["holds"] and {r["kind"] for r in page["rows"]} >= {"request.received", "tool.read"}
    newest = call(site, "/api/audit?kind=auth.", cookie=alice)[1]["rows"]
    assert newest and all(r["kind"].startswith("auth.") for r in newest)
    assert call(site, f"/api/audit?before={newest[-1]['seq']}&kind=auth.", cookie=alice)[1]["rows"] == [
        r for r in newest if r["seq"] < newest[-1]["seq"]]


def test_policy_set_from_the_command_line(site, tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("approvals: {high: 4}\n")
    main(["policy-set", str(_tenant()), str(path)])
    with db.tenant(_tenant()) as conn:
        assert conn.execute("SELECT set_by FROM policies").fetchone()["set_by"] == "cli"
    path.write_text("approvals: [high\n")
    done = subprocess.run([sys.executable, "-m", "orchestrator", "policy-set", str(_tenant()), str(path)],
                          capture_output=True, text=True)
    assert done.returncode == 1 and done.stderr.startswith("error: the policy is not valid YAML")


def _tenant():
    with db.app() as conn:
        return conn.execute("SELECT tenant_id_by_name('acme') AS id").fetchone()["id"]
