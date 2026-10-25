import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from executors import ansible_executor as ex
from orchestrator import db, loop
from tests.test_core import add_device, scripted, tool_call

pytestmark = pytest.mark.skipif(not shutil.which("ansible"), reason="ansible-core is not installed")
TOKEN = "t" * 40


@pytest.fixture
def executor(tmp_path, monkeypatch):
    """The executor on a free local port, with an inventory of one host Ansible reaches without a network."""
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("all:\n  hosts:\n    lab1:\n      ansible_connection: local\n"
                         f"      ansible_python_interpreter: {shutil.which('python3')}\n")
    monkeypatch.setattr(ex.Handler, "token", TOKEN)
    monkeypatch.setattr(ex.Handler, "inventory", str(inventory))
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
