import base64
import json
import os
import ssl
import urllib.error
import urllib.request

from orchestrator.plugins import Refused, known_devices, setting

# The query is fixed here; the model only supplies device names, which go in as parameters, never into the text.
FIELDS = ("Caption, IPAddress, Status, StatusDescription, ResponseTime, CPULoad, PercentMemoryUsed, LastBoot, "
          "MachineType")
# shortcut: a read-only SolarWinds account in the core's environment (ORCH_SOLARWINDS_URL/USER/PASSWORD/CA); move it
# behind an executor when a customer needs SolarWinds credentials off the core.


def query(swql, parameters):
    req = urllib.request.Request(
        setting("ORCH_SOLARWINDS_URL").rstrip("/") + "/SolarWinds/InformationService/v3/Json/Query",
        data=json.dumps({"query": swql, "parameters": parameters}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Basic " + base64.b64encode(
            f"{setting('ORCH_SOLARWINDS_USER')}:{setting('ORCH_SOLARWINDS_PASSWORD')}".encode()).decode()})
    ctx = ssl.create_default_context(cafile=os.environ.get("ORCH_SOLARWINDS_CA"))
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            return json.load(resp)["results"]
    except urllib.error.HTTPError as exc:
        raise Refused(f"SolarWinds answered {exc.code}: {exc.read()[:300].decode(errors='replace')}")


def node_status(conn, devices):
    known_devices(conn, devices)
    where = " OR ".join(f"Caption = @n{i}" for i in range(len(devices)))
    rows = query(f"SELECT {FIELDS} FROM Orion.Nodes WHERE {where}", {f"n{i}": d for i, d in enumerate(devices)})
    found = {r["Caption"]: r for r in rows}
    return {d: found.get(d, {"status": "not monitored in SolarWinds"}) for d in devices}
