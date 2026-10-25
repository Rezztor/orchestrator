"""ServiceNow change records: required by risk class (policy `change_record`), checked before a run, noted after.

The orchestrator's own approvals still apply; a change record adds the organisation's change board. Before a run the
record must be approved, Scheduled or Implement, and inside its planned start and end. After the run its outcome is
written to the record's work notes.

    ORCH_SNOW_URL, ORCH_SNOW_USER, ORCH_SNOW_PASSWORD   a ServiceNow account that reads and notes change requests
    ORCH_SNOW_CA                                        CA bundle for a private certificate
"""
import base64
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .plugins import Refused

NUMBER = re.compile(r"[A-Z]{2,6}[0-9]{5,10}")
READY = {"-2": "Scheduled", "-1": "Implement"}  # ServiceNow's change_request state values


def call(method, path, body=None):
    if not os.environ.get("ORCH_SNOW_URL"):
        raise Refused("this change needs a change record, and no ticketing system is configured")
    login = base64.b64encode(f"{os.environ['ORCH_SNOW_USER']}:{os.environ['ORCH_SNOW_PASSWORD']}".encode()).decode()
    req = urllib.request.Request(
        os.environ["ORCH_SNOW_URL"].rstrip("/") + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": "Basic " + login})
    ctx = ssl.create_default_context(cafile=os.environ.get("ORCH_SNOW_CA"))
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return json.load(resp)["result"]
    except (OSError, ValueError, KeyError) as exc:  # URLError and HTTPError are OSErrors
        raise Refused(f"ServiceNow could not be asked: {exc}")


def record(number):
    query = urllib.parse.urlencode({"sysparm_query": f"number={number}", "sysparm_limit": "1",
                                    "sysparm_fields": "sys_id,number,state,approval,start_date,end_date"})
    rows = call("GET", f"/api/now/table/change_request?{query}")
    if not rows:
        raise Refused(f"no change record {number} in ServiceNow")
    return rows[0]


def check(number, now=None):
    """Refuse unless the record allows the change to run now. Returns the record."""
    row, now = record(number), now or datetime.now(timezone.utc)
    if row["approval"] != "approved":
        raise Refused(f"change record {number} is not approved (approval: {row['approval']})")
    if row["state"] not in READY:
        raise Refused(f"change record {number} is not Scheduled or Implement")
    window = [datetime.strptime(row[k], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if row[k] else None
              for k in ("start_date", "end_date")]  # ServiceNow's API speaks UTC
    if not all(window) or not window[0] <= now <= window[1]:
        raise Refused(f"now is outside change record {number}'s planned window")
    return row


def note(number, text):
    call("PATCH", f"/api/now/table/change_request/{record(number)['sys_id']}", {"work_notes": text})
