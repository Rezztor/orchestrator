import json
import os
import ssl
import urllib.error
import urllib.request

from orchestrator.plugins import Refused

# ponytail: one executor per installation (ORCH_ANSIBLE_URL); binding an executor to each tenant comes with tenant settings.


def read_facts(conn, devices):
    if not 0 < len(devices) <= 50:
        raise Refused("ask for 1 to 50 devices at a time")
    known = {r["name"] for r in conn.execute("SELECT name FROM devices WHERE name = ANY(%s::text[])", (devices,))}
    unknown = sorted(set(devices) - known)
    if unknown:  # the executor only ever hears of this tenant's own devices
        raise Refused(f"not in this tenant's inventory: {', '.join(unknown)}")
    req = urllib.request.Request(
        os.environ["ORCH_ANSIBLE_URL"].rstrip("/") + "/v1/jobs",
        data=json.dumps({"capability": "ansible.facts.read", "devices": devices}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["ORCH_ANSIBLE_TOKEN"]})
    ctx = ssl.create_default_context(cafile=os.environ.get("ORCH_ANSIBLE_CA"))
    try:
        with urllib.request.urlopen(req, timeout=330, context=ctx) as resp:
            return json.load(resp)["hosts"]
    except urllib.error.HTTPError as exc:
        raise Refused(f"executor answered {exc.code}: {exc.read()[:300].decode(errors='replace')}")
