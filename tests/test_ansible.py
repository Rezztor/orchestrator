import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from executors import ansible_executor as ex
from orchestrator import changes, db, loop, plugins
from tests.test_core import add_device, scripted, tool_call
from tests.test_policy_guard import add_user

pytestmark = pytest.mark.skipif(not shutil.which("ansible"), reason="ansible-core is not installed")
TOKEN = "t" * 40
BANNER = """- name: Set the login banner
  hosts: all
  gather_facts: false
  tasks:
    - name: Write the banner
      ansible.builtin.copy: {dest: "{{ orch.path }}", content: "{{ orch.text }}\\n"}
"""
REMOVE = BANNER.replace("Set", "Remove").replace("Write", "Remove").replace(
    'copy: {dest: "{{ orch.path }}", content: "{{ orch.text }}\\n"}', 'file: {path: "{{ orch.path }}", state: absent}')
BROKEN = BANNER + "    - name: Fail after writing\n      ansible.builtin.fail: {msg: device refused}\n"


@pytest.fixture
def executor(tmp_path, monkeypatch):
    """The executor on a free local port, with an inventory of one host Ansible reaches without a network."""
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("all:\n  hosts:\n    lab1:\n      ansible_connection: local\n"
                         f"      ansible_python_interpreter: {shutil.which('python3')}\n")
    monkeypatch.setattr(ex.Handler, "token", TOKEN)
    monkeypatch.setattr(ex.Handler, "inventory", str(inventory))
    books = tmp_path / "playbooks"
    books.mkdir()
    for name, text in [("banner", BANNER), ("banner.rollback", REMOVE), ("broken", BROKEN),
                       ("broken.rollback", REMOVE)]:
        (books / f"{name}.yml").write_text(text)
    monkeypatch.setattr(ex.Handler, "playbooks", str(books))
    server = ThreadingHTTPServer(("127.0.0.1", 0), ex.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("ORCH_ANSIBLE_URL", url)
    monkeypatch.setenv("ORCH_ANSIBLE_TOKEN", TOKEN)
    yield url, str(inventory)
    server.shutdown()


def post(url, body, token=TOKEN):
    req = urllib.request.Request(url + "/v1/jobs", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


@pytest.mark.parametrize("devices", [[], ["all"] * 201, ["*"], ["all:!lab1"], ["~.*"], ["@hosts.txt"], ["a,b"], [5]])
def test_the_executor_takes_only_plain_device_names(executor, devices):
    with pytest.raises(ValueError):
        ex.run_job("ansible.facts.read", devices, executor[1])


def test_the_executor_needs_its_token_and_a_known_capability(executor):
    url, _ = executor
    assert post(url, {"capability": "ansible.facts.read", "devices": ["lab1"]}, token="wrong")[0] == 401
    assert post(url, {"capability": "ansible.shell", "devices": ["lab1"]})[0] == 400


def test_facts_come_back_allow_listed(executor):
    status, body = post(executor[0], {"capability": "ansible.facts.read", "devices": ["lab1", "ghost"]})
    assert status == 200
    lab1 = body["hosts"]["lab1"]
    assert lab1["status"] == "ok" and lab1["facts"]["ansible_hostname"]
    assert set(lab1["facts"]) <= ex.FACT_KEYS and "ansible_env" not in lab1["facts"]
    assert body["hosts"]["ghost"] == {"status": "not_in_inventory"}


def test_the_agent_reads_facts_only_for_its_own_tenants_devices(database, executor):
    a = db.create_tenant("acme", "alice")
    b = db.create_tenant("globex", "bob")
    add_device(a, "lab1", "colombo")
    chat = scripted(tool_call("ansible__facts__read", {"devices": ["lab1"]}), {"content": "lab1 runs Linux."})
    assert loop.run(a, "alice", "What OS is lab1?", chat=chat)[1] == "DONE"
    result = json.loads(chat.seen[1][-1]["content"])["untrusted_tool_output"]["result"]
    assert result["lab1"]["status"] == "ok"

    chat = scripted(tool_call("ansible__facts__read", {"devices": ["lab1"]}), {"content": "Not mine."})
    loop.run(b, "bob", "What OS is lab1?", chat=chat)
    assert "not in this tenant's inventory: lab1" in json.loads(chat.seen[1][-1]["content"])["refused"]


def playbook(url, phase, name, path, text="Authorised use only", **extra):
    return post(url, {"capability": "ansible.playbook.run", "phase": phase, "playbook": name, "devices": ["lab1"],
                      "vars": {"path": str(path), "text": text}, **extra})


def test_a_playbook_runs_only_the_plan_that_was_approved(executor, tmp_path):
    url, banner = executor[0], tmp_path / "motd"
    status, plan = playbook(url, "plan", "banner", banner)
    assert status == 200 and plan["rollback"] is True and not banner.exists()  # check mode changes nothing
    task = plan["hosts"]["lab1"]["tasks"][0]
    assert task["changed"] and "+Authorised use only" in task["diff"]
    assert playbook(url, "plan", "banner", banner)[1]["digest"] == plan["digest"]

    assert playbook(url, "apply", "banner", banner, "Other text", digest=plan["digest"])[1]["outcome"] == "superseded"
    assert not banner.exists()
    status, run = playbook(url, "apply", "banner", banner, digest=plan["digest"])
    assert (status, run["outcome"], banner.read_text()) == (200, "verified", "Authorised use only\n")
    assert not any(t["changed"] for t in run["verify"]["lab1"]["tasks"])


def test_a_failed_apply_is_rolled_back(executor, tmp_path):
    url, banner = executor[0], tmp_path / "motd"
    plan = playbook(url, "plan", "broken", banner)[1]
    run = playbook(url, "apply", "broken", banner, digest=plan["digest"])[1]
    assert run["outcome"] == "rolled_back" and not banner.exists()
    assert run["applied"]["lab1"]["tasks"][-1]["message"] == "device refused"


@pytest.mark.parametrize("name, values", [("../banner", {}), ("banner.rollback", {}), ("nope", {}),
                                          ("banner", {"path": "{{ lookup('pipe', 'id') }}"}),
                                          ("banner", {"bad-name": "x"}), ("banner", {"path": ["a"]})])
def test_playbook_jobs_take_only_reviewed_names_and_plain_values(executor, name, values):
    body = {"capability": "ansible.playbook.run", "phase": "plan", "playbook": name, "devices": ["lab1"],
            "vars": values}
    assert post(executor[0], body)[0] == 400


def test_the_change_path_end_to_end(database, executor, tmp_path):
    t = db.create_tenant("acme", "alice")
    add_user(t, "oscar", "operator")
    add_device(t, "lab1", "colombo")
    banner = tmp_path / "motd"
    chat = scripted(tool_call("ansible__playbooks__list", {}),
                    tool_call("ansible__playbook__run", {"playbook": "banner", "devices": ["lab1"],
                                                         "vars": {"path": str(banner), "text": "Hi"}}),
                    {"content": "Planned."})
    assert loop.run(t, "oscar", "Set the banner on lab1 to Hi", chat=chat)[1] == "DONE"
    books = json.loads(chat.seen[1][-1]["content"])["untrusted_tool_output"]["result"]
    assert {"name": "banner", "description": "Set the login banner", "rollback": True, "vars": ["path", "text"]} in books
    planned = json.loads(chat.seen[2][-1]["content"])["untrusted_tool_output"]["result"]
    assert (planned["state"], planned["risk"]) == ("PLANNED", "medium") and not banner.exists()

    with db.tenant(t) as conn:
        digest = changes.listing(conn)[0]["digest"]
    assert changes.decide(t, "alice", planned["change"], digest, True) == "APPROVED"
    state, result = changes.run(t, "oscar", planned["change"], plugins.load())
    assert (state, banner.read_text()) == ("VERIFIED", "Hi\n")


def test_a_failed_dry_run_tells_the_agent_why(database, executor, tmp_path):
    t = db.create_tenant("acme", "alice")
    add_user(t, "oscar", "operator")
    add_device(t, "lab1", "colombo")
    chat = scripted(tool_call("ansible__playbook__run", {"playbook": "broken", "devices": ["lab1"],
                                                         "vars": {"path": str(tmp_path / "motd"), "text": "Hi"}}),
                    {"content": "It failed."})
    loop.run(t, "oscar", "Set the banner on lab1 to Hi", chat=chat)
    refused = json.loads(chat.seen[1][-1]["content"])["refused"]
    assert refused == "the dry run did not succeed on lab1 (failed: device refused)"
