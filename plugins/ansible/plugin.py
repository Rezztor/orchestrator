import json
import os
import ssl
import urllib.error
import urllib.request

from orchestrator.plugins import Refused

# shortcut: one executor per installation (ORCH_ANSIBLE_URL); binding an executor to each tenant comes with tenant settings.


def job(body, timeout):
    req = urllib.request.Request(
        os.environ["ORCH_ANSIBLE_URL"].rstrip("/") + "/v1/jobs", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["ORCH_ANSIBLE_TOKEN"]})
    ctx = ssl.create_default_context(cafile=os.environ.get("ORCH_ANSIBLE_CA"))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise Refused(f"executor answered {exc.code}: {exc.read()[:300].decode(errors='replace')}")


def known(conn, devices):
    if not 0 < len(devices) <= 50:
        raise Refused("ask for 1 to 50 devices at a time")
    found = {r["name"] for r in conn.execute("SELECT name FROM devices WHERE name = ANY(%s::text[])", (devices,))}
    unknown = sorted(set(devices) - found)
    if unknown:  # the executor only ever hears of this tenant's own devices
        raise Refused(f"not in this tenant's inventory: {', '.join(unknown)}")


def read_facts(conn, devices):
    known(conn, devices)
    return job({"capability": "ansible.facts.read", "devices": devices}, 330)["hosts"]


def list_playbooks(conn):
    return job({"capability": "ansible.playbooks.list"}, 30)["playbooks"]


def plan_playbook(conn, playbook, devices, vars=None):
    known(conn, devices)
    plan = job({"capability": "ansible.playbook.run", "phase": "plan", "playbook": playbook, "devices": devices,
                "vars": vars or {}}, 660)
    return {**plan, "targets": devices}


def apply_playbook(digest, playbook, devices, vars=None):
    # plan, apply, verify and perhaps rollback, each up to 10 minutes on the executor
    return job({"capability": "ansible.playbook.run", "phase": "apply", "digest": digest, "playbook": playbook,
                "devices": devices, "vars": vars or {}}, 2500)
