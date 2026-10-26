import base64
import json
import shutil
import socket
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from executors import tofu_executor as tx
from orchestrator import changes, db, loop, notify, plugins, siem, tickets
from tests.test_changes import DIGEST, change_cap
from tests.test_core import add_device, audit_kinds, scripted, set_policy, tool_call
from tests.test_policy_guard import add_user

TOKEN = "t" * 40


@pytest.fixture
def fake():
    """An HTTP server that records each request and answers with fake.answer(method, path, body)."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def handle_any(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            seen.append({"method": self.command, "path": self.path, "body": body, "headers": dict(self.headers)})
            code, answer = server.answer(self.command, self.path, body)
            data = json.dumps(answer).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = do_PATCH = handle_any

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.seen, server.answer = seen, lambda method, path, body: (200, {})
    server.url = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def acme(database):
    t = db.create_tenant("acme", "alice")
    add_user(t, "oscar", "operator")
    add_user(t, "ava", "approver")
    add_user(t, "bob", "approver")
    add_user(t, "vic", "viewer")
    add_device(t, "sw1", "colombo")
    return t


def approved_change(tenant_id, caps):
    chat = scripted(tool_call("ansible__test__set", {"devices": ["sw1"]}), {"content": "Planned."})
    loop.run(tenant_id, "oscar", "set it", chat=chat, caps=caps)
    change = json.loads(chat.seen[1][-1]["content"])["untrusted_tool_output"]["result"]["change"]
    changes.decide(tenant_id, "ava", change, DIGEST, True)
    return change


# --- OpenTofu cloud executor -------------------------------------------------------------------------------------

MODULE = '''variable "note" { type = string }
variable "secret" {
  type      = string
  sensitive = true
  default   = "s3cr3t-value"
}
resource "terraform_data" "note" {
  input = { note = var.note, secret = var.secret }
}
'''


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    if not shutil.which("tofu"):
        pytest.skip("OpenTofu is not installed")
    (tmp_path / "modules" / "note").mkdir(parents=True)
    (tmp_path / "modules" / "note" / "main.tf").write_text(MODULE)
    (tmp_path / "modules" / "note" / "README.md").write_text("# Keep a note\n")
    monkeypatch.setattr(tx.Handler, "token", TOKEN)
    monkeypatch.setattr(tx.Handler, "modules", str(tmp_path / "modules"))
    monkeypatch.setattr(tx.Handler, "state", str(tmp_path / "state"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), tx.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ORCH_TOFU_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("ORCH_TOFU_TOKEN", TOKEN)
    yield tmp_path
    server.shutdown()


def test_a_cloud_module_applies_only_the_approved_plan(cloud):
    job = {"module": "note", "workspace": "dev", "vars": {"note": "hello"}, "phase": "plan"}
    plan = tx.module_job(job, cloud / "modules", cloud / "state")
    assert plan["rollback"] is False and plan["changes"][0]["actions"] == ["create"]
    assert plan["changes"][0]["after"]["input"]["secret"] == "(sensitive)"
    assert tx.module_job({**job, "vars": {"note": "other"}, "phase": "apply", "digest": plan["digest"]},
                         cloud / "modules", cloud / "state")["outcome"] == "superseded"
    run = tx.module_job({**job, "phase": "apply", "digest": plan["digest"]}, cloud / "modules", cloud / "state")
    assert (run["outcome"], run["verify"]) == ("verified", "nothing left to change")
    assert tx.module_job(job, cloud / "modules", cloud / "state")["changes"] == []  # applied: nothing left to do
    for bad in [{**job, "module": "../note"}, {**job, "workspace": "Prod!"}, {**job, "vars": {"note": ["x"]}}]:
        with pytest.raises(ValueError):
            tx.module_job(bad, cloud / "modules", cloud / "state")


def test_the_agent_proposes_a_cloud_change_that_needs_two_people(acme, cloud):
    chat = scripted(tool_call("cloud__modules__list", {}),
                    tool_call("cloud__module__apply", {"module": "note", "workspace": "dev", "vars": {"note": "hi"}}),
                    {"content": "Planned."})
    loop.run(acme, "oscar", "keep a note in dev", chat=chat)
    assert json.loads(chat.seen[1][-1]["content"])["untrusted_tool_output"]["result"] == \
        [{"name": "note", "description": "Keep a note"}]
    planned = json.loads(chat.seen[2][-1]["content"])["untrusted_tool_output"]["result"]
    assert (planned["risk"], planned["approvals_needed"]) == ("high", 2)  # no rollback in the cloud
    task = planned["plan"]["hosts"]["note/dev"]["tasks"][0]
    assert task["task"] == "terraform_data.note (create)" and "s3cr3t" not in task["diff"]
    with db.tenant(acme) as conn:
        digest = changes.listing(conn)[0]["digest"]
    changes.decide(acme, "ava", planned["change"], digest, True)
    assert changes.decide(acme, "bob", planned["change"], digest, True) == "APPROVED"
    assert changes.run(acme, "oscar", planned["change"], plugins.load())[0] == "VERIFIED"


# --- SolarWinds ------------------------------------------------------------------------------------------------------

def test_a_connection_that_is_not_configured_is_refused_not_a_crash(acme, monkeypatch):
    monkeypatch.delenv("ORCH_SOLARWINDS_URL", raising=False)
    chat = scripted(tool_call("solarwinds__nodes__status", {"devices": ["sw1"]}), {"content": "not connected"})
    assert loop.run(acme, "oscar", "is sw1 up?", chat=chat)[1] == "DONE"
    assert "ORCH_SOLARWINDS_URL is not set" in json.loads(chat.seen[1][-1]["content"])["refused"]


def test_solarwinds_status_uses_parameters_and_only_the_tenants_devices(acme, fake, monkeypatch):
    monkeypatch.setenv("ORCH_SOLARWINDS_URL", fake.url)
    monkeypatch.setenv("ORCH_SOLARWINDS_USER", "orch-read")
    monkeypatch.setenv("ORCH_SOLARWINDS_PASSWORD", "pw")
    node = {"Caption": "sw1", "Status": 1, "StatusDescription": "Node is Up."}
    fake.answer = lambda m, p, b: (200, {"results": [node]})
    add_device(acme, "sw2", "colombo")
    chat = scripted(tool_call("solarwinds__nodes__status", {"devices": ["sw1", "sw2"]}),
                    tool_call("solarwinds__nodes__status", {"devices": ["sw1' OR 1=1 --"]}), {"content": "ok"})
    loop.run(acme, "oscar", "is sw1 up?", chat=chat)
    result = json.loads(chat.seen[1][-1]["content"])["untrusted_tool_output"]["result"]
    assert result == {"sw1": node, "sw2": {"status": "not monitored in SolarWinds"}}
    sent = fake.seen[0]
    assert sent["path"] == "/SolarWinds/InformationService/v3/Json/Query"
    assert sent["body"]["parameters"] == {"n0": "sw1", "n1": "sw2"} and "sw1" not in sent["body"]["query"]
    assert base64.b64decode(sent["headers"]["Authorization"].split()[1]) == b"orch-read:pw"
    assert "not in this tenant's inventory" in json.loads(chat.seen[2][-1]["content"])["refused"]
    assert len(fake.seen) == 1


# --- ServiceNow change records -------------------------------------------------------------------------------------

def snow_record(**fields):
    now = datetime.now(timezone.utc)
    return {"sys_id": "abc123", "number": "CHG0012345", "state": "-1", "approval": "approved",
            "start_date": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"), **fields}


def test_a_risk_class_can_require_an_approved_change_record(acme, fake, monkeypatch):
    set_policy(acme, "change_record: [medium]")
    caps = change_cap([])
    change = approved_change(acme, caps)
    monkeypatch.delenv("ORCH_SNOW_URL", raising=False)
    changes.set_record(acme, "oscar", change, "CHG0012345")
    with pytest.raises(plugins.Refused, match="no ticketing system"):
        changes.run(acme, "oscar", change, caps)
    monkeypatch.setenv("ORCH_SNOW_URL", fake.url)
    monkeypatch.setenv("ORCH_SNOW_USER", "orch")
    monkeypatch.setenv("ORCH_SNOW_PASSWORD", "pw")
    with pytest.raises(ValueError):
        changes.set_record(acme, "oscar", change, "anything; DROP")
    with pytest.raises(PermissionError):
        changes.set_record(acme, "vic", change, "CHG0012345")

    for record, reason in [(snow_record(approval="requested"), "not approved"),
                           (snow_record(state="0"), "not Scheduled or Implement"),
                           (snow_record(end_date="2020-01-01 00:00:00"), "outside"), (None, "no change record")]:
        fake.answer = lambda m, p, b, r=record: (200, {"result": [r] if r else []})
        with pytest.raises(plugins.Refused, match=reason):
            changes.run(acme, "oscar", change, caps)

    fake.answer = lambda m, p, b: (200, {"result": [snow_record()] if m == "GET" else {}})
    assert changes.run(acme, "oscar", change, caps)[0] == "VERIFIED"
    patch = [r for r in fake.seen if r["method"] == "PATCH"][-1]
    assert patch["path"] == "/api/now/table/change_request/abc123" and "ended VERIFIED" in patch["body"]["work_notes"]
    assert "ticket.noted" in audit_kinds(acme)
    with db.tenant(acme) as conn:
        assert changes.listing(conn)[0]["change_record"] == "CHG0012345"


def test_low_risk_changes_need_no_record_and_a_missing_one_is_named(acme):
    set_policy(acme, "change_record: [medium]")
    caps = change_cap([])
    change = approved_change(acme, caps)
    with pytest.raises(plugins.Refused, match="needs a change record"):
        changes.run(acme, "oscar", change, caps)
    set_policy(acme, "change_record: [high]")
    assert changes.run(acme, "oscar", change, caps)[0] == "VERIFIED"


# --- ChatOps webhooks ------------------------------------------------------------------------------------------------

def test_change_events_reach_every_webhook_with_a_link(acme, fake, monkeypatch):
    monkeypatch.setenv("ORCH_PUBLIC_URL", "https://orch.example.com")
    monkeypatch.setattr(notify, "send", wait_for(notify.send))
    with pytest.raises(ValueError):
        notify.add(acme, "cli", "teams", "http://plain-http.example.com")
    for kind in notify.KINDS:
        notify.add(acme, "cli", kind, fake.url.replace("http://", "https://"))
    with db.tenant(acme) as conn:  # the fake speaks plain HTTP; the stored address must still say https
        conn.execute("UPDATE webhooks SET url = replace(url, 'https://', 'http://')")

    caps = change_cap([])
    change = approved_change(acme, caps)
    changes.run(acme, "oscar", change, caps)
    events = [r["body"] for r in fake.seen]
    teams, slack, plain = events[0:3]  # change.planned first, to all three
    assert teams["attachments"][0]["content"]["actions"][0]["url"] == f"https://orch.example.com/#/changes/{change}"
    assert "oscar asked for ansible.test.set" in slack["text"] and plain["event"] == "change.planned"
    assert [e["event"] for e in events if "event" in e] == ["change.planned", "change.approved", "change.verified"]

    fake.answer = lambda m, p, b: (500, {})
    loop.set_kill_switch(acme, "alice", "incident")
    assert "notify.failed" in audit_kinds(acme)
    assert "alice stopped all agent work: incident" in fake.seen[-1]["body"].get("text", json.dumps(fake.seen[-1]))


def wait_for(send):
    def sync(*args, **kwargs):  # the tests wait for the background post so they can look at it
        thread = send(*args, **kwargs)
        if thread:
            thread.join(10)
    return sync


# --- SIEM ------------------------------------------------------------------------------------------------------------

def test_the_ledger_reaches_the_siem_once_per_row(acme, database):
    listener = socket.create_server(("127.0.0.1", 0))
    received = []

    def accept():
        while True:
            conn, _ = listener.accept()
            with conn:
                data = b""
                while chunk := conn.recv(65536):
                    data += chunk
            while data:  # RFC 6587 octet counting: "LENGTH SP MESSAGE"
                length, rest = data.split(b" ", 1)
                received.append(rest[:int(length)].decode())
                data = rest[int(length):]
    threading.Thread(target=accept, daemon=True).start()
    target = f"tcp://127.0.0.1:{listener.getsockname()[1]}"
    with db.tenant(acme) as conn:
        db.audit(conn, "alice", "test.event", {"n": 1})
        total = conn.execute("SELECT count(*) AS n FROM audit").fetchone()["n"]

    assert siem.forward_once(database, target) == total
    assert siem.forward_once(database, target) == 0  # the cursor remembers
    with db.tenant(acme) as conn:
        db.audit(conn, "alice", "test.event", {"n": 2})
    assert siem.forward_once(database, target) == 1
    for _ in range(50):
        if len(received) == total + 1:
            break
        threading.Event().wait(0.1)
    assert len(received) == total + 1
    last = received[-1]
    assert last.startswith("<110>1 ") and " rezztor-orchestrator - test.event - " in last
    body = json.loads(last.split(" - ", 2)[2])
    assert (body["tenant"], body["kind"], body["data"]) == (str(acme), "test.event", {"n": 2})
