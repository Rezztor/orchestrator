"""The page against a freshly seeded test database and a scripted model, for check.js. See uat/README.md.

    ORCH_TEST_DB_URL   the test database (every table is dropped first; never one you care about)
    UAT_PORT           default 8099

python uat/browser/stand_in.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
OWNER = os.environ["ORCH_TEST_DB_URL"]
PORT = os.environ.get("UAT_PORT", "8099")
import psycopg  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

os.environ.update(ORCH_DB_URL=OWNER, ORCH_DB_APP_URL=make_conninfo(OWNER, user="orch_app", password="test-app-password"),
                  ORCH_PUBLIC_URL=f"http://localhost:{PORT}", ORCH_WEB_LISTEN=f"127.0.0.1:{PORT}")
from orchestrator import admin, auth, db, model, notify, plugins, recipes, web  # noqa: E402
from tests.test_changes import change_cap  # noqa: E402
from tests.test_core import add_device, tool_call  # noqa: E402
with psycopg.connect(OWNER, autocommit=True) as conn:
    conn.execute("DROP TABLE IF EXISTS triggers, routes, oidc_logins, oidc, sign_in_failures, siem_cursor, webhooks, "
                 "schedules, recipes, skills, approvals, changes, audit, devices, requests, sessions, policies, "
                 "kill_switch, installation_kill, users, tenants CASCADE")
db.migrate(OWNER, "test-app-password")
t = db.create_tenant("acme", "alice")
auth.set_password(t, "alice", "correct horse battery")
admin.add_person(t, "alice", "bob", "approver", "bob password 123")
admin.add_person(t, "alice", "oscar", "operator", "oscar password 1")
for name, site in (("core-sw1", "colombo"), ("edge-r1", "kandy"), ("core-sw2", "colombo")):
    add_device(t, name, site)
recipes.add(t, "cli", "health", "description: Check a site's devices\nprompt: Check the health of every device at {site}")
notify.add(t, "alice", "teams", "https://hooks.example/abc/SECRET")
caps = change_cap([])
plugins.load = lambda: caps


def chat(messages, tools, model_name=None):
    if messages[0]["content"].startswith("You review"):
        return {"content": "No concerns. The plan touches only core-sw1.", "usage": {"total_tokens": 50}}
    last = messages[-1]
    prompt = next(m["content"] for m in messages if m["role"] == "user")
    if last["role"] == "tool":
        if "banner" in prompt:
            return {"content": "I planned the banner on **core-sw1**. It waits for one approval.", "usage": {"total_tokens": 300}}
        return {"content": "## Devices\n\n| Name | Site |\n|---|---|\n| core-sw1 | colombo |\n| core-sw2 | colombo |\n\n"
                "- Both answer `show version`\n- See [the docs](https://example.com/docs)\n\n<b>not html</b>",
                "usage": {"total_tokens": 400}}
    if "banner" in prompt:
        return {**tool_call("ansible__test__set", {"devices": ["core-sw1"]}), "usage": {"total_tokens": 200}}
    return {**tool_call("inventory__devices__list", {}), "usage": {"total_tokens": 200}}


model.chat = chat
print(f"stand-in page on http://localhost:{PORT}", flush=True)
web.serve()
